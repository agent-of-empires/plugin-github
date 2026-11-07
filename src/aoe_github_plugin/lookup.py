"""PR discovery across a checkout's origin and the other GitHub remotes it has.

A fork's PR is opened against the upstream repository, so searching only
``origin`` misses it (#129). The destinations are the checkout's own configured
GitHub remotes: no metadata call and no lineage inference, because a remote that
is not configured was never a destination this checkout could target. A
fork-of-fork needs its middle remote configured to be visible, which is the
price of that rule.

The head identity rule is what makes this safe: a PR matches only when both its
branch name AND its head repository equal the checkout's own origin. A
contributor's identically named branch against the same upstream therefore never
binds to this checkout.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from collections.abc import Callable

from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError

Repository = tuple[str, str]
# (destination owner, destination repo, branch, head owner, head repo)
PullKey = tuple[str, str, str, str, str]

# A ``head=owner:branch`` filter can span several repositories of one
# destination; five pages is far past any realistic case and bounds the loop.
MAX_PULL_PAGES = 5

# The identity-only connection that resolves a branch to its PRs before any rich
# query runs: ten tiny nodes per page bounded by ``MAX_PULL_PAGES``.
DISCOVER_PAGE_SIZE = 10
DISCOVER_QUERY = f"""query($owner: String!, $repo: String!, $branch: String!, $cursor: String) {{
  rateLimit {{ cost remaining resetAt }}
  repository(owner: $owner, name: $repo) {{
    pullRequests(headRefName: $branch, states: [OPEN, MERGED], first: {DISCOVER_PAGE_SIZE}, after: $cursor) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ number state headRefOid headRefName headRepository {{ name owner {{ login }} }} }}
    }}
  }}
}}"""


def repositories(origin: Repository, remotes: list[Repository]) -> list[Repository]:
    """Repositories to search: ``origin`` first, then every other configured
    GitHub remote, deduplicated case-insensitively.

    Origin stays first because it is authoritative for freshness and errors (see
    ``open_pulls``): an optional remote must never hide a healthy origin result.
    """
    seen = {_casefold(origin)}
    out = [origin]
    for repo in remotes:
        key = _casefold(repo)
        if key not in seen:
            seen.add(key)
            out.append(repo)
    return out


def _owner_repo(data: dict[str, Any]) -> Repository | None:
    owner = (data.get("owner") or {}).get("login")
    name = data.get("name")
    return (owner, name) if isinstance(owner, str) and isinstance(name, str) else None


def _casefold(repo: Repository) -> tuple[str, str]:
    return repo[0].casefold(), repo[1].casefold()


def pull_keys(repos: list[Repository], branch: str) -> list[PullKey]:
    """One lookup per destination. Every key pins the head to ``repos[0]``.

    The head is unconditional: only the checkout's own origin can own the
    branch, so the same branch pushed to a different fork never matches. An
    empty list means there is no destination to query at all.
    """
    if not repos:
        return []
    head_owner, head_repo = repos[0]
    return [(owner, repo, branch, head_owner, head_repo) for owner, repo in dict.fromkeys(repos)]


def matches_head(head: Any, key: PullKey) -> bool:
    """A branch name alone is not identity: require its actual repository too."""
    if not isinstance(head, dict) or head.get("ref") != key[2]:
        return False
    found = _owner_repo(head.get("repo") or {})
    return found is not None and _casefold(found) == (key[3].casefold(), key[4].casefold())


def graphql_head(node: dict[str, Any]) -> dict[str, Any]:
    return {"ref": node.get("headRefName"), "repo": node.get("headRepository")}


def discover_pages(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
    observe: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Every OPEN/MERGED PR with this branch name in one destination, paged
    through the identity-only connection.

    Returns ``(nodes, complete, rate_limited)``. ``complete`` is True only when
    the server itself reported the last page (``hasNextPage`` false). It is
    False whenever the answer cannot be proven: the page bound was reached with
    another page still pending, the cursor was unusable, or the response was an
    error envelope (a 200 carrying ``errors``, e.g. ``RATE_LIMITED``, or a
    missing ``repository``/connection). The caller then falls back to the
    owner-scoped REST list instead of caching an empty result as authoritative
    ownership. A final page that happens to be exactly full is still complete,
    because the server's own ``hasNextPage`` is the authority, not the node
    count.

    ``rate_limited`` is True when an envelope reported a ``RATE_LIMITED`` error,
    so the caller arms the shared GraphQL backoff instead of retrying into it.
    ``observe`` receives each envelope's ``rateLimit`` payload: discovery's spend
    is real budget, so it is recorded like every other query rather than
    silently unaccounted.

    This runs BEFORE any rich query: the aliased full and digest documents cost
    30-110 points per document, so paging them would burn budget on a busy
    branch name. Here a page is a handful of scalars, and the caller keeps only
    the owned nodes and asks for their rich fields by number.
    """
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PULL_PAGES):
        data = client.post_graphql(
            DISCOVER_QUERY,
            {"owner": owner, "repo": repo, "branch": branch, "cursor": cursor},
        )
        rate_info = (data.get("data") or {}).get("rateLimit")
        if observe is not None and isinstance(rate_info, dict):
            observe(rate_info)
        errors = data.get("errors")
        rate_limited = _is_rate_limited_envelope(errors)
        repository = (data.get("data") or {}).get("repository")
        if errors or not isinstance(repository, dict):
            # An error envelope is not an empty census: it proves nothing about
            # which PRs exist, so it must never be cached as "no owned PRs".
            return nodes, False, rate_limited
        pull_requests = repository.get("pullRequests")
        if not isinstance(pull_requests, dict):
            return nodes, False, rate_limited
        page = pull_requests.get("pageInfo")
        if not isinstance(page, dict):
            return nodes, False, rate_limited
        nodes.extend(node for node in pull_requests.get("nodes") or [] if isinstance(node, dict))
        if not page.get("hasNextPage"):
            return nodes, True, rate_limited
        cursor = page.get("endCursor")
        if not isinstance(cursor, str):
            return nodes, False, rate_limited
    return nodes, False, False


def _is_rate_limited_envelope(errors: Any) -> bool:
    """GitHub signals a GraphQL secondary rate limit as a 200 carrying an error
    of type ``RATE_LIMITED`` (not an HTTP 4xx), so read it from the envelope."""
    return isinstance(errors, list) and any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors)


def filter_connection(conn: Any, key: PullKey) -> Any:
    if not isinstance(conn, dict):
        return conn
    nodes = [n for n in conn.get("nodes") or [] if isinstance(n, dict) and matches_head(graphql_head(n), key)]
    return {**conn, "nodes": nodes}


def destination_of(url: Any, fallback: Repository) -> Repository:
    """The repository a PR lives in, parsed from its URL.

    A PR's head may be the fork while it belongs to upstream, so labelling it
    with the checkout's origin misnames both the repo and the number. Mirrors
    the selector id in ``uistate._pull_key`` so a row and its summary agree.
    """
    if isinstance(url, str):
        parts = urlsplit(url).path.strip("/").split("/")
        if len(parts) == 4 and parts[2] == "pull":
            return parts[0], parts[1]
    return fallback


def trim_pull(pr: dict[str, Any]) -> dict[str, Any]:
    """Basic REST representation, retaining the real head rather than the destination."""
    return {
        "number": pr.get("number"),
        "url": pr.get("html_url"),
        "title": pr.get("title") or "",
        "state": pr.get("state"),
        "draft": bool(pr.get("draft", False)),
        "head": pr.get("head"),
    }


def _page_pulls(client: GitHubClient, owner: str, repo: str, branch: str, head_owner: str) -> list[dict[str, Any]]:
    """Every open PR with ``head_owner:branch`` as its head, across pages.

    The ``head`` filter is ``owner:branch``, so one owner with the same branch
    name open against several repositories of this destination can exceed a
    single page. Bounded so an unexpected full-page loop cannot spin.
    """
    raw: list[dict[str, Any]] = []
    for page in range(1, MAX_PULL_PAGES + 1):
        batch = client.get_json(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open", "head": f"{head_owner}:{branch}", "per_page": "100", "page": str(page)},
        )
        if not isinstance(batch, list):
            break
        raw.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return raw


def open_pulls(client: GitHubClient, keys: list[PullKey]) -> list[dict[str, Any]]:
    """Open PRs matching every key, deduplicated by URL.

    Origin (``keys[0]``) is authoritative: a failing optional upstream must never
    hide a healthy origin result, so only the origin's own failure surfaces. It
    surfaces only when nothing else found the PR either: an upstream hit is a
    real answer, and raising over it would send ``github.open`` to a compare URL
    that claims no PR exists.
    """
    pulls: dict[str, dict[str, Any]] = {}
    origin_error: GitHubError | None = None
    for index, key in enumerate(keys):
        owner, repo, branch, head_owner, _ = key
        try:
            raw = _page_pulls(client, owner, repo, branch, head_owner)
        except GitHubError as exc:
            if index == 0:
                origin_error = exc
            continue
        for pr in raw:
            if isinstance(pr, dict) and matches_head(pr.get("head"), key):
                pull = trim_pull(pr)
                pulls[pull["url"]] = pull
    if origin_error is not None and not pulls:
        raise origin_error
    return list(pulls.values())

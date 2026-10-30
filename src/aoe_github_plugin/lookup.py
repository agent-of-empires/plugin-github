"""PR discovery across a checkout's origin and the upstream it may be a fork of.

GitHub reports a fork's lineage directly: ``GET /repos/{owner}/{repo}`` carries
``parent`` (the immediate upstream) and ``source`` (the root of the fork line),
both absent for a non-fork. That is the only fork signal used here - no remote
scanning and no guessing by name.

The head identity rule is what makes this safe: a PR matches only when both its
branch name AND its head repository equal the checkout's own origin. A
contributor's identically named branch against the same upstream therefore never
binds to this checkout.
"""

from __future__ import annotations

import time
import threading
from typing import Any

from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError

Repository = tuple[str, str]
# (destination owner, destination repo, branch, head owner, head repo)
PullKey = tuple[str, str, str, str, str]

PARENT_TTL = 3600.0
PARENT_RETRY = 60.0
# A ``head=owner:branch`` filter can span several repositories of one
# destination; five pages is far past any realistic case and bounds the loop.
MAX_PULL_PAGES = 5

_parent_lock = threading.Lock()
_parent_cache: dict[Repository, tuple[float, Repository | None]] = {}


def repositories(
    client: GitHubClient,
    origin: Repository,
    upstream: Repository | None = None,
    *,
    metadata_allowed: bool = True,
) -> list[Repository]:
    """Repositories to search: origin first, then the fork's upstream if any.

    An explicitly configured ``upstream`` remote wins; otherwise GitHub's
    ``parent`` field supplies the upstream once per TTL. Metadata failure leaves
    origin usable and retries after a short cooldown, and a known parent stays
    usable throughout so a transient error never hides upstream PRs.
    """
    if upstream is None:
        cache_key = origin[0].casefold(), origin[1].casefold()
        with _parent_lock:
            cached = _parent_cache.get(cache_key)
        upstream = cached[1] if cached else None
        now = time.monotonic()
        if metadata_allowed and (cached is None or now >= cached[0]):
            # The request runs outside the lock: a slow repo must not stall
            # lookups for every other checkout. A racing caller may repeat it;
            # both observe the same answer.
            try:
                data = client.get_json(f"/repos/{origin[0]}/{origin[1]}")
                parent = data.get("parent") if isinstance(data, dict) else None
                upstream = _owner_repo(parent) if isinstance(parent, dict) else None
            except GitHubError:
                with _parent_lock:
                    _parent_cache[cache_key] = now + PARENT_RETRY, upstream
            else:
                with _parent_lock:
                    _parent_cache[cache_key] = now + PARENT_TTL, upstream
    repos = [origin]
    if upstream and _casefold(upstream) != _casefold(origin):
        repos.append(upstream)
    return repos


def _owner_repo(data: dict[str, Any]) -> Repository | None:
    owner = (data.get("owner") or {}).get("login")
    name = data.get("name")
    return (owner, name) if isinstance(owner, str) and isinstance(name, str) else None


def _casefold(repo: Repository) -> tuple[str, str]:
    return repo[0].casefold(), repo[1].casefold()


def pull_keys(repos: list[Repository], branch: str) -> list[PullKey]:
    """One lookup per destination. Every key pins the head to ``repos[0]``.

    The head is unconditional: only the checkout's own origin can own the
    branch, so the same branch pushed to a different fork never matches.
    """
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


def filter_connection(conn: Any, key: PullKey) -> Any:
    if not isinstance(conn, dict):
        return conn
    nodes = [n for n in conn.get("nodes") or [] if isinstance(n, dict) and matches_head(graphql_head(n), key)]
    return {**conn, "nodes": nodes}


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
    hide a healthy origin result, so only the origin's own failure surfaces.
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
    if origin_error is not None:
        raise origin_error
    return list(pulls.values())

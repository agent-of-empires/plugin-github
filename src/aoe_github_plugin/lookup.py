"""Bounded PR discovery across a checkout's origin and upstream repository."""

from __future__ import annotations

import time
import threading
from typing import Any

from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError

Repository = tuple[str, str]
PullKey = tuple[str, str, str, str, str]  # destination owner/repo, branch, head owner/repo
PARENT_TTL = 3600.0
PARENT_RETRY = 60.0
_parent_lock = threading.Lock()
_parent_cache: dict[Repository, tuple[float, Repository | None]] = {}


def repositories(
    client: GitHubClient,
    origin: Repository,
    upstream: Repository | None = None,
    *,
    metadata_allowed: bool = True,
) -> list[Repository]:
    """Use an explicit upstream, else cached direct-parent metadata; never scan remotes.

    Metadata failure leaves origin usable and is retried after a short cooldown.
    Successful non-fork responses are cached too. A stale parent remains usable
    during a REST backoff or a transient metadata failure.
    """
    if upstream is None:
        cache_key = origin[0].casefold(), origin[1].casefold()
        with _parent_lock:
            cached = _parent_cache.get(cache_key)
            upstream = cached[1] if cached else None
            now = time.monotonic()
            if metadata_allowed and (cached is None or now >= cached[0]):
                try:
                    data = client.get_json(f"/repos/{origin[0]}/{origin[1]}")
                    parent = data.get("parent") if isinstance(data, dict) else None
                    owner = (parent.get("owner") or {}).get("login") if isinstance(parent, dict) else None
                    name = parent.get("name") if isinstance(parent, dict) else None
                    upstream = (owner, name) if isinstance(owner, str) and isinstance(name, str) else None
                except GitHubError:
                    _parent_cache[cache_key] = now + PARENT_RETRY, upstream
                else:
                    _parent_cache[cache_key] = now + PARENT_TTL, upstream
    repos = [origin]
    if upstream and tuple(s.casefold() for s in upstream) != tuple(s.casefold() for s in origin):
        repos.append(upstream)
    return repos


def pull_keys(repos: list[Repository], branch: str) -> list[PullKey]:
    """Keep destination and permitted head identities separate in every cache key."""
    return [(owner, repo, branch, head_owner, head_repo) for owner, repo in repos for head_owner, head_repo in repos]


def matches_head(head: Any, key: PullKey) -> bool:
    """A branch name alone is not identity: require its actual repository too."""
    if not isinstance(head, dict) or head.get("ref") != key[2]:
        return False
    repo = head.get("repo") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    return (
        isinstance(owner, str)
        and isinstance(name, str)
        and owner.casefold() == key[3].casefold()
        and name.casefold() == key[4].casefold()
    )


def graphql_head(node: dict[str, Any]) -> dict[str, Any]:
    return {"ref": node.get("headRefName"), "repo": node.get("headRepository")}


def filter_connection(conn: Any, key: PullKey) -> Any:
    if not isinstance(conn, dict):
        return conn
    return {
        **conn,
        "nodes": [n for n in conn.get("nodes") or [] if isinstance(n, dict) and matches_head(graphql_head(n), key)],
    }


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


def open_pulls(client: GitHubClient, keys: list[PullKey]) -> list[dict[str, Any]]:
    pulls: dict[str, dict[str, Any]] = {}
    first_error: GitHubError | None = None
    for key in keys:
        owner, repo, branch, head_owner, _ = key
        try:
            raw = client.get_json(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "open", "head": f"{head_owner}:{branch}", "per_page": "100"},
            )
        except GitHubError as exc:
            first_error = first_error or exc
            continue
        for pr in raw:
            if isinstance(pr, dict) and matches_head(pr.get("head"), key):
                pull = trim_pull(pr)
                pulls[pull["url"]] = pull
    if not pulls and first_error is not None:
        raise first_error
    return list(pulls.values())

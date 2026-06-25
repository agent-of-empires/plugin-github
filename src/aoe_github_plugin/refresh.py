"""Multi-session GitHub refresh: discover the repos in every session's
workspace, look up each repo's open PR, and assemble one aggregate snapshot the
UI mapper turns into ``ui.state.set`` pushes.

This module is the network/filesystem half of the proactive refresh. It does
NOT touch stdin or call host RPCs (those belong to the main loop in ``main``);
it is handed the session list and returns a plain-dict snapshot. Everything is
fail-soft: a bad workspace, a detached HEAD, a non-github checkout, or an API
error degrades that one repo's entry, never the whole refresh.

Efficiency, for GitHub's 60 req/hr unauthenticated ceiling:
- repos are deduplicated by ``(owner, repo, branch)`` so a branch shared across
  sessions/workspaces is fetched once per refresh;
- lookups use conditional requests (ETag / ``If-None-Match``); a ``304`` does
  not count against the primary rate limit;
- a ``403``/``429`` trips a short global backoff, during which cached (stale)
  values are served instead of hammering the API.
"""

from __future__ import annotations

import os
import time
import threading
import subprocess
from typing import Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx

from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.handlers import _resolve_optional_token
from aoe_github_plugin.utils.gitctx import parse_owner_repo

GIT_TIMEOUT = 2.0
MAX_WORKERS = 10
# ponytail: fixed 60s backoff after a rate-limit response, not the precise
# X-RateLimit-Reset (which classify_status discards). Short enough to recover
# quickly, long enough to stop hammering. Parse the reset header if it matters.
BACKOFF_SECS = 60.0

RepoKey = tuple[str, str, str]  # (owner, repo, branch)

# Cross-refresh ETag cache and a single rate-limit backoff gate. The HTTP
# fan-out threads read/write both, so guard with a lock.
_cache_lock = threading.Lock()
_etag_cache: dict[RepoKey, dict[str, Any]] = {}  # key -> {"etag", "pulls"}
# Mutable holder (not a bare module float) so updating it needs no `global`.
_backoff = {"until": 0.0}  # time.monotonic() seconds; HTTP is skipped until then


def _git(path: str, *args: str) -> str | None:
    """Run ``git -C path <args>`` with a hard timeout. Returns stripped stdout,
    or ``None`` on any failure (non-zero exit, timeout, git missing). Fail-soft:
    discovery and identity must never raise per repo."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def discover_checkouts(workspace: str) -> list[str]:
    """Git checkouts in a session workspace: the workspace root and each
    immediate child directory that is its own checkout. Worktree-safe (a
    worktree's ``.git`` is a file, so we ask git, not the filesystem) and
    shallow (no deep scan into ``node_modules`` etc).

    A candidate counts only if ``git rev-parse --show-toplevel`` equals the
    candidate itself; that rejects subdirectories of a repo rooted higher up
    (so a workspace that is itself one repo does not report every child).
    """
    candidates = [workspace]
    try:
        with os.scandir(workspace) as it:
            candidates += [e.path for e in it if e.is_dir()]
    except OSError:
        pass
    out: list[str] = []
    seen: set[str] = set()
    for cand in candidates:
        top = _git(cand, "rev-parse", "--show-toplevel")
        if not top:
            continue
        real = os.path.realpath(top)
        if real != os.path.realpath(cand):
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(cand)
    return out


def _identify(path: str) -> tuple[str | None, RepoKey | None]:
    """``(repo, key)`` for a checkout: ``repo`` is ``"owner/name"`` (or ``None``
    if the origin is not a github.com remote, a benign skip); ``key`` is the
    dedup key, or ``None`` on a detached HEAD (known repo, no branch to query)."""
    remote = _git(path, "remote", "get-url", "origin")
    owner_repo = parse_owner_repo(remote) if remote else None
    if owner_repo is None:
        return None, None
    owner, repo = owner_repo
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        return f"{owner}/{repo}", None
    return f"{owner}/{repo}", (owner, repo, branch)


def _trim(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open PRs trimmed to the fields the UI renders. Defensive: a malformed
    entry from a drifting API shape is skipped/defaulted, never raised."""
    return [
        {
            "number": pr.get("number"),
            "url": pr.get("html_url"),
            "title": pr.get("title") or "",
            "state": pr.get("state"),
            "draft": bool(pr.get("draft", False)),
        }
        for pr in raw
        if isinstance(pr, dict)
    ]


def _set_backoff() -> None:
    with _cache_lock:
        _backoff["until"] = time.monotonic() + BACKOFF_SECS


def _fetch_key(client: GitHubClient, key: RepoKey) -> list[dict[str, Any]]:
    """Open PRs for one ``(owner, repo, branch)``. Uses the ETag cache and
    honors the backoff gate. Raises ``GitHubError`` only when there is no cached
    value to fall back on."""
    owner, repo, branch = key
    with _cache_lock:
        cached = _etag_cache.get(key)
        blocked = time.monotonic() < _backoff["until"]
    if blocked:
        if cached is not None:
            return cached["pulls"]
        raise RateLimitedError
    etag = cached["etag"] if cached else None
    params = {"state": "open", "head": f"{owner}:{branch}", "per_page": "10"}
    try:
        status, new_etag, raw = client.get_json_conditional(f"/repos/{owner}/{repo}/pulls", params, etag)
    except RateLimitedError:
        _set_backoff()
        if cached is not None:
            return cached["pulls"]
        raise
    if status == 304 and cached is not None:
        return cached["pulls"]
    pulls = _trim(raw or [])
    with _cache_lock:
        _etag_cache[key] = {"etag": new_etag, "pulls": pulls}
    return pulls


def _fetch_all(
    keys: set[RepoKey],
    env: TokenEnvironment | None,
    transport: httpx.BaseTransport | None,
) -> dict[RepoKey, dict[str, Any]]:
    """Fetch every unique key concurrently. Returns ``key -> {"pulls": [...]}``
    or ``key -> {"error": {kind, hint}}``. httpx.Client is safe to share across
    the pool threads; none of them touch stdin or host RPCs."""
    if not keys:
        return {}
    token = _resolve_optional_token(env)
    with GitHubClient(token=token, transport=transport) as client:

        def one(key: RepoKey) -> tuple[RepoKey, dict[str, Any]]:
            try:
                return key, {"pulls": _fetch_key(client, key)}
            except GitHubError as exc:
                return key, {"error": {"kind": exc.kind, "hint": str(exc)}}
            except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
                return key, {"error": {"kind": "internal", "hint": f"refresh failed: {exc}"}}

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(keys))) as ex:
            return dict(ex.map(one, keys))


def build_snapshot(
    sessions: list[dict[str, Any]],
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Assemble the aggregate snapshot from a host ``sessions.list`` result.

    Returns ``{"sessions": [{session_id, title, project_path, repos: [...]}]}``
    where each repo is ``{path, name, repo, branch, pulls, error}``. Pure of IO
    side effects on the host channel; only filesystem + GitHub HTTP. ``env`` and
    ``transport`` are test seams.
    """
    per_session: list[tuple[dict[str, Any], list[str]]] = []
    for session in sessions:
        path = session.get("project_path")
        checkouts = discover_checkouts(path) if path else []
        per_session.append((session, checkouts))

    ident: dict[str, tuple[str | None, RepoKey | None]] = {}
    keys: set[RepoKey] = set()
    for _, checkouts in per_session:
        for checkout in checkouts:
            repo_str, key = _identify(checkout)
            ident[checkout] = (repo_str, key)
            if key is not None:
                keys.add(key)

    fetched = _fetch_all(keys, env, transport)

    out_sessions: list[dict[str, Any]] = []
    for session, checkouts in per_session:
        repos: list[dict[str, Any]] = []
        for checkout in checkouts:
            repo_str, key = ident[checkout]
            entry: dict[str, Any] = {
                "path": checkout,
                "name": Path(checkout).name or checkout,
                "repo": repo_str,
                "branch": key[2] if key else None,
                "pulls": [],
                "error": None,
            }
            if key is not None:
                result = fetched.get(key, {})
                entry["pulls"] = result.get("pulls", [])
                entry["error"] = result.get("error")
            repos.append(entry)
        out_sessions.append(
            {
                "session_id": session.get("id"),
                "title": session.get("title") or "",
                "project_path": session.get("project_path"),
                "repos": repos,
            }
        )
    return {"sessions": out_sessions}

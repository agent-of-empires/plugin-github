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
from datetime import datetime
from datetime import timezone
from concurrent.futures import ThreadPoolExecutor

import httpx

from aoe_github_plugin import graphql
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import ApiError
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.handlers import _resolve_optional_token
from aoe_github_plugin.utils.gitctx import parse_owner_repo

GIT_TIMEOUT = 2.0
MAX_WORKERS = 10
# Back off proactively once the GraphQL point budget (5000/hr) is nearly spent,
# so a busy workspace degrades to stale data rather than hard rate-limit errors.
RATELIMIT_FLOOR = 50
# GraphQL has no ETag/304, so a per-key TTL is its only conditional mechanism: a
# key re-queried within this window serves its cached result instead of spending
# points. Floors the cost even if the poll cadence is set aggressively low.
GRAPHQL_TTL = 60.0
# Fixed fallback backoff when no precise reset is known (REST 403/429, which
# classify_status discards the reset for). The GraphQL path prefers the
# response's rateLimit.resetAt; see _set_backoff.
BACKOFF_SECS = 60.0
# Cap a reset-derived backoff so a far-future or skewed resetAt cannot wedge the
# worker for an unreasonable stretch.
MAX_BACKOFF_SECS = 3600.0

RepoKey = tuple[str, str, str]  # (owner, repo, branch)

# Cross-refresh ETag cache and a single rate-limit backoff gate. The HTTP
# fan-out threads read/write both, so guard with a lock.
_cache_lock = threading.Lock()
_etag_cache: dict[RepoKey, dict[str, Any]] = {}  # key -> {"etag", "pulls"}
# Last-good rich (GraphQL) result per key: {"pulls", "fetched_at"}. Serves the
# TTL window and is the stale fallback while rate-limited.
_graphql_cache: dict[RepoKey, dict[str, Any]] = {}
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


def _reset_to_monotonic(reset_at: Any) -> float | None:
    """An ISO-8601 ``rateLimit.resetAt`` -> a ``time.monotonic`` deadline, or
    ``None`` when it is absent/unparseable/in the past. Capped so a skewed or
    far-future reset cannot wedge the worker."""
    if not isinstance(reset_at, str):
        return None
    try:
        ts = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (ts - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return None
    return time.monotonic() + min(delta, MAX_BACKOFF_SECS)


def _set_backoff(reset_at: Any = None) -> None:
    """Arm the shared backoff gate. Prefers the GraphQL response's ``resetAt``
    (so we wait exactly until the budget refills); falls back to a fixed window
    for the REST path, which has no usable reset."""
    until = _reset_to_monotonic(reset_at)
    with _cache_lock:
        _backoff["until"] = until if until is not None else time.monotonic() + BACKOFF_SECS


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


def _graphql_rate_limited(data: dict[str, Any]) -> bool:
    """GitHub signals a GraphQL secondary rate limit as a 200 carrying an error
    of type ``RATE_LIMITED`` (not an HTTP 4xx), so detect it in the envelope."""
    errors = data.get("errors")
    return isinstance(errors, list) and any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors)


def _graphql_no_repository(
    data: dict[str, Any],
    rate_info: dict[str, Any],
    cached: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Resolve a response that carried no ``repository``: serve stale if we have
    it, else back off (rate limit) or surface the error. A genuinely null repo
    (private/not found) is an empty result, not an error."""
    if _graphql_rate_limited(data):
        _set_backoff(rate_info.get("resetAt"))
        if cached is not None:
            return cached["pulls"]
        raise RateLimitedError
    errors = data.get("errors")
    if errors:
        if cached is not None:
            return cached["pulls"]
        message = next((e.get("message") for e in errors if isinstance(e, dict) and e.get("message")), "GraphQL error")
        raise ApiError(200, str(message))
    return []


def _fetch_key_graphql(client: GitHubClient, key: RepoKey, *, force: bool = False) -> list[dict[str, Any]]:
    """Rich PR data for one ``(owner, repo, branch)`` via GraphQL. Serves the
    per-key TTL cache, honors the backoff gate, and prefers any usable response
    data (a GraphQL partial success is kept, not discarded). Raises
    ``GitHubError`` only when there is nothing to fall back on. ``force`` skips
    the TTL freshness check (a user-initiated refresh wants live CI/review data,
    not a cache hit) while still respecting the backoff gate and stale fallback,
    so it never hammers a rate-limited API."""
    owner, repo, branch = key
    now = time.monotonic()
    with _cache_lock:
        cached = _graphql_cache.get(key)
        blocked = now < _backoff["until"]
    if not force and cached is not None and (now - cached["fetched_at"]) < GRAPHQL_TTL:
        return cached["pulls"]
    if blocked:
        if cached is not None:
            return cached["pulls"]
        raise RateLimitedError
    variables = {"owner": owner, "repo": repo, "branch": branch}
    try:
        data = client.post_graphql(graphql.QUERY, variables)
    except RateLimitedError:
        _set_backoff()
        if cached is not None:
            return cached["pulls"]
        raise

    payload = data.get("data") or {}
    rate_info = payload.get("rateLimit") or {}
    if payload.get("repository") is None:
        return _graphql_no_repository(data, rate_info, cached)

    pulls = graphql.normalize_pulls(payload)
    # Arm backoff for the next refresh if a rate limit rode along with the data,
    # or the remaining point budget is running low.
    remaining = rate_info.get("remaining")
    if _graphql_rate_limited(data) or (isinstance(remaining, int) and remaining < RATELIMIT_FLOOR):
        _set_backoff(rate_info.get("resetAt"))
    with _cache_lock:
        _graphql_cache[key] = {"pulls": pulls, "fetched_at": time.monotonic()}
    return pulls


def _fetch_all(
    keys: set[RepoKey],
    env: TokenEnvironment | None,
    transport: httpx.BaseTransport | None,
    *,
    force: bool = False,
) -> tuple[dict[RepoKey, dict[str, Any]], bool]:
    """Fetch every unique key concurrently. Returns ``(results, token_present)``
    where results is ``key -> {"pulls": [...]}`` or ``key -> {"error": {...}}``.
    With a token, each key is fetched via the rich GraphQL path; without one we
    keep the REST open-PR path and report ``token_present=False`` so the UI can
    show the "token needed" banner. ``force`` bypasses the GraphQL TTL on the
    token path (a manual refresh); the REST path always revalidates via ETag so
    it ignores the flag. httpx.Client is safe to share across the pool threads;
    none of them touch stdin or host RPCs."""
    if not keys:
        return {}, True
    token = _resolve_optional_token(env)
    present = token is not None
    fetch = (lambda c, k: _fetch_key_graphql(c, k, force=force)) if present else _fetch_key
    with GitHubClient(token=token, transport=transport) as client:

        def one(key: RepoKey) -> tuple[RepoKey, dict[str, Any]]:
            try:
                return key, {"pulls": fetch(client, key)}
            except GitHubError as exc:
                return key, {"error": {"kind": exc.kind, "hint": str(exc)}}
            except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
                return key, {"error": {"kind": "internal", "hint": f"refresh failed: {exc}"}}

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(keys))) as ex:
            return dict(ex.map(one, keys)), present


def build_snapshot(
    sessions: list[dict[str, Any]],
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Assemble the aggregate snapshot from a host ``sessions.list`` result.

    Returns ``{"sessions": [{session_id, title, project_path, repos: [...]}],
    "auth": {"present": bool}}`` where each repo is
    ``{path, name, repo, branch, pulls, error}``. With a token the pulls carry
    rich fields (state/merged/review_state/checks/comments); without one they
    are the basic REST shape and ``auth.present`` is ``False``. ``force`` bypasses
    the GraphQL TTL so a user-initiated refresh fetches live CI/review data. Pure
    of IO side effects on the host channel; only filesystem + GitHub HTTP. ``env``
    and ``transport`` are test seams.
    """
    per_session: list[tuple[dict[str, Any], list[str]]] = []
    for session in sessions:
        path = session.get("project_path")
        checkouts = discover_checkouts(path) if isinstance(path, str) and path else []
        per_session.append((session, checkouts))

    # Identity (two git calls) is keyed by real path so a checkout shared across
    # sessions is resolved once, not once per occurrence.
    ident: dict[str, tuple[str | None, RepoKey | None]] = {}
    keys: set[RepoKey] = set()
    for _, checkouts in per_session:
        for checkout in checkouts:
            checkout_id = os.path.realpath(checkout)
            if checkout_id in ident:
                continue
            repo_str, key = _identify(checkout)
            ident[checkout_id] = (repo_str, key)
            if key is not None:
                keys.add(key)

    fetched, auth_present = _fetch_all(keys, env, transport, force=force)

    out_sessions: list[dict[str, Any]] = []
    for session, checkouts in per_session:
        repos: list[dict[str, Any]] = []
        for checkout in checkouts:
            repo_str, key = ident[os.path.realpath(checkout)]
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
    return {"sessions": out_sessions, "auth": {"present": auth_present}}

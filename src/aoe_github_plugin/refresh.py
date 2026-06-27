"""Multi-session GitHub refresh: discover the repos in every session's
workspace, look up each repo's open PR, and assemble one aggregate snapshot the
UI mapper turns into ``ui.state.set`` pushes.

This module is the network/filesystem half of the proactive refresh. It does
NOT touch stdin or call host RPCs (those belong to the main loop in ``main``);
it is handed the session list and returns a plain-dict snapshot. Everything is
fail-soft: a bad workspace, a detached HEAD, a non-github checkout, or an API
error degrades that one repo's entry, never the whole refresh.

Efficiency, against the user token's shared budget (REST 5000 req/hr, GraphQL
5000 points/hr):
- repos are deduplicated by ``(owner, repo, branch)`` so a branch shared across
  sessions/workspaces is fetched once per refresh;
- a cheap REST conditional request (ETag / ``If-None-Match``) is the PRIMARY
  poll; a ``304`` does not count against the primary rate limit, so a steady
  state where nothing changed costs ~0;
- the expensive rich GraphQL query fires only when that conditional check
  reports a change, on an explicit (forced) refresh, or when the cached rich
  result has aged past ``GRAPHQL_MAX_STALE`` (a freshness ceiling, so CI/review
  state cannot lie indefinitely between PR-list changes);
- requests are issued serially (no concurrent fan-out) to stay clear of the
  secondary/concurrency limits;
- a ``403``/``429``/``RATE_LIMITED`` trips a short global backoff, during which
  cached (stale) values are served instead of hammering the API.
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
# Back off proactively once the GraphQL point budget (5000/hr) is nearly spent,
# so a busy workspace degrades to stale data rather than hard rate-limit errors.
RATELIMIT_FLOOR = 50
# Freshness ceiling for the rich (GraphQL) cache. The REST conditional check
# gates GraphQL on a detected change, but the ``/pulls`` ETag does not reliably
# bump on a CI check completing or a review thread changing, so a rich result
# this old is refreshed even on a ``304``. Bounds how stale CI/review state can
# get (a user-clicked refresh forces it immediately regardless).
GRAPHQL_MAX_STALE = 300.0
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
#   until        - time.monotonic() seconds; HTTP is skipped until then.
#   reset_known  - True iff `until` came from a parsed GraphQL `resetAt` (so the
#                  countdown is real); False for the fixed REST fallback.
#   notified     - whether this window's user-facing notice has been emitted, so
#                  a user-initiated refresh announces a backoff at most once.
_backoff: dict[str, Any] = {"until": 0.0, "reset_known": False, "notified": False}


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
    for the REST path, which has no usable reset.

    A fresh window (the previous one already expired) clears the ``notified``
    flag so the next user-initiated refresh announces it once. Re-arming an
    already-active window only extends the deadline and upgrades ``reset_known``;
    it never shortens the wait, downgrades a real reset to the fallback, or
    re-opens the notice (the fan-out threads call this repeatedly per window)."""
    until = _reset_to_monotonic(reset_at)
    now = time.monotonic()
    new_until = until if until is not None else now + BACKOFF_SECS
    new_known = until is not None
    with _cache_lock:
        if now >= _backoff["until"]:  # fresh window
            _backoff["until"] = new_until
            _backoff["reset_known"] = new_known
            _backoff["notified"] = False
        else:  # extend the live window; never downgrade
            _backoff["until"] = max(_backoff["until"], new_until)
            _backoff["reset_known"] = _backoff["reset_known"] or new_known


def _consume_rate_limit_notice() -> dict[str, Any] | None:
    """Claim this backoff window's one user-facing notice. Returns
    ``{"seconds", "reset_known"}`` the first time it is called while a backoff is
    active and unannounced (then marks the window announced), else ``None``. Only
    a user-initiated (forced) refresh consumes it; background ticks never do, so a
    rate-limited workspace is not nagged on every poll."""
    now = time.monotonic()
    with _cache_lock:
        until = _backoff["until"]
        if now >= until or _backoff["notified"]:
            return None
        _backoff["notified"] = True
        return {"seconds": until - now, "reset_known": _backoff["reset_known"]}


def _forced_rate_limit_notice(*, force: bool) -> dict[str, Any]:
    """The ``rate_limit_notice`` snapshot fragment for a forced refresh: a single
    ``{"rate_limit_notice": {...}}`` when a backoff is active and unannounced,
    else ``{}``. Background refreshes (``force=False``) never announce."""
    if not force:
        return {}
    notice = _consume_rate_limit_notice()
    return {"rate_limit_notice": notice} if notice is not None else {}


def _rest_probe(client: GitHubClient, key: RepoKey) -> tuple[bool, list[dict[str, Any]]]:
    """Cheap REST conditional check for one ``(owner, repo, branch)``: returns
    ``(changed, pulls)`` where ``changed`` is ``False`` on a ``304`` (the open-PR
    list is unchanged, the request was free against the primary limit). Uses the
    ETag cache and honors the backoff gate; a blocked or rate-limited probe with
    a cached value reports ``changed=False`` and serves the cache, and only
    raises ``GitHubError`` when there is nothing to fall back on."""
    owner, repo, branch = key
    with _cache_lock:
        cached = _etag_cache.get(key)
        blocked = time.monotonic() < _backoff["until"]
    if blocked:
        if cached is not None:
            return False, cached["pulls"]
        raise RateLimitedError
    etag = cached["etag"] if cached else None
    params = {"state": "open", "head": f"{owner}:{branch}", "per_page": "10"}
    try:
        status, new_etag, raw = client.get_json_conditional(f"/repos/{owner}/{repo}/pulls", params, etag)
    except RateLimitedError:
        _set_backoff()
        if cached is not None:
            return False, cached["pulls"]
        raise
    if status == 304 and cached is not None:
        return False, cached["pulls"]
    pulls = _trim(raw or [])
    with _cache_lock:
        _etag_cache[key] = {"etag": new_etag, "pulls": pulls}
    return True, pulls


def _fetch_key(client: GitHubClient, key: RepoKey) -> list[dict[str, Any]]:
    """Open PRs for one ``(owner, repo, branch)`` via the basic REST path (no
    token). The conditional probe is the whole job here; the change flag only
    matters on the token path, which gates GraphQL on it."""
    _, pulls = _rest_probe(client, key)
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


def _fetch_key_graphql(client: GitHubClient, key: RepoKey) -> list[dict[str, Any]]:
    """Rich PR data for one ``(owner, repo, branch)`` via GraphQL. Honors the
    backoff gate and prefers any usable response data (a GraphQL partial success
    is kept, not discarded). Raises ``GitHubError`` only when there is nothing to
    fall back on. The decision of *whether* to spend a GraphQL query (vs serving
    the cache) belongs to ``_fetch_key_rich_gated``, which gates on the cheap
    REST conditional check; this only runs once that gate has decided to fetch."""
    owner, repo, branch = key
    with _cache_lock:
        cached = _graphql_cache.get(key)
        blocked = time.monotonic() < _backoff["until"]
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


def _fetch_key_rich_gated(client: GitHubClient, key: RepoKey, *, force: bool = False) -> list[dict[str, Any]]:
    """Rich PR data for one key, with the cheap REST conditional check as the
    primary gate (#21). Fires the expensive GraphQL query only when ``force`` (a
    user refresh / first load), the REST probe reports a change, there is no rich
    cache yet, or the cached rich result has aged past ``GRAPHQL_MAX_STALE``;
    otherwise it serves the cached rich pulls for ~0 rate-limit cost. Fail-soft:
    if GraphQL fails it falls back to the rich cache, then the basic REST pulls,
    so a transient rich-path failure never blanks a PR."""
    now = time.monotonic()
    with _cache_lock:
        rich = _graphql_cache.get(key)
    try:
        changed, basic = _rest_probe(client, key)
    except GitHubError:
        if rich is not None:
            return rich["pulls"]
        raise
    stale = rich is None or (now - rich["fetched_at"]) >= GRAPHQL_MAX_STALE
    if not force and not changed and not stale and rich is not None:
        return rich["pulls"]
    try:
        return _fetch_key_graphql(client, key)
    except GitHubError:
        if rich is not None:
            return rich["pulls"]
        return basic


def _fetch_all(
    keys: set[RepoKey],
    env: TokenEnvironment | None,
    transport: httpx.BaseTransport | None,
    *,
    force: bool = False,
) -> tuple[dict[RepoKey, dict[str, Any]], bool]:
    """Fetch every unique key SERIALLY. Returns ``(results, token_present)``
    where results is ``key -> {"pulls": [...]}`` or ``key -> {"error": {...}}``.
    With a token, each key goes through the REST-conditional-gated rich path
    (``_fetch_key_rich_gated``); without one we keep the basic REST open-PR path
    and report ``token_present=False`` so the UI can show the "token needed"
    banner. ``force`` (a manual refresh) bypasses the staleness gate on the token
    path. Requests are serialized (sorted for determinism) rather than fanned
    out, to stay under GitHub's secondary/concurrency limits (#22); none of this
    touches stdin or host RPCs."""
    if not keys:
        return {}, True
    token = _resolve_optional_token(env)
    present = token is not None
    fetch = (lambda c, k: _fetch_key_rich_gated(c, k, force=force)) if present else _fetch_key

    def one(client: GitHubClient, key: RepoKey) -> dict[str, Any]:
        """Fail-soft per repo: a typed error or any surprise becomes an error
        entry for that one key, never aborting the whole refresh."""
        try:
            return {"pulls": fetch(client, key)}
        except GitHubError as exc:
            return {"error": {"kind": exc.kind, "hint": str(exc)}}
        except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
            return {"error": {"kind": "internal", "hint": f"refresh failed: {exc}"}}

    results: dict[RepoKey, dict[str, Any]] = {}
    with GitHubClient(token=token, transport=transport) as client:
        for key in sorted(keys):
            results[key] = one(client, key)
    return results, present


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
    the GraphQL TTL so a user-initiated refresh fetches live CI/review data, and a
    forced refresh that is rate-limited adds a one-shot ``rate_limit_notice``
    (``{"seconds", "reset_known"}``) for the main loop to surface (issue #20). Pure
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
    # A forced refresh that is rate-limited carries a one-shot notice so the main
    # loop can tell the user why nothing changed (issue #20); background ticks
    # stay silent. See _forced_rate_limit_notice.
    return {
        "sessions": out_sessions,
        "auth": {"present": auth_present},
        **_forced_rate_limit_notice(force=force),
    }

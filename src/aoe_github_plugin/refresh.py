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
from collections import defaultdict
from dataclasses import dataclass

import httpx

from aoe_github_plugin import graphql
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.client import GitHubClient
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
# Shorter ceiling for a branch whose cached rich state is ACTIVE: a CI check is
# running or queued (#26). Such state transitions in seconds and the ``/pulls``
# ETag does not bump for it, so the 300s ceiling would lag a finishing CI run by
# minutes. A small floor (vs 0) still refreshes on every background tick while
# deduping sub-tick bursts (closely spaced session-list changes, host retries).
# Scoped to CI on purpose: a PR merely awaiting review can sit idle for days, so
# polling it every tick would burn budget for no signal; it keeps the 300s gate.
GRAPHQL_ACTIVE_STALE = 30.0
# Max branches aliased into one batched GraphQL query (#25). Caps the per-query
# point cost (cost scales with aliases x their nested connections) so a repo with
# many worktrees splits across a few serial queries rather than one giant one.
MAX_GRAPHQL_ALIASES = 10
# Hard cap on review threads paginated for one PR (#28), matching uistate's
# render cap. The graceful last-resort bound against a pathological PR.
MAX_REVIEW_THREADS = 500
# Fixed fallback backoff when no precise reset is known (REST 403/429, which
# classify_status discards the reset for). The GraphQL path prefers the
# response's rateLimit.resetAt; see _set_backoff.
BACKOFF_SECS = 60.0
# Cap a reset-derived backoff so a far-future or skewed resetAt cannot wedge the
# worker for an unreasonable stretch.
MAX_BACKOFF_SECS = 3600.0

RepoKey = tuple[str, str, str]  # (owner, repo, branch)
RichCacheKey = tuple[str, str, str, bool]


@dataclass(frozen=True)
class SnapshotSettings:
    ignore_submodules: bool = True
    required_checks_only: bool = False


DEFAULT_SNAPSHOT_SETTINGS = SnapshotSettings()

# Cross-refresh ETag cache and a single rate-limit backoff gate. The HTTP
# fan-out threads read/write both, so guard with a lock.
_cache_lock = threading.Lock()
_etag_cache: dict[RepoKey, dict[str, Any]] = {}  # key -> {"etag", "pulls"}
# Last-good rich (GraphQL) result per key: {"pulls", "fetched_at"}. Serves the
# TTL window and is the stale fallback while rate-limited.
_graphql_cache: dict[RichCacheKey, dict[str, Any]] = {}
# Last successful full refresh per session id, scoped to the repo-key set so a
# changed workspace never inherits a timestamp from unrelated data.
_session_refresh_cache: dict[str, dict[str, Any]] = {}
# Mutable holder (not a bare module float) so updating it needs no `global`.
#   until        - time.monotonic() seconds; HTTP is skipped until then.
#   reset_known  - True iff `until` came from a parsed GraphQL `resetAt` (so the
#                  countdown is real); False for the fixed REST fallback.
#   notified     - whether this window's user-facing notice has been emitted, so
#                  a user-initiated refresh announces a backoff at most once.
_backoff: dict[str, Any] = {"until": 0.0, "reset_known": False, "notified": False}


def _utc_now_iso() -> str:
    """Current UTC wall-clock time for user-facing freshness metadata."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _pulls_result(pulls: list[dict[str, Any]], *, fresh: bool, stale: bool = False) -> dict[str, Any]:
    """Per-key result fragment with internal freshness flags for aggregation."""
    return {"pulls": pulls, "_fresh": fresh, "_stale": stale}


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


def _is_submodule_checkout(path: str) -> bool:
    return bool(_git(path, "rev-parse", "--show-superproject-working-tree"))


def discover_checkouts(workspace: str, *, ignore_submodules: bool = True) -> list[str]:
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
    workspace_real = os.path.realpath(workspace)
    for cand in candidates:
        top = _git(cand, "rev-parse", "--show-toplevel")
        if not top:
            continue
        real = os.path.realpath(top)
        if real != os.path.realpath(cand):
            continue
        if ignore_submodules and real != workspace_real and _is_submodule_checkout(cand):
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


def _rest_probe(client: GitHubClient, key: RepoKey) -> tuple[bool, list[dict[str, Any]], bool]:
    """Cheap REST conditional check for one ``(owner, repo, branch)``: returns
    ``(changed, pulls, fresh)`` where ``changed`` is ``False`` on a ``304`` (the
    open-PR list is unchanged, the request was free against the primary limit).
    A ``304`` is still fresh for the basic REST representation. Uses the
    ETag cache and honors the backoff gate; a blocked or rate-limited probe with
    a cached value reports ``fresh=False`` because it serves stale fallback, and
    only raises ``GitHubError`` when there is nothing to fall back on."""
    owner, repo, branch = key
    with _cache_lock:
        cached = _etag_cache.get(key)
        blocked = time.monotonic() < _backoff["until"]
    if blocked:
        if cached is not None:
            return False, cached["pulls"], False
        raise RateLimitedError
    etag = cached["etag"] if cached else None
    params = {"state": "open", "head": f"{owner}:{branch}", "per_page": "10"}
    try:
        status, new_etag, raw = client.get_json_conditional(f"/repos/{owner}/{repo}/pulls", params, etag)
    except RateLimitedError:
        _set_backoff()
        if cached is not None:
            return False, cached["pulls"], False
        raise
    if status == 304 and cached is not None:
        return False, cached["pulls"], True
    pulls = _trim(raw or [])
    with _cache_lock:
        _etag_cache[key] = {"etag": new_etag, "pulls": pulls}
    return True, pulls, True


def _fetch_key(client: GitHubClient, key: RepoKey) -> tuple[list[dict[str, Any]], bool]:
    """Open PRs for one ``(owner, repo, branch)`` via the basic REST path (no
    token). The conditional probe is the whole job here; the change flag only
    matters on the token path, which gates GraphQL on it."""
    _, pulls, fresh = _rest_probe(client, key)
    return pulls, fresh


def _fetch_one_basic(client: GitHubClient, key: RepoKey) -> dict[str, Any]:
    """Fail-soft no-token result for one key: a typed error or any surprise
    becomes an error entry for that key, never aborting the whole refresh."""
    try:
        pulls, fresh = _fetch_key(client, key)
        return _pulls_result(pulls, fresh=fresh, stale=not fresh)
    except GitHubError as exc:
        return _error_entry(exc)
    except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
        return {"error": {"kind": "internal", "hint": f"refresh failed: {exc}"}, "_fresh": False, "_stale": True}


def _graphql_rate_limited(data: dict[str, Any]) -> bool:
    """GitHub signals a GraphQL secondary rate limit as a 200 carrying an error
    of type ``RATE_LIMITED`` (not an HTTP 4xx), so detect it in the envelope."""
    errors = data.get("errors")
    return isinstance(errors, list) and any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in errors)


def _error_entry(exc: GitHubError) -> dict[str, Any]:
    """A typed error rendered as the per-key result fragment the UI mapper reads."""
    return {"error": {"kind": exc.kind, "hint": str(exc)}, "_fresh": False, "_stale": True}


def _rich_cache_key(key: RepoKey, *, required_checks_only: bool) -> RichCacheKey:
    owner, repo, branch = key
    return owner, repo, branch, required_checks_only


def _is_active(pulls: list[dict[str, Any]]) -> bool:
    """A branch's cached rich state is ACTIVE when a non-merged PR has a CI check
    running or queued (#26). That state finishes in seconds yet does not bump the
    ``/pulls`` ETag, so it earns the shorter staleness ceiling. A PR merely
    awaiting review is deliberately NOT active: it can idle for days, so polling
    it every tick would burn budget for no signal."""
    for pull in pulls:
        if pull.get("merged"):
            continue
        checks = pull.get("checks")
        if not isinstance(checks, dict):
            continue
        if checks.get("state") in ("running", "queued"):
            return True
        if any(isinstance(run, dict) and run.get("state") in ("running", "queued") for run in checks.get("runs") or []):
            return True
    return False


def _rich_stale(rich: dict[str, Any] | None, now: float) -> bool:
    """Whether the cached rich result is missing or aged past its ceiling: the
    short ``GRAPHQL_ACTIVE_STALE`` for an active branch (#26), else the 300s one."""
    if rich is None:
        return True
    ceiling = GRAPHQL_ACTIVE_STALE if _is_active(rich["pulls"]) else GRAPHQL_MAX_STALE
    return (now - rich["fetched_at"]) >= ceiling


def _error_aliases(data: dict[str, Any]) -> set[str]:
    """Aliases (``b0``/``b1``/...) named in a GraphQL ``errors[].path`` so a
    field-level failure is attributed to its own branch, not the whole group.
    Errors with no usable path (e.g. a global RATE_LIMITED) name nothing here and
    are handled by the budget/backoff check instead."""
    out: set[str] = set()
    for err in data.get("errors") or []:
        if not isinstance(err, dict):
            continue
        path = err.get("path")
        if isinstance(path, list) and len(path) >= 2 and path[0] == "repository" and isinstance(path[1], str):
            out.add(path[1])
    return out


def _paginate_threads(client: GitHubClient, pull_node: dict[str, Any]) -> None:
    """When a PR's first reviewThreads page did not cover them all (#28), fetch
    the rest by the node id and append the raw thread nodes onto ``pull_node`` IN
    PLACE, so the normalizer counts every unresolved comment. Bounded by
    ``MAX_REVIEW_THREADS`` and fail-soft: any error keeps the first page rather
    than blanking comments (the next refresh retries the tail)."""
    threads = pull_node.get("reviewThreads")
    node_id = pull_node.get("id")
    if not isinstance(threads, dict) or not isinstance(node_id, str):
        return
    nodes = threads.get("nodes")
    if not isinstance(nodes, list):
        return
    page = threads.get("pageInfo") or {}
    cursor = page.get("endCursor")
    try:
        while page.get("hasNextPage") and isinstance(cursor, str) and len(nodes) < MAX_REVIEW_THREADS:
            with _cache_lock:
                if time.monotonic() < _backoff["until"]:
                    return  # the budget gate tripped (e.g. the batched query armed it); stop here
            data = client.post_graphql(graphql.THREADS_PAGE_QUERY, {"id": node_id, "cursor": cursor})
            if _graphql_rate_limited(data):
                _set_backoff(((data.get("data") or {}).get("rateLimit") or {}).get("resetAt"))
                return
            conn = ((data.get("data") or {}).get("node") or {}).get("reviewThreads") or {}
            more = [n for n in (conn.get("nodes") or []) if isinstance(n, dict)]
            if not more:
                break
            nodes.extend(more[: MAX_REVIEW_THREADS - len(nodes)])  # never overshoot the cap on a full last page
            page = conn.get("pageInfo") or {}
            cursor = page.get("endCursor")
    except RateLimitedError:
        _set_backoff()
        return
    except GitHubError:
        return


def _fallback_pulls(pending: dict[str, Any]) -> dict[str, Any]:
    """Per-key result when GraphQL could not produce fresh data: the last-good
    rich cache if present, else the basic REST open-PR pulls. Never an error and
    never accidentally empty, preserving the fail-soft per-key contract."""
    rich = pending["rich"]
    return _pulls_result(rich["pulls"] if rich is not None else pending["basic"], fresh=False, stale=True)


def _chunk_fallback(
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    out: dict[RepoKey, dict[str, Any]],
) -> None:
    """Fall back every key in a chunk (blocked gate or a whole-query failure)."""
    for key in chunk:
        out[key] = _fallback_pulls(pending[key])


def _resolve_null_repository(
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    data: dict[str, Any],
    out: dict[RepoKey, dict[str, Any]],
) -> None:
    """Per-key result when the batched response carried no ``repository``: prefer
    the last-good rich cache; a GraphQL error with no cache degrades to the basic
    REST pulls (never blanks a PR over a transient failure); a genuinely null repo
    (no errors) is an empty result, matching the prior single-key behavior."""
    errored = bool(data.get("errors"))
    for key in chunk:
        rich = pending[key]["rich"]
        if rich is not None:
            out[key] = _pulls_result(rich["pulls"], fresh=False, stale=True)
        elif errored:
            out[key] = _pulls_result(pending[key]["basic"], fresh=False, stale=True)
        else:
            out[key] = _pulls_result([], fresh=True)


def _apply_aliases(
    client: GitHubClient,
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    data: dict[str, Any],
    *,
    required_checks_only: bool,
) -> dict[RepoKey, dict[str, Any]]:
    """Normalize each alias connection back into its key's cache + result. A failed
    (named in ``errors[].path``) or malformed alias falls back per key, never
    poisoning its siblings. ``data`` is the full envelope (its ``repository`` is a
    dict here, validated by the caller)."""
    repository = (data.get("data") or {}).get("repository") or {}
    failed = _error_aliases(data)
    now = time.monotonic()
    out: dict[RepoKey, dict[str, Any]] = {}
    for i, key in enumerate(chunk):
        conn = repository.get(f"b{i}")
        if f"b{i}" in failed or not isinstance(conn, dict):
            out[key] = _fallback_pulls(pending[key])
            continue
        for node in conn.get("nodes") or []:
            if isinstance(node, dict):
                _paginate_threads(client, node)
        pulls = graphql.normalize_connection(conn, required_checks_only=required_checks_only)
        with _cache_lock:
            _graphql_cache[_rich_cache_key(key, required_checks_only=required_checks_only)] = {
                "pulls": pulls,
                "fetched_at": now,
            }
        out[key] = _pulls_result(pulls, fresh=True)
    return out


def _fetch_chunk(
    client: GitHubClient,
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    out: dict[RepoKey, dict[str, Any]],
    *,
    required_checks_only: bool,
) -> None:
    """One batched GraphQL query for up to ``MAX_GRAPHQL_ALIASES`` branches of one
    repo (#25), aliased ``b0:``/``b1:``/... ``chunk`` is non-empty and all keys
    share one ``(owner, repo)``. Writes a per-key result into ``out`` for every
    key in ``chunk``, isolating failures: a blocked/rate-limited query or a single
    failed alias falls back per key (rich cache, then basic REST), never
    group-wide."""
    owner, repo, _ = chunk[0]
    with _cache_lock:
        blocked = time.monotonic() < _backoff["until"]
    if blocked:
        _chunk_fallback(chunk, pending, out)
        return

    variables: dict[str, Any] = {"owner": owner, "repo": repo}
    for i, key in enumerate(chunk):
        variables[f"b{i}"] = key[2]
    try:
        data = client.post_graphql(graphql.build_query(len(chunk)), variables)
    except RateLimitedError:
        _set_backoff()
        _chunk_fallback(chunk, pending, out)
        return
    except GitHubError:
        _chunk_fallback(chunk, pending, out)
        return

    payload = data.get("data") or {}
    rate_info = payload.get("rateLimit") or {}
    repository = payload.get("repository")
    # Arm backoff for the next refresh on a riding rate-limit error or a low
    # remaining budget, after caching whatever good data this response carries.
    remaining = rate_info.get("remaining")
    if _graphql_rate_limited(data) or (isinstance(remaining, int) and remaining < RATELIMIT_FLOOR):
        _set_backoff(rate_info.get("resetAt"))

    if not isinstance(repository, dict):
        _resolve_null_repository(chunk, pending, data, out)
        return
    out.update(_apply_aliases(client, chunk, pending, data, required_checks_only=required_checks_only))


def _fetch_rich(
    client: GitHubClient, keys: list[RepoKey], *, force: bool, required_checks_only: bool
) -> dict[RepoKey, dict[str, Any]]:
    """Token path: a cheap REST conditional probe gates each key (#21), then the
    keys that need fresh GraphQL are grouped by ``(owner, repo)`` and aliased into
    one batched query per group, chunked to ``MAX_GRAPHQL_ALIASES`` (#25). Gating
    is PER KEY before grouping, so one active branch never drags its quiescent
    siblings into a fetch. Fail-soft per key: a probe error or GraphQL failure
    serves the rich cache, then basic REST pulls, then a typed error."""
    now = time.monotonic()
    out: dict[RepoKey, dict[str, Any]] = {}
    pending: dict[RepoKey, dict[str, Any]] = {}
    for key in keys:
        cache_key = _rich_cache_key(key, required_checks_only=required_checks_only)
        with _cache_lock:
            rich = _graphql_cache.get(cache_key)
        try:
            changed, basic, _fresh = _rest_probe(client, key)
        except GitHubError as exc:
            out[key] = _pulls_result(rich["pulls"], fresh=False, stale=True) if rich is not None else _error_entry(exc)
            continue
        if rich is not None and not force and not changed and not _rich_stale(rich, now):
            out[key] = _pulls_result(rich["pulls"], fresh=False)
            continue
        pending[key] = {"basic": basic, "rich": rich}

    groups: dict[tuple[str, str], list[RepoKey]] = defaultdict(list)
    for key in pending:
        groups[(key[0], key[1])].append(key)
    for gkeys in groups.values():
        gkeys.sort(key=lambda k: k[2])
        for start in range(0, len(gkeys), MAX_GRAPHQL_ALIASES):
            chunk = gkeys[start : start + MAX_GRAPHQL_ALIASES]
            try:
                _fetch_chunk(client, chunk, pending, out, required_checks_only=required_checks_only)
            except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
                for key in chunk:
                    out.setdefault(
                        key,
                        {
                            "error": {"kind": "internal", "hint": f"refresh failed: {exc}"},
                            "_fresh": False,
                            "_stale": True,
                        },
                    )
    return out


def _session_fingerprint(keys: list[RepoKey]) -> tuple[RepoKey, ...]:
    """Stable identity for the GitHub data a session pane displays."""
    return tuple(sorted(set(keys)))


def _session_freshness(
    session_id: Any,
    fingerprint: tuple[RepoKey, ...],
    results: list[dict[str, Any]],
    refreshed_at: str,
) -> dict[str, Any] | None:
    """Freshness metadata for one session, conservative on partial failure."""
    if not isinstance(session_id, str) or not fingerprint:
        return None
    all_fresh = all(result.get("_fresh") is True and not result.get("error") for result in results)
    stale = any(
        result.get("_stale") is True
        or result.get("error") is not None
        or (result.get("_fresh") is not True and not result.get("pulls"))
        for result in results
    )
    with _cache_lock:
        cached = _session_refresh_cache.get(session_id)
        if all_fresh:
            _session_refresh_cache[session_id] = {"fingerprint": fingerprint, "refreshed_at": refreshed_at}
            return {"refreshed_at": refreshed_at, "stale": False}
        if cached and cached.get("fingerprint") == fingerprint:
            return {"refreshed_at": cached["refreshed_at"], "stale": stale}
    return None


def _fetch_all(
    keys: set[RepoKey],
    env: TokenEnvironment | None,
    transport: httpx.BaseTransport | None,
    *,
    force: bool = False,
    required_checks_only: bool = False,
) -> tuple[dict[RepoKey, dict[str, Any]], bool]:
    """Fetch every unique key. Returns ``(results, token_present)`` where results
    is ``key -> {"pulls": [...]}`` or ``key -> {"error": {...}}``. With a token,
    keys go through the REST-conditional-gated rich path and same-repo branches
    that need GraphQL share one batched query (``_fetch_rich``); without one we
    keep the basic REST open-PR path and report ``token_present=False`` so the UI
    can show the "token needed" banner. ``force`` (a manual refresh) bypasses the
    staleness gate on the token path. GraphQL is batched per repo but otherwise
    serialized (sorted for determinism), to stay under GitHub's secondary/
    concurrency limits (#22); none of this touches stdin or host RPCs."""
    if not keys:
        return {}, True
    token = _resolve_optional_token(env)
    present = token is not None
    ordered = sorted(keys)
    results: dict[RepoKey, dict[str, Any]] = {}
    with GitHubClient(token=token, transport=transport) as client:
        if present:
            results = _fetch_rich(client, ordered, force=force, required_checks_only=required_checks_only)
        else:
            for key in ordered:
                results[key] = _fetch_one_basic(client, key)
    return results, present


def build_snapshot(
    sessions: list[dict[str, Any]],
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
    *,
    force: bool = False,
    settings: SnapshotSettings = DEFAULT_SNAPSHOT_SETTINGS,
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
        checkouts = (
            discover_checkouts(path, ignore_submodules=settings.ignore_submodules)
            if isinstance(path, str) and path
            else []
        )
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

    fetched, auth_present = _fetch_all(
        keys,
        env,
        transport,
        force=force,
        required_checks_only=settings.required_checks_only,
    )
    refreshed_at = _utc_now_iso()

    out_sessions: list[dict[str, Any]] = []
    for session, checkouts in per_session:
        repos: list[dict[str, Any]] = []
        session_keys: list[RepoKey] = []
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
                session_keys.append(key)
                result = fetched.get(key, {})
                entry["pulls"] = result.get("pulls", [])
                entry["error"] = result.get("error")
            repos.append(entry)
        fingerprint = _session_fingerprint(session_keys)
        freshness = _session_freshness(
            session.get("id"),
            fingerprint,
            [fetched.get(key, {}) for key in fingerprint],
            refreshed_at,
        )
        out_session = {
            "session_id": session.get("id"),
            "title": session.get("title") or "",
            "project_path": session.get("project_path"),
            "repos": repos,
        }
        if freshness is not None:
            out_session["freshness"] = freshness
        out_sessions.append(out_session)
    # A forced refresh that is rate-limited carries a one-shot notice so the main
    # loop can tell the user why nothing changed (issue #20); background ticks
    # stay silent. See _forced_rate_limit_notice.
    return {
        "sessions": out_sessions,
        "auth": {"present": auth_present},
        **_forced_rate_limit_notice(force=force),
    }

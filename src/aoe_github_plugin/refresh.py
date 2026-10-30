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
- lookups are deduplicated by destination, branch, and head owner/repository,
  keeping shared workspaces cheap without mixing contributors' same-name branches;
- a cheap REST conditional request (ETag / ``If-None-Match``) is the PRIMARY
  open-PR poll; a ``304`` is free, so that path costs ~0 when unchanged (native
  stack enrichment is separate and can add calls as its cache expires);
- the expensive rich GraphQL query fires only when that conditional check
  reports a change, on an explicit (forced) refresh, on a cold cache, or when
  the cheap DIGEST query (#69) detects a change past the digest ceiling; the
  ceilings exist so CI/review state cannot lie indefinitely between PR-list
  changes, and the digest tier keeps revalidating them near-free (a hard
  ``GRAPHQL_FULL_MAX_STALE`` bounds what the digest cannot see);
- local git identity and the REST probes fan out on small bounded pools (#70);
  GraphQL queries stay serial to stay clear of the secondary/concurrency
  limits;
- a ``403``/``429``/``RATE_LIMITED`` on primary queries trips global backoff;
  cached values are served instead of hammering the API;
- native stack enrichment requires a token and uses its own cache and cooldown;
  stack failures do not extend primary REST backoff.
"""

from __future__ import annotations

import os
import json
import time
import logging
import tempfile
import threading
import contextlib
import subprocess
from typing import Any
from pathlib import Path
from datetime import datetime
from datetime import timezone
from collections import defaultdict
from dataclasses import dataclass
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx

from aoe_github_plugin import lookup
from aoe_github_plugin import graphql
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import NotFoundError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.handlers import _resolve_optional_token
from aoe_github_plugin.utils.gitctx import parse_owner_repo

_LOG = logging.getLogger(__name__)

GIT_TIMEOUT = 2.0
# Bounded fan-out pools (#70). Local git identity is subprocess-bound with no
# rate-limit interaction, so it parallelizes freely; the REST conditional
# probes are independent per key and 304s are free, but the pool stays small to
# keep well under GitHub's ~100-concurrent secondary-limit guidance. GraphQL
# chunks stay serial on purpose: post-#69 they are few and cheap, and they are
# the piece the secondary limits actually watch (#22).
GIT_WORKERS = 16
REST_PROBE_WORKERS = 6
# Back off proactively once the GraphQL point budget (5000/hr) is nearly spent,
# so a busy workspace degrades to stale data rather than hard rate-limit errors.
RATELIMIT_FLOOR = 50
# Freshness ceilings for the rich (GraphQL) cache, two-tier since #69. The REST
# conditional check gates GraphQL on a detected change, but the ``/pulls`` ETag
# does not reliably bump on a CI check completing or a review thread changing,
# so cached rich state is still revalidated on a ``304``, just via the cheap
# DIGEST query (~1 point per batched chunk) instead of the full rich query
# (~25-30 points per chunk, the #69 budget burn). Only a digest mismatch, a
# REST-detected change, a forced refresh, a cold cache, or the FULL ceiling
# spends the rich query.
GRAPHQL_DIGEST_STALE = 300.0
# Shorter digest ceiling for a branch whose cached rich state is ACTIVE: a CI
# check is running or queued (#26). Such state transitions in seconds and the
# ``/pulls`` ETag does not bump for it. A small floor (vs 0) still revalidates
# on every background tick while deduping sub-tick bursts. Scoped to CI on
# purpose: a PR merely awaiting review can sit idle for days, so polling it
# every tick would burn budget for no signal; it keeps the 300s gate.
GRAPHQL_ACTIVE_DIGEST_STALE = 30.0
# Hard ceilings on how long a FULL rich fetch can be deferred by matching
# digests. The digest is a heuristic, not a hash of everything the pane renders
# (a comment body edit or a per-check change under an unchanged rollup can slip
# past it), so a periodic full refresh bounds any false-negative staleness. An
# active branch gets a much shorter bound so its per-check rows stay honest
# while CI churns.
GRAPHQL_FULL_MAX_STALE = 7200.0
GRAPHQL_ACTIVE_FULL_MAX_STALE = 300.0
# Cooldown before retrying a key whose previous GraphQL attempt failed with a
# non-rate-limit error (digest or full). Without it, a persistently failing
# query (schema drift, scope error, malformed alias, a repo-level GraphQL error)
# becomes a new steady-state burn loop: stale, re-query, fail, repeat every tick.
# Fixed rather than exponential: worst case is one attempt per key per cooldown,
# already bounded.
GRAPHQL_RETRY_COOLDOWN = 300.0
# Budget pressure: once the last-seen GraphQL ``remaining`` drops below this,
# stretch the background ceilings by the multiplier so a shared token under
# load degrades to slower background freshness instead of racing to the
# RATELIMIT_FLOOR cliff. Never applies to forced, REST-changed, or cold keys.
BUDGET_PRESSURE_REMAINING = 1000
BUDGET_PRESSURE_MULTIPLIER = 4.0
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

RepoKey = lookup.PullKey
CheckoutKey = tuple[str, str, str, str, str]  # origin owner/repo, branch, configured upstream owner/repo
RichCacheKey = tuple[str, str, str, str, str, bool]
StackCacheKey = tuple[str, str, int]
StackRepoKey = tuple[str, str]


class _UnsupportedStackEndpointError(Exception):
    """The repository does not expose GitHub's native stack endpoint."""


@dataclass(frozen=True)
class SnapshotSettings:
    ignore_submodules: bool = True
    required_checks_only: bool = False
    ignored_commenters: frozenset[str] = frozenset()
    approved_overrides_stale_block: bool = False


DEFAULT_SNAPSHOT_SETTINGS = SnapshotSettings()

# Cross-refresh ETag cache and a single rate-limit backoff gate. The HTTP
# fan-out threads read/write both, so guard with a lock.
_cache_lock = threading.Lock()
_etag_cache: dict[RepoKey, dict[str, Any]] = {}  # key -> {"etag", "pulls"}
# Last-good rich (GraphQL) result per key: {"pulls", "fetched_at"}. Serves the
# TTL window and is the stale fallback while rate-limited.
_graphql_cache: dict[RichCacheKey, dict[str, Any]] = {}
# Last successful stack response per PR. The REST stack endpoint is separate
# from the conditional open-PR poll, so bound its refresh cadence independently.
_stack_cache: dict[StackCacheKey, dict[str, Any]] = {}
STACK_CACHE_TTL = 300.0
# Isolate the optional stack endpoint from primary open-PR polling.
STACK_RETRY_COOLDOWN = 300.0
_stack_backoff = {"until": 0.0}
# Some repositories do not support the native stack endpoint.
STACK_UNSUPPORTED_TTL = 3600.0
_stack_unsupported: dict[StackRepoKey, float] = {}
STACK_API_VERSION = "2026-03-10"
# Last successful full refresh per session id, scoped to the repo-key set so a
# changed workspace never inherits a timestamp from unrelated data.
_session_refresh_cache: dict[str, dict[str, Any]] = {}
# Rate-limit backoff, keyed by GitHub's two SEPARATE budgets (REST core, 5000
# req/hr; GraphQL, 5000 points/hr). Exhausting one must not gate the other, so a
# GraphQL backoff never blocks the cheap REST poll that discovers a new PR (#62).
# Each per-budget gate is a mutable holder (no `global` needed to update it):
#   until        - time.monotonic() seconds; that budget's HTTP is skipped until then.
#   reset_known  - True iff `until` came from a parsed GraphQL `resetAt` (so the
#                  countdown is real); False for the fixed REST fallback.
# ``notified`` is shared: one user-facing notice per continuous backoff WINDOW
# (the span where either gate is active), so a forced refresh announces at most once.
_BACKOFF_KINDS = ("rest", "graphql")
_backoff: dict[str, Any] = {
    "rest": {"until": 0.0, "reset_known": False},
    "graphql": {"until": 0.0, "reset_known": False},
    "notified": False,
}
# Last GraphQL budget observation, from any query's ``rateLimit`` payload (#69).
# ``valid_until`` is the window's reset as a monotonic deadline (or None when
# resetAt was unusable); past it the observation is discarded, since the budget
# refilled.
_graphql_budget: dict[str, Any] = {"remaining": None, "valid_until": None}
# Per-key cooldown deadline (monotonic) after a NON-rate GraphQL failure, keyed
# by ``RichCacheKey`` so it is independent of whether a rich cache entry exists
# (#71). A rate-limit trip already arms the shared backoff gate; this covers the
# other failure classes (schema drift, a repo-level GraphQL error, a malformed
# alias, an unexpected raise mid-parse) that would otherwise re-spend a query
# every background tick, at the DIGEST tier and for COLD keys too, both of which
# the old per-entry ``full_retry_at`` could not reach.
_retry_cooldown: dict[RichCacheKey, float] = {}


def _snapshot_cache_path() -> Path:
    """Where the last full snapshot is persisted for instant repaint on restart.
    A cache (not config/state), so it follows the XDG cache convention and stays
    decoupled from the host's own directories."""
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "agent-of-empires" / "github-plugin" / "snapshot.json"


def save_snapshot(snapshot: dict[str, Any]) -> None:
    """Persist a full snapshot so the next worker start can paint last-known data
    before its cold network refresh finishes. Fully fail-soft: a disk problem
    never affects a refresh. An empty snapshot is skipped so a transient
    "no sessions" blip cannot erase a useful cache. Written atomically (temp file
    in the same dir + ``os.replace``) so a crash mid-write cannot corrupt it."""
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        return
    with contextlib.suppress(Exception):
        path = _snapshot_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)


def load_snapshot() -> dict[str, Any] | None:
    """The persisted snapshot, or ``None`` if it is missing, corrupt, or the
    wrong shape. Never raises: a bad cache is simply ignored, and the normal
    refresh repopulates. Structural validation (plus the caller filtering by the
    live session list) is enough; no version envelope is needed."""
    with contextlib.suppress(Exception):
        with _snapshot_cache_path().open(encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data
    return None


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

    This remains the compatibility path when the host does not supply usable
    ``workspace_repos`` metadata.
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


def _identify(path: str, branch_override: str | None = None) -> tuple[str | None, CheckoutKey | None]:
    """Resolve the origin repo and branch, preferring the host's session branch.

    A non-GitHub origin is a benign skip; a known repo on detached HEAD has no
    branch key to query. Discovery-only checkouts read their branch from Git.
    """
    remote = _git(path, "remote", "get-url", "origin")
    owner_repo = parse_owner_repo(remote) if remote else None
    if owner_repo is None:
        return None, None
    owner, repo = owner_repo
    branch = branch_override or _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        return f"{owner}/{repo}", None
    remote = _git(path, "remote", "get-url", "upstream")
    upstream = parse_owner_repo(remote) if remote else None
    upstream_owner, upstream_name = upstream or ("", "")
    return f"{owner}/{repo}", (owner, repo, branch, upstream_owner, upstream_name)


def _session_checkouts(session: dict[str, Any], *, ignore_submodules: bool) -> list[tuple[str, str | None]]:
    """Return checkout/branch pairs, preferring usable host metadata. A ``None`` branch means "read live HEAD"."""
    workspace_repos = session.get("workspace_repos")
    if isinstance(workspace_repos, list) and workspace_repos:
        checkouts: list[tuple[str, str | None]] = []
        seen: set[tuple[str, str | None]] = set()
        for repo in workspace_repos:
            if not isinstance(repo, dict) or not isinstance(repo.get("source_path"), str) or not repo["source_path"]:
                continue
            # A host that sends ``worktree_path`` names the checkout the session works in: read its live
            # branch (the stored one is the branch at workspace creation). Older hosts keep the stored branch.
            worktree = repo.get("worktree_path")
            if isinstance(worktree, str) and worktree:
                checkout, branch = worktree, None
            else:
                checkout, branch = repo["source_path"], repo.get("branch")
                if not isinstance(branch, str) or not branch:
                    continue
            top = _git(checkout, "rev-parse", "--show-toplevel")
            if not top or os.path.realpath(top) != os.path.realpath(checkout):
                continue
            key = (os.path.realpath(checkout), branch)
            if key not in seen:
                seen.add(key)
                checkouts.append((checkout, branch))
        if checkouts:
            return checkouts
    path = session.get("project_path")
    if not (isinstance(path, str) and path):
        return []
    return [(checkout, None) for checkout in discover_checkouts(path, ignore_submodules=ignore_submodules)]


def _trim(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Basic REST pulls, retaining their actual head identity."""
    return [lookup.trim_pull(pr) for pr in raw if isinstance(pr, dict)]


def _stack_pull(raw: dict[str, Any], owner: str, repo: str) -> dict[str, Any] | None:
    """Trim one full REST PR response for stack navigation and readiness."""
    number = raw.get("number")
    head = raw.get("head")
    base = raw.get("base")
    mergeable_state = raw.get("mergeable_state")
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    return {
        "number": number,
        "url": raw.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}",
        "title": raw.get("title") or "",
        "state": raw.get("state"),
        "draft": bool(raw.get("draft")),
        "merged": raw.get("merged_at") is not None,
        "mergeable_state": mergeable_state if isinstance(mergeable_state, str) else None,
        "head": (head.get("ref") if isinstance(head, dict) else None),
        "base": (base.get("ref") if isinstance(base, dict) else None),
    }


def _stack_for_number(response: list[Any], number: int) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in response
            if isinstance(item, dict)
            and isinstance(item.get("pull_requests"), list)
            and any(isinstance(member, dict) and member.get("number") == number for member in item["pull_requests"])
        ),
        None,
    )


def _fetch_stack_members(client: GitHubClient, key: RepoKey, members: Any) -> list[dict[str, Any]] | None:
    if not isinstance(members, list) or not members:
        return None
    pulls: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict):
            return None
        number = member.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            return None
        raw = client.get_json(
            f"/repos/{key[0]}/{key[1]}/pulls/{number}",
            api_version=STACK_API_VERSION,
        )
        if not isinstance(raw, dict):
            return None
        trimmed = _stack_pull(raw, key[0], key[1])
        if trimmed is None:
            return None
        pulls.append(trimmed)
    return pulls


def _cache_stack(key: StackCacheKey, pulls: list[dict[str, Any]], fetched_at: float) -> None:
    member_numbers = {pull["number"] for pull in pulls}
    with _cache_lock:
        previous = _stack_cache.get(key)
        previous_pulls = previous.get("pulls") if previous is not None else None
        if isinstance(previous_pulls, list):
            for member in previous_pulls:
                old_key = (key[0], key[1], member["number"])
                entry = _stack_cache.get(old_key)
                if (
                    member["number"] not in member_numbers
                    and entry is not None
                    and entry.get("pulls") is previous_pulls
                ):
                    del _stack_cache[old_key]

        entry = {
            "pulls": pulls,
            "fetched_at": fetched_at,
            "retry_at": 0.0,
            "ttl": STACK_CACHE_TTL,
            "stale": False,
        }
        if pulls:
            for pull in pulls:
                _stack_cache[(key[0], key[1], pull["number"])] = entry
        else:
            _stack_cache[key] = entry


def _stale_stack(
    cache_key: StackCacheKey,
    cached: dict[str, Any] | None,
    now: float,
) -> list[dict[str, Any]] | None:
    if cached is None:
        with _cache_lock:
            cached = _stack_cache.setdefault(
                cache_key,
                {
                    "pulls": None,
                    "fetched_at": 0.0,
                    "retry_at": now + STACK_CACHE_TTL,
                    "ttl": STACK_CACHE_TTL,
                    "stale": False,
                },
            )
    pulls = cached["pulls"]
    with _cache_lock:
        cached["retry_at"] = now + STACK_CACHE_TTL
        if pulls is not None:
            cached["stale"] = True
            for member in pulls:
                member_key = (cache_key[0], cache_key[1], member["number"])
                entry = _stack_cache.get(member_key)
                if entry is not None and entry.get("pulls") is pulls:
                    entry["retry_at"] = now + STACK_CACHE_TTL
                    entry["stale"] = True
    return pulls


def _fetch_stack_with_state(
    client: GitHubClient,
    key: RepoKey,
    number: int,
    *,
    force: bool = False,
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Return stack data and whether it is stale fallback data."""
    cache_key: StackCacheKey = (key[0], key[1], number)
    now = time.monotonic()
    with _cache_lock:
        cached = _stack_cache.get(cache_key)
    shortcut = _stack_cache_shortcut(cache_key, cached, now)
    if shortcut is not None:
        return shortcut
    if cached is not None and not force and now - cached["fetched_at"] < cached.get("ttl", STACK_CACHE_TTL):
        return cached["pulls"], False

    try:
        pulls = _fetch_stack_response(client, key, number)
    except RateLimitedError:
        _set_stack_backoff(now)
        pulls = None
    except _UnsupportedStackEndpointError:
        with _cache_lock:
            _stack_unsupported[(key[0], key[1])] = now + STACK_UNSUPPORTED_TTL
        pulls = None
    except GitHubError:
        pulls = None
    if pulls is None:
        stale_pulls = _stale_stack(cache_key, cached, now)
        return stale_pulls, stale_pulls is not None
    _cache_stack(cache_key, pulls, now)
    return pulls, False


def _stack_cache_is_stale(cached: dict[str, Any], now: float) -> bool:
    return cached.get("stale", False) or now - cached["fetched_at"] >= cached.get("ttl", STACK_CACHE_TTL)


def _stack_cache_result(cached: dict[str, Any] | None, now: float) -> tuple[list[dict[str, Any]] | None, bool]:
    if cached is None:
        return None, False
    pulls = cached["pulls"]
    return pulls, pulls is not None and _stack_cache_is_stale(cached, now)


def _stack_cache_shortcut(
    cache_key: StackCacheKey,
    cached: dict[str, Any] | None,
    now: float,
) -> tuple[list[dict[str, Any]] | None, bool] | None:
    repo_key = (cache_key[0], cache_key[1])
    with _cache_lock:
        blocked = now < _backoff["rest"]["until"] or now < _stack_backoff["until"]
        unsupported_until = _stack_unsupported.get(repo_key)
        if unsupported_until is not None and unsupported_until <= now:
            del _stack_unsupported[repo_key]
            unsupported_until = None
    if blocked:
        return _stack_cache_result(cached, now)
    if unsupported_until is not None:
        if cached is not None and not _stack_cache_is_stale(cached, now):
            return cached["pulls"], False
        stale_pulls = _stale_stack(cache_key, cached, now)
        return stale_pulls, stale_pulls is not None
    if cached is not None and now < cached.get("retry_at", 0.0):
        return _stack_cache_result(cached, now)
    return None


def _set_stack_backoff(now: float) -> None:
    with _cache_lock:
        _stack_backoff["until"] = max(_stack_backoff["until"], now + STACK_RETRY_COOLDOWN)


def _fetch_stack_response(client: GitHubClient, key: RepoKey, number: int) -> list[dict[str, Any]] | None:
    try:
        response = client.get_json(
            f"/repos/{key[0]}/{key[1]}/stacks",
            {"pull_request": str(number)},
            api_version=STACK_API_VERSION,
        )
    except NotFoundError as exc:
        raise _UnsupportedStackEndpointError from exc
    if not isinstance(response, list):
        return None
    stack = _stack_for_number(response, number)
    if stack is None:
        return []
    return _fetch_stack_members(client, key, stack.get("pull_requests"))


def _fetch_stack(
    client: GitHubClient,
    key: RepoKey,
    number: int,
    *,
    force: bool = False,
) -> list[dict[str, Any]] | None:
    pulls, _stale = _fetch_stack_with_state(client, key, number, force=force)
    return pulls


def _attach_stack_members(
    stack: list[dict[str, Any]],
    pulls: list[dict[str, Any]],
    pulls_by_number: dict[int, dict[str, Any]],
) -> None:
    for member in stack:
        existing = pulls_by_number.get(member["number"])
        if existing is None:
            existing = dict(member, stack=stack, stack_only=True)
            pulls.append(existing)
            pulls_by_number[member["number"]] = existing
        else:
            existing["stack"] = stack
            existing["mergeable_state"] = member["mergeable_state"]


def _fetch_stack_for_pull(
    client: GitHubClient,
    key: RepoKey,
    pull: dict[str, Any],
    refreshed: set[StackCacheKey],
    *,
    force: bool,
) -> tuple[list[dict[str, Any]] | None, bool]:
    number = pull.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return None, False
    cache_key = (key[0], key[1], number)
    stack, stale = _fetch_stack_with_state(
        client,
        key,
        number,
        force=force and cache_key not in refreshed,
    )
    if stack:
        refreshed.update((key[0], key[1], member["number"]) for member in stack)
    return stack, stale


def _attach_stack_for_pull(
    pull: dict[str, Any],
    stack: list[dict[str, Any]] | None,
    pulls: list[dict[str, Any]],
    pulls_by_number: dict[int, dict[str, Any]],
) -> None:
    if stack is None:
        return
    if not stack:
        pull["stack"] = []
        return
    _attach_stack_members(stack, pulls, pulls_by_number)


def _attach_stacks(
    client: GitHubClient,
    keys: list[RepoKey],
    results: dict[RepoKey, dict[str, Any]],
    *,
    force: bool,
) -> None:
    refreshed: set[StackCacheKey] = set()
    for key in keys:
        result = results.get(key)
        if not isinstance(result, dict):
            continue
        raw_pulls = result.get("pulls")
        if not isinstance(raw_pulls, list):
            continue
        pulls = [dict(pull) for pull in raw_pulls if isinstance(pull, dict)]
        result["pulls"] = pulls
        pulls_by_number = {
            pull["number"]: pull
            for pull in pulls
            if isinstance(pull, dict)
            and isinstance(pull.get("number"), int)
            and not isinstance(pull.get("number"), bool)
        }
        for pull in list(pulls):
            stack, stale = _fetch_stack_for_pull(client, key, pull, refreshed, force=force)
            if stale:
                result["_stale"] = True
            _attach_stack_for_pull(pull, stack, pulls, pulls_by_number)


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


def _set_backoff(api: str, reset_at: Any = None) -> None:
    """Arm one budget's backoff gate (``api`` is ``"rest"`` or ``"graphql"``).
    Prefers the GraphQL response's ``resetAt`` (so we wait exactly until the
    budget refills); falls back to a fixed window for the REST path, which has no
    usable reset.

    A fresh WINDOW (both gates already expired) clears the shared ``notified``
    flag so the next user-initiated refresh announces it once; an overlapping trip
    of the other budget never re-opens the notice. Re-arming an already-active
    gate only extends the deadline and upgrades ``reset_known``; it never shortens
    the wait or downgrades a real reset to the fallback (the fan-out calls this
    repeatedly per window)."""
    until = _reset_to_monotonic(reset_at)
    now = time.monotonic()
    new_until = until if until is not None else now + BACKOFF_SECS
    new_known = until is not None
    with _cache_lock:
        # Fresh window only when neither budget is currently backed off.
        if all(now >= _backoff[k]["until"] for k in _BACKOFF_KINDS):
            _backoff["notified"] = False
        gate = _backoff[api]
        if new_until > gate["until"]:  # arm or extend; never shorten
            gate["until"] = new_until
            gate["reset_known"] = new_known
        elif new_until == gate["until"]:
            gate["reset_known"] = gate["reset_known"] or new_known


def _active_backoff_locked(now: float) -> dict[str, Any] | None:
    """The active gate that stays limited LONGEST (the point the whole plugin is
    clear again), as ``{"seconds", "reset_known", "budget"}``, or ``None`` when
    neither budget is backed off. ``budget`` is ``"mixed"`` when both are active.
    Caller must hold ``_cache_lock``. ``reset_known`` is that one gate's flag, not
    an OR: a known 20s GraphQL reset behind an unknown 60s REST fallback is still
    an unknown overall clear time."""
    active = [(k, _backoff[k]) for k in _BACKOFF_KINDS if now < _backoff[k]["until"]]
    if not active:
        return None
    _kind, gate = max(active, key=lambda item: item[1]["until"])
    budget = active[0][0] if len(active) == 1 else "mixed"
    return {"seconds": gate["until"] - now, "reset_known": bool(gate["reset_known"]), "budget": budget}


def _rate_limit_status() -> dict[str, Any] | None:
    """Non-consuming snapshot of the current backoff, for the always-on pane note
    (#62). Unlike ``_consume_rate_limit_notice`` this never touches ``notified``,
    so the background pane can reflect the degraded state without spending the
    forced-refresh toast's one-shot."""
    with _cache_lock:
        return _active_backoff_locked(time.monotonic())


def _consume_rate_limit_notice() -> dict[str, Any] | None:
    """Claim this backoff window's one user-facing notice. Returns
    ``{"seconds", "reset_known", "budget"}`` the first time it is called while a
    backoff is active and unannounced (then marks the window announced), else
    ``None``. Only a user-initiated (forced) refresh consumes it; background ticks
    never do, so a rate-limited workspace is not nagged on every poll."""
    now = time.monotonic()
    with _cache_lock:
        status = _active_backoff_locked(now)
        if status is None or _backoff["notified"]:
            return None
        _backoff["notified"] = True
        return status


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
    owner, repo, branch, head_owner, _ = key
    with _cache_lock:
        cached = _etag_cache.get(key)
        blocked = time.monotonic() < _backoff["rest"]["until"]
    if blocked:
        if cached is not None:
            return False, cached["pulls"], False
        raise RateLimitedError
    etag = cached["etag"] if cached else None
    params = {"state": "open", "head": f"{head_owner}:{branch}", "per_page": "100"}
    try:
        status, new_etag, raw = client.get_json_conditional(f"/repos/{owner}/{repo}/pulls", params, etag)
    except RateLimitedError:
        _set_backoff("rest")
        if cached is not None:
            return False, cached["pulls"], False
        raise
    if status == 304 and cached is not None:
        return False, cached["pulls"], True
    pulls = _trim([pr for pr in raw or [] if isinstance(pr, dict) and lookup.matches_head(pr.get("head"), key)])
    with _cache_lock:
        _etag_cache[key] = {"etag": new_etag, "pulls": pulls}
    return True, pulls, True


def _fetch_key(client: GitHubClient, key: RepoKey) -> tuple[list[dict[str, Any]], bool]:
    """Open PRs for one ``(owner, repo, branch)`` via the basic REST path (no
    token). The conditional probe is the whole job here; the change flag only
    matters on the token path, which gates GraphQL on it."""
    _, pulls, fresh = _rest_probe(client, key)
    return pulls, fresh


def _probe_all(
    client: GitHubClient, keys: list[RepoKey]
) -> dict[RepoKey, tuple[bool, list[dict[str, Any]], bool] | GitHubError]:
    """Run the REST conditional probe for every key on a small bounded pool
    (#70). The probes are independent per key; the ETag cache and the backoff
    gates are lock-guarded, and a rate-limit trip mid-pool arms the shared gate
    so the remaining probes fall back to cache without further requests. A
    per-key ``GitHubError`` is returned as a value so the caller keeps its
    fail-soft per-key handling."""

    def probe(key: RepoKey) -> tuple[bool, list[dict[str, Any]], bool] | GitHubError:
        try:
            return _rest_probe(client, key)
        except GitHubError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=REST_PROBE_WORKERS) as pool:
        return dict(zip(keys, pool.map(probe, keys), strict=True))


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
    return (*key, required_checks_only)


def _is_active(pulls: list[dict[str, Any]]) -> bool:
    """A branch is ACTIVE when a non-merged PR has running CI or is in a merge
    queue. Those transitions can happen without the REST pull-list ETag changing,
    so they earn the shorter staleness ceiling."""
    for pull in pulls:
        if pull.get("merged"):
            continue
        if pull.get("merge_queue") is not None:
            return True
        checks = pull.get("checks")
        if not isinstance(checks, dict):
            continue
        if checks.get("state") in ("running", "queued"):
            return True
        if any(isinstance(run, dict) and run.get("state") in ("running", "queued") for run in checks.get("runs") or []):
            return True
    return False


def _observe_rate_limit(rate_info: dict[str, Any], *, kind: str, repo: str, aliases: int) -> None:
    """Record a query's ``rateLimit`` payload: log the spend (the #69 telemetry
    gap; ``cost`` used to be selected and thrown away) and remember ``remaining``
    for budget-pressure pacing, valid until the window resets."""
    cost = rate_info.get("cost")
    remaining = rate_info.get("remaining")
    _LOG.info("graphql kind=%s repo=%s aliases=%d cost=%s remaining=%s", kind, repo, aliases, cost, remaining)
    if not isinstance(remaining, int):
        return
    with _cache_lock:
        _graphql_budget["remaining"] = remaining
        _graphql_budget["valid_until"] = _reset_to_monotonic(rate_info.get("resetAt"))


def _budget_multiplier(now: float) -> float:
    """Ceiling stretch under budget pressure: ``BUDGET_PRESSURE_MULTIPLIER`` when
    the last-seen GraphQL ``remaining`` (still within its window) is low, else 1.
    An observation whose window has reset is discarded; the budget refilled."""
    with _cache_lock:
        remaining = _graphql_budget["remaining"]
        valid_until = _graphql_budget["valid_until"]
        if remaining is None:
            return 1.0
        if valid_until is not None and now >= valid_until:
            _graphql_budget["remaining"] = None
            _graphql_budget["valid_until"] = None
            return 1.0
    return BUDGET_PRESSURE_MULTIPLIER if remaining < BUDGET_PRESSURE_REMAINING else 1.0


def _digest_stale(rich: dict[str, Any], now: float) -> bool:
    """Whether the cached rich result wants digest revalidation: aged past the
    short active ceiling (#26) or the 300s one since it was last fetched OR
    digest-validated, stretched under budget pressure."""
    ceiling = GRAPHQL_ACTIVE_DIGEST_STALE if _is_active(rich["pulls"]) else GRAPHQL_DIGEST_STALE
    validated = rich.get("validated_at", rich["fetched_at"])
    return (now - validated) >= ceiling * _budget_multiplier(now)


def _full_stale(rich: dict[str, Any], now: float) -> bool:
    """Whether the cached rich result is past the HARD full-fetch ceiling: only a
    real full query resets this clock, so digest false negatives (fields the
    signature cannot see) are bounded rather than permanent."""
    ceiling = GRAPHQL_ACTIVE_FULL_MAX_STALE if _is_active(rich["pulls"]) else GRAPHQL_FULL_MAX_STALE
    return (now - rich["fetched_at"]) >= ceiling * _budget_multiplier(now)


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
                if time.monotonic() < _backoff["graphql"]["until"]:
                    return  # the GraphQL gate tripped (e.g. the batched query armed it); stop here
            data = client.post_graphql(graphql.THREADS_PAGE_QUERY, {"id": node_id, "cursor": cursor})
            _observe_rate_limit(((data.get("data") or {}).get("rateLimit") or {}), kind="threads", repo="-", aliases=1)
            if _graphql_rate_limited(data):
                _set_backoff("graphql", ((data.get("data") or {}).get("rateLimit") or {}).get("resetAt"))
                return
            conn = ((data.get("data") or {}).get("node") or {}).get("reviewThreads") or {}
            more = [n for n in (conn.get("nodes") or []) if isinstance(n, dict)]
            if not more:
                break
            nodes.extend(more[: MAX_REVIEW_THREADS - len(nodes)])  # never overshoot the cap on a full last page
            page = conn.get("pageInfo") or {}
            cursor = page.get("endCursor")
    except RateLimitedError:
        _set_backoff("graphql")
        return
    except GitHubError:
        return


def _fallback_pulls(pending: dict[str, Any]) -> dict[str, Any]:
    """Per-key result when GraphQL could not produce fresh data: the last-good
    rich cache if present, else the basic REST open-PR pulls. Never an error and
    never accidentally empty, preserving the fail-soft per-key contract.

    When the REST probe reported the open-PR list CHANGED (#33), the cached rich
    result is structurally stale (it can miss a just-opened PR or show a closed
    one), so serve the fresh basic REST pulls instead. Those lack the rich
    CI/review fields but reflect the current PR set; the next successful GraphQL
    tick restores the rich shape. Without this, a GraphQL rate limit can hide a
    PR that REST already discovered (#62)."""
    rich = pending["rich"]
    if rich is not None and not pending.get("changed"):
        return _pulls_result(rich["pulls"], fresh=False, stale=True)
    return _pulls_result(pending["basic"], fresh=False, stale=True)


def _chunk_fallback(
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    out: dict[RepoKey, dict[str, Any]],
) -> None:
    """Fall back every key in a chunk (blocked gate or a whole-query failure)."""
    for key in chunk:
        out[key] = _fallback_pulls(pending[key])


def _arm_retry_cooldown(keys: list[RepoKey], pending: dict[RepoKey, dict[str, Any]], now: float) -> None:
    """Cooldown the failed keys' next attempt after a NON-rate failure (#69/#71),
    keyed by ``cache_key`` so it holds whether or not a rich cache exists (a cold
    key that keeps failing its full query, a digest that keeps failing) and
    covers both tiers. A rate-limit trip is handled by the shared backoff gate
    instead and never lands here."""
    with _cache_lock:
        for key in keys:
            _retry_cooldown[pending[key]["cache_key"]] = now + GRAPHQL_RETRY_COOLDOWN


def _resolve_null_repository(
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    data: dict[str, Any],
    out: dict[RepoKey, dict[str, Any]],
) -> None:
    """Per-key result when the batched response carried no ``repository``: prefer
    the last-good rich cache (unless REST saw the list change, #33, in which case
    serve the fresh basic pulls); a GraphQL error with no cache degrades to the
    basic REST pulls (never blanks a PR over a transient failure); a genuinely
    null repo (no errors) is an empty result, matching the prior single-key
    behavior."""
    errored = bool(data.get("errors"))
    if errored:
        # A repo-level error (renamed/deleted repo, transient server error) would
        # otherwise re-fire the full query every tick; cool the chunk down (#71).
        _arm_retry_cooldown(chunk, pending, time.monotonic())
    for key in chunk:
        rich = pending[key]["rich"]
        if rich is not None:
            out[key] = _fallback_pulls(pending[key])
        elif errored:
            out[key] = _pulls_result(pending[key]["basic"], fresh=False, stale=True)
        else:
            out[key] = _pulls_result([], fresh=True)


def _owned_connection(key: RepoKey, conn: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Filter one queried page down to the checkout's own PRs.

    Returns ``(truncated, owned_conn)``. ``truncated`` is True when the source
    connection still has more same-name PRs than the single page fetched, so the
    page cannot prove the checkout's PR is absent. The caller then serves the
    owner-scoped REST list instead of caching a false empty, and never arms the
    per-key cooldown (a collision is not a failure to retry later). No follow-up
    GraphQL paging: full queries per page would burn the shared point budget on
    busy branch names, including same-repo ``main``.
    """
    owned = lookup.filter_connection(conn, key)
    truncated = bool((conn.get("pageInfo") or {}).get("hasNextPage"))
    return truncated, owned


def _apply_aliases(
    client: GitHubClient,
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    data: dict[str, Any],
    *,
    required_checks_only: bool,
) -> dict[RepoKey, dict[str, Any]]:
    """Normalize each alias connection back into its key's cache + result. A failed
    (named in ``errors[].path``) or malformed alias falls back per key (and arms
    that key's retry cooldown, #69/#71), never poisoning its siblings. A good
    alias clears any prior cooldown. ``data`` is the full envelope (its
    ``repository`` is a dict here, validated by the caller)."""
    repository = (data.get("data") or {}).get("repository") or {}
    failed = _error_aliases(data)
    now = time.monotonic()
    out: dict[RepoKey, dict[str, Any]] = {}
    for i, key in enumerate(chunk):
        conn = repository.get(f"b{i}")
        if f"b{i}" in failed or not isinstance(conn, dict):
            _arm_retry_cooldown([key], pending, now)
            out[key] = _fallback_pulls(pending[key])
            continue
        truncated, conn = _owned_connection(key, conn)
        if truncated:
            # The single page was exhausted by foreign same-name PRs, so it
            # cannot prove the checkout's own PR is absent. The owner-scoped
            # REST probe already lists it; serve that fresh rather than caching
            # a false empty, and do not arm the cooldown (this is not a failure).
            out[key] = _pulls_result(pending[key]["basic"], fresh=True)
            continue
        # Compute the ownership-filtered signature before thread pagination.
        digest = graphql.digest_signature(conn)
        for node in conn.get("nodes") or []:
            if isinstance(node, dict):
                _paginate_threads(client, node)
        pulls = graphql.normalize_connection(conn, required_checks_only=required_checks_only)
        cache_key = _rich_cache_key(key, required_checks_only=required_checks_only)
        with _cache_lock:
            _graphql_cache[cache_key] = {
                "pulls": pulls,
                "digest": digest,
                "fetched_at": now,
                "validated_at": now,
            }
            _retry_cooldown.pop(cache_key, None)
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
    owner, repo = chunk[0][:2]
    with _cache_lock:
        blocked = time.monotonic() < _backoff["graphql"]["until"]
    if blocked:
        _chunk_fallback(chunk, pending, out)
        return

    variables: dict[str, Any] = {"owner": owner, "repo": repo}
    for i, key in enumerate(chunk):
        variables[f"b{i}"] = key[2]
    try:
        data = client.post_graphql(graphql.build_query(len(chunk)), variables)
    except RateLimitedError:
        _set_backoff("graphql")
        _chunk_fallback(chunk, pending, out)
        return
    except GitHubError:
        # A non-rate failure arms the per-key cooldown, not the global gate: the
        # budget is fine, this query is not, so retrying it every tick would be
        # the #69 burn loop in a new shape.
        _arm_retry_cooldown(chunk, pending, time.monotonic())
        _chunk_fallback(chunk, pending, out)
        return

    payload = data.get("data") or {}
    rate_info = payload.get("rateLimit") or {}
    repository = payload.get("repository")
    _observe_rate_limit(rate_info, kind="full", repo=f"{owner}/{repo}", aliases=len(chunk))
    # Arm backoff for the next refresh on a riding rate-limit error or a low
    # remaining budget, after caching whatever good data this response carries.
    remaining = rate_info.get("remaining")
    if _graphql_rate_limited(data) or (isinstance(remaining, int) and remaining < RATELIMIT_FLOOR):
        _set_backoff("graphql", rate_info.get("resetAt"))

    if not isinstance(repository, dict):
        _resolve_null_repository(chunk, pending, data, out)
        return
    out.update(_apply_aliases(client, chunk, pending, data, required_checks_only=required_checks_only))


def _apply_digest_aliases(
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    data: dict[str, Any],
    out: dict[RepoKey, dict[str, Any]],
) -> list[RepoKey]:
    """Compare each alias's digest signature against its cached one. A match
    revalidates the rich cache (bumps ``validated_at``, serves it fresh, like a
    304); a failed or malformed alias serves the stale cache. Returns the
    mismatched keys, which the caller promotes to the full query."""
    repository = (data.get("data") or {}).get("repository") or {}
    failed = _error_aliases(data)
    now = time.monotonic()
    promoted: list[RepoKey] = []
    for i, key in enumerate(chunk):
        conn = repository.get(f"b{i}")
        signature = None
        if f"b{i}" not in failed and isinstance(conn, dict):
            truncated, owned = _owned_connection(key, conn)
            if truncated:
                # Cannot verify the owned set from a truncated page: promote to
                # the full query (which will itself fall back to REST if also
                # truncated). Serve the fail-soft cache-shaped result meanwhile
                # so the key is never left unset if the promoted fetch declines
                # to write. The digest proved nothing, so it must report stale,
                # never fresh. A collision is not a failure, so no cooldown.
                out[key] = _fallback_pulls(pending[key])
                promoted.append(key)
                continue
            signature = graphql.digest_signature(owned)
        rich = pending[key]["rich"]  # never None on the digest path (classification requires a cache)
        if signature is None:
            # A failed or malformed digest alias arms the cooldown so it does not
            # re-query every tick (#71); the rich cache still serves meanwhile.
            _arm_retry_cooldown([key], pending, now)
            out[key] = _fallback_pulls(pending[key])
        elif signature == rich.get("digest"):
            with _cache_lock:
                rich["validated_at"] = now
            out[key] = _pulls_result(rich["pulls"], fresh=True)
        else:
            promoted.append(key)
    return promoted


def _fetch_digest_chunk(
    client: GitHubClient,
    chunk: list[RepoKey],
    pending: dict[RepoKey, dict[str, Any]],
    out: dict[RepoKey, dict[str, Any]],
    full_pending: dict[RepoKey, dict[str, Any]],
) -> None:
    """One batched DIGEST query (#69) for up to ``MAX_GRAPHQL_ALIASES`` branches
    of one repo. A matching signature revalidates that key's rich cache; a
    mismatch promotes the key into ``full_pending`` for the rich query. Failures
    (blocked gate, rate limit, malformed alias) serve the stale cache; they
    never promote to full, so a flaky digest cannot burn full-query points."""
    owner, repo = chunk[0][:2]
    with _cache_lock:
        blocked = time.monotonic() < _backoff["graphql"]["until"]
    if blocked:
        _chunk_fallback(chunk, pending, out)
        return

    variables: dict[str, Any] = {"owner": owner, "repo": repo}
    for i, key in enumerate(chunk):
        variables[f"b{i}"] = key[2]
    try:
        data = client.post_graphql(graphql.build_digest_query(len(chunk)), variables)
    except RateLimitedError:
        _set_backoff("graphql")
        _chunk_fallback(chunk, pending, out)
        return
    except GitHubError:
        # Non-rate failure: cool the chunk down so it does not re-digest every
        # tick (#71). The rich cache still serves meanwhile.
        _arm_retry_cooldown(chunk, pending, time.monotonic())
        _chunk_fallback(chunk, pending, out)
        return

    payload = data.get("data") or {}
    rate_info = payload.get("rateLimit") or {}
    _observe_rate_limit(rate_info, kind="digest", repo=f"{owner}/{repo}", aliases=len(chunk))
    remaining = rate_info.get("remaining")
    if _graphql_rate_limited(data) or (isinstance(remaining, int) and remaining < RATELIMIT_FLOOR):
        _set_backoff("graphql", rate_info.get("resetAt"))
    if _graphql_rate_limited(data):
        _chunk_fallback(chunk, pending, out)  # backoff gate handles the retry cadence
        return
    if not isinstance(payload.get("repository"), dict):
        _arm_retry_cooldown(chunk, pending, time.monotonic())
        _chunk_fallback(chunk, pending, out)
        return

    for key in _apply_digest_aliases(chunk, pending, data, out):
        full_pending[key] = pending[key]


def _run_chunked(
    pending: dict[RepoKey, dict[str, Any]],
    run: Callable[[list[RepoKey]], None],
    out: dict[RepoKey, dict[str, Any]],
) -> None:
    """Group ``pending`` by ``(owner, repo)``, chunk to ``MAX_GRAPHQL_ALIASES``
    (#25), and run each chunk fail-soft: an unexpected error degrades its own
    chunk's keys, never the whole refresh."""
    groups: dict[tuple[str, str], list[RepoKey]] = defaultdict(list)
    for key in pending:
        groups[(key[0], key[1])].append(key)
    for gkeys in groups.values():
        gkeys.sort()
        for start in range(0, len(gkeys), MAX_GRAPHQL_ALIASES):
            chunk = gkeys[start : start + MAX_GRAPHQL_ALIASES]
            try:
                run(chunk)
            except Exception as exc:  # noqa: BLE001 - fail-soft per repo, never abort the refresh
                # An unexpected raise mid-parse would otherwise re-run and re-crash
                # every tick; cool the chunk down so a deterministic bug degrades
                # to periodic retries, not a per-tick burn (#71).
                _arm_retry_cooldown(chunk, pending, time.monotonic())
                for key in chunk:
                    out.setdefault(
                        key,
                        {
                            "error": {"kind": "internal", "hint": f"refresh failed: {exc}"},
                            "_fresh": False,
                            "_stale": True,
                        },
                    )


def _fetch_rich(
    client: GitHubClient, keys: list[RepoKey], *, force: bool, required_checks_only: bool
) -> dict[RepoKey, dict[str, Any]]:
    """Token path: a cheap REST conditional probe gates each key (#21), then a
    two-tier GraphQL pass (#69). Keys that are cold, forced, or REST-changed go
    straight to the full rich query; a key whose cache merely aged past its
    digest ceiling is revalidated by the cheap digest query first and only a
    signature mismatch (or the hard full ceiling) spends the rich query. Both
    tiers batch same-repo branches into aliased queries chunked to
    ``MAX_GRAPHQL_ALIASES`` (#25). Gating is PER KEY before grouping, so one
    active branch never drags its quiescent siblings into a fetch. Fail-soft per
    key: a probe error or GraphQL failure serves the rich cache, then basic REST
    pulls, then a typed error."""
    now = time.monotonic()
    out: dict[RepoKey, dict[str, Any]] = {}
    full_pending: dict[RepoKey, dict[str, Any]] = {}
    digest_pending: dict[RepoKey, dict[str, Any]] = {}
    probes = _probe_all(client, keys)
    for key in keys:
        cache_key = _rich_cache_key(key, required_checks_only=required_checks_only)
        with _cache_lock:
            rich = _graphql_cache.get(cache_key)
        probe = probes[key]
        if isinstance(probe, GitHubError):
            stale = _pulls_result(rich["pulls"], fresh=False, stale=True) if rich is not None else None
            out[key] = stale if stale is not None else _error_entry(probe)
            continue
        changed, basic, _fresh = probe
        entry = {"basic": basic, "rich": rich, "changed": changed, "cache_key": cache_key}
        with _cache_lock:
            cooling = now < _retry_cooldown.get(cache_key, 0.0)
        if not force and cooling:
            # A recent non-rate failure for this key: serve what we have and
            # spend nothing until the cooldown lapses (#71). A forced refresh
            # bypasses it, since the user asked for a live attempt.
            out[key] = _fallback_pulls(entry)
        elif rich is None or force or changed or rich.get("digest") is None or _full_stale(rich, now):
            full_pending[key] = entry
        elif _digest_stale(rich, now):
            digest_pending[key] = entry
        else:
            # ``_fresh`` distinguishes a real 304 (GitHub confirmed the data is
            # current) from a backoff-served cache (GitHub was never contacted).
            # Mark the latter stale so it does not advance the freshness stamp.
            out[key] = _pulls_result(rich["pulls"], fresh=False, stale=not _fresh)

    # Digest tier first: mismatches promote into full_pending, so the full tier
    # below covers them in the same refresh.
    _run_chunked(
        digest_pending,
        lambda chunk: _fetch_digest_chunk(client, chunk, digest_pending, out, full_pending),
        out,
    )
    _run_chunked(
        full_pending,
        lambda chunk: _fetch_chunk(client, chunk, full_pending, out, required_checks_only=required_checks_only),
        out,
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
    """Freshness metadata for one session, conservative on partial failure.

    The stamp advances on any refresh that confirmed the displayed data is
    current, not only a fresh fetch. A successful conditional probe (304)
    validates the cache, so it counts: otherwise the stamp would look frozen
    during normal background polling and only move on a manual refresh. A
    stale fallback (backoff, error) keeps the last successful stamp. Each per-key
    result already carries its own ``_stale``/``error`` verdict (a backoff-served
    cache is marked stale at the source in ``_fetch_rich``), so a plain
    ``not stale`` gate is enough; the old ``not pulls`` heuristic wrongly flagged
    a confirmed-current repo with zero open PRs as stale."""
    if not isinstance(session_id, str) or not fingerprint:
        return None
    stale = any(result.get("_stale") is True or result.get("error") is not None for result in results)
    with _cache_lock:
        cached = _session_refresh_cache.get(session_id)
        if not stale:
            _session_refresh_cache[session_id] = {"fingerprint": fingerprint, "refreshed_at": refreshed_at}
            return {"refreshed_at": refreshed_at, "stale": False}
        if cached and cached.get("fingerprint") == fingerprint:
            return {"refreshed_at": cached["refreshed_at"], "stale": True}
    return None


def _fetch_all(
    keys: set[CheckoutKey],
    env: TokenEnvironment | None,
    transport: httpx.BaseTransport | None,
    *,
    force: bool = False,
    required_checks_only: bool = False,
) -> tuple[dict[CheckoutKey, dict[str, Any]], bool]:
    """Fetch every unique key. Returns ``(results, token_present)`` where results
    is ``key -> {"pulls": [...]}`` or ``key -> {"error": {...}}``. With a token,
    keys go through the REST-conditional-gated rich path and same-repo branches
    that need GraphQL share one batched query (``_fetch_rich``); without one we
    keep the basic REST open-PR path and report ``token_present=False`` so the UI
    can show the "token needed" banner. ``force`` (a manual refresh) bypasses the
    staleness gate on the token path. REST probes fan out on a small bounded
    pool (#70); GraphQL is batched per repo and stays serialized (sorted for
    determinism), to stay under GitHub's secondary/concurrency limits (#22);
    none of this touches stdin or host RPCs."""
    if not keys:
        return {}, True
    token = _resolve_optional_token(env)
    present = token is not None
    by_checkout: dict[CheckoutKey, list[RepoKey]] = {}
    with GitHubClient(token=token, transport=transport) as client:
        for key in sorted(keys):
            with _cache_lock:
                metadata_allowed = time.monotonic() >= _backoff["rest"]["until"]
            upstream = (key[3], key[4]) if key[3] else None
            repos = lookup.repositories(client, (key[0], key[1]), upstream, metadata_allowed=metadata_allowed)
            by_checkout[key] = lookup.pull_keys(repos, key[2])
        ordered = sorted({candidate for candidates in by_checkout.values() for candidate in candidates})
        if present:
            fetched = _fetch_rich(client, ordered, force=force, required_checks_only=required_checks_only)
            _attach_stacks(client, ordered, fetched, force=force)
        else:
            with ThreadPoolExecutor(max_workers=REST_PROBE_WORKERS) as pool:
                fetched = dict(zip(ordered, pool.map(lambda key: _fetch_one_basic(client, key), ordered), strict=True))
    results: dict[CheckoutKey, dict[str, Any]] = {}
    for key, candidates in by_checkout.items():
        # The origin candidate (destination == head == checkout origin) is
        # authoritative for freshness and error. An optional upstream that
        # fails must never degrade a healthy origin result; it only adds
        # visibility on top of what origin confirms.
        origin = fetched[candidates[0]]
        pulls = {}
        for candidate in candidates:
            for pull in fetched[candidate].get("pulls", []):
                pulls[pull["url"]] = pull
        result = _pulls_result(
            list(pulls.values()),
            fresh=not origin.get("_stale"),
            stale=bool(origin.get("_stale")),
        )
        if origin.get("error"):
            result["error"] = origin["error"]
        results[key] = result
    return results, present


def build_snapshot(
    sessions: list[dict[str, Any]],
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
    *,
    force: bool = False,
    settings: SnapshotSettings = DEFAULT_SNAPSHOT_SETTINGS,
) -> dict[str, Any]:
    """Assemble the host-session snapshot consumed by the UI mapper.

    Returns ``{"sessions": [{session_id, title, project_path, repos: [...]}],
    "auth": {"present": bool}}``; each repo is
    ``{path, name, repo, branch, pulls, error}``. Token-backed pulls include
    rich review/CI/comment state; without a token they use the basic REST shape
    and ``auth.present`` is false. An optional ``freshness`` record summarizes
    cached data, and ``rate_limit`` reports current backoff state.

    Host ``workspace_repos`` paths and branches are authoritative when usable;
    otherwise shallow checkout discovery under ``project_path`` supplies the
    compatibility path. Discovery and Git identity use bounded local pools
    (#70), while GraphQL remains serialized to avoid secondary limits.
    ``force`` bypasses the GraphQL TTL; a forced rate-limited refresh adds a
    one-shot ``rate_limit_notice`` (``{"seconds", "reset_known"}``) for the main
    loop to surface (issue #20). The always-on rate-limit state (#62) does not
    consume that notice. This function performs no host-channel I/O; ``env`` and
    ``transport`` are test seams.
    """

    def _discover(session: dict[str, Any]) -> list[tuple[str, str | None]]:
        return _session_checkouts(session, ignore_submodules=settings.ignore_submodules)

    with ThreadPoolExecutor(max_workers=GIT_WORKERS) as pool:
        per_session = list(zip(sessions, pool.map(_discover, sessions), strict=True))
        reps: dict[tuple[str, str | None], str] = {}
        for _, checkouts in per_session:
            for checkout, branch in checkouts:
                reps.setdefault((os.path.realpath(checkout), branch), checkout)
        ident: dict[tuple[str, str | None], tuple[str | None, CheckoutKey | None]] = dict(
            zip(reps.keys(), pool.map(lambda item: _identify(item[1], item[0][1]), reps.items()), strict=True)
        )
    keys: set[CheckoutKey] = {key for _, key in ident.values() if key is not None}
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
        for checkout, branch in checkouts:
            repo_str, key = ident[(os.path.realpath(checkout), branch)]
            entry: dict[str, Any] = {
                "path": checkout,
                "name": Path(checkout).name or checkout,
                "repo": repo_str,
                "branch": key[2] if key else branch,
                "pulls": [],
                "error": None,
            }
            if key is not None:
                session_keys.append(key)
                result = fetched.get(key, {})
                pulls = [
                    graphql.drop_ignored_commenters(p, settings.ignored_commenters) for p in result.get("pulls", [])
                ]
                if settings.approved_overrides_stale_block:
                    pulls = [graphql.approve_over_stale_block(p) for p in pulls]
                entry["pulls"] = pulls
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
    # ``rate_limit`` is the always-on counterpart (#62): the current backoff state
    # regardless of force, so the pane can surface a persistent note on background
    # refreshes too, not only when the user clicks Refresh. It does not consume the
    # one-shot notice.
    rate_limit = _rate_limit_status()
    return {
        "sessions": out_sessions,
        "auth": {"present": auth_present},
        **({"rate_limit": rate_limit} if rate_limit is not None else {}),
        **_forced_rate_limit_notice(force=force),
    }

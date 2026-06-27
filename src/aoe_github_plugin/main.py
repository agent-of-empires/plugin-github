"""Tier 1 worker entrypoint.

Speaks ndjson JSON-RPC 2.0 over stdio, both directions. The worker *answers*
host requests (``github.status`` / ``github.refresh`` / ``github.open``) and
*initiates* its own: it reads its ``ui_refresh_secs`` setting via ``config.get``,
enumerates sessions via ``sessions.list``, and pushes per-session + global UI
state via ``ui.state.set``.

Concurrency model -- the single-stdin-reader invariant. Only ONE thread may read
stdin. A dedicated reader thread drains stdin, parses each line, and puts the
message on an in-process queue (an ``_EOF`` sentinel at end of stream). The main
loop owns everything else: it consumes that queue, correlates replies to its own
outbound host RPCs (``Runtime.call_host``), dispatches inbound host requests,
and runs the periodic refresh. Because only the reader touches stdin, a slow
refresh never drops host messages -- they buffer in the queue.

The refresh runs synchronously on the main loop (discovery + GitHub fan-out live
in ``refresh``; the UI mapping in ``uistate``). At a multi-minute cadence that is
simpler and safe; the reader thread keeps stdin drained meanwhile. Exits on stdin
EOF, which is how the host shuts the worker down.

Run via the ``aoe-github-worker`` console script or ``python -m
aoe_github_plugin.main``.
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import queue
import random
import itertools
import threading
import contextlib
from typing import Any
from datetime import datetime
from datetime import timedelta
from collections.abc import Callable

from aoe_github_plugin import refresh
from aoe_github_plugin import uistate
from aoe_github_plugin import handlers
from aoe_github_plugin.utils.rpc import error_response
from aoe_github_plugin.utils.rpc import result_response

Sink = Callable[[dict[str, Any]], None]

UI_STATE_SET = "ui.state.set"
UI_STATE_REMOVE = "ui.state.remove"
UI_NOTIFY = "ui.notify"
SESSIONS_LIST = "sessions.list"
CONFIG_GET = "config.get"
REFRESH_SETTING_KEY = "ui_refresh_secs"
# Default NETWORK poll interval. Sized so worst-case (every key changes every
# tick, so each spends a REST + a GraphQL query) stays well under the user's
# shared 5000/hr budgets: at 120s a 20-key workspace tops out around 600 REST
# req/hr and ~600 GraphQL queries/hr, a small fraction of either limit. In the
# common steady state the REST conditional check returns 304 (free) and no
# GraphQL fires, so the real cost is far lower. The fast local session tick
# (SESSION_POLL_SECS) stays separate and unthrottled. See refresh.py and README.
DEFAULT_REFRESH_SECS = 120
# A wedged host must never freeze the worker: outbound host RPCs time out and
# the caller falls back (default interval, empty session list).
HOST_RPC_TIMEOUT = 10.0
# Fast tick: a cheap local `sessions.list` to notice a created/removed session
# within a couple seconds, decoupled from the (network) GitHub refresh. A short
# timeout so a wedged host stalls one tick, not the loop.
SESSION_POLL_SECS = 2.0
SESSION_LIST_TIMEOUT = 0.5
# Positive, bounded jitter added to each network deadline so independently
# started workers do not align their ticks into synchronized bursts against the
# API. Capped, and never negative: the worker must not poll faster than the
# configured interval.
NETWORK_JITTER_FRAC = 0.1
NETWORK_JITTER_MAX = 30.0

# End-of-stdin sentinel placed on the queue by the reader thread.
_EOF = object()

# Host-bound request ids live in their own high range so they never collide
# with the ids the host assigns to its requests to us.
_outbound_ids = itertools.count(1_000_000)
_stdout_lock = threading.Lock()


def _checkout_path(params: dict[str, Any]) -> str:
    """The checkout a single-path request applies to. The host supplies this;
    until the worker/host contract lands (#2095) it falls back to the worker
    CWD."""
    args = params.get("args") or {}
    return args.get("path") or params.get("cwd") or "."


def _send(message: dict[str, Any]) -> None:
    """Write one JSON-RPC message line to stdout, serialized across threads."""
    with _stdout_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def dispatch(method: str, params: dict[str, Any]) -> Any:
    """Return the result for a single-path host request, or raise.
    ``LookupError`` signals an unknown method; ``errors.GitHubError`` carries an
    actionable hint. ``github.refresh`` only acknowledges here -- the actual
    aggregate refresh is scheduled by the runtime (see ``Runtime.handle_inbound``).
    """
    if method == "github.status":
        return handlers.github_status(_checkout_path(params))
    if method == "github.open":
        return handlers.github_open(_checkout_path(params))
    if method == "github.refresh":
        return {"accepted": True}
    raise LookupError(method)


def _env_interval() -> int:
    """``AOE_GITHUB_UI_REFRESH_SECS`` override, else the default. A dev/test
    escape hatch used only when the plugin setting is unset."""
    raw = os.environ.get("AOE_GITHUB_UI_REFRESH_SECS")
    if raw is None:
        return DEFAULT_REFRESH_SECS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_REFRESH_SECS


def _rate_limit_notify_params(notice: dict[str, Any]) -> dict[str, Any]:
    """``ui.notify`` params for a rate-limit backoff. Global (no ``session``): the
    limit is token/IP-bound, not per-session. A known GraphQL reset gets a
    countdown (``ceil`` minutes, clamped to >=1 so an active backoff never shows
    "~0m", plus the local wall-clock time); the REST fallback has no real reset,
    so it stays generic rather than quoting the fixed 60s throttle as fact."""
    params: dict[str, Any] = {"tone": "warning", "title": "GitHub rate limited"}
    if notice.get("reset_known"):
        seconds = max(0.0, float(notice.get("seconds", 0.0)))
        minutes = max(1, math.ceil(seconds / 60))
        reset_at = datetime.now().astimezone() + timedelta(seconds=seconds)
        params["body"] = f"Resets in ~{minutes}m ({reset_at.strftime('%H:%M')})."
    else:
        params["body"] = "GitHub rate limit hit; showing cached data. Retrying shortly."
    return params


def _network_jitter(interval: int) -> float:
    """Positive, bounded jitter for a network deadline. Zero when polling is
    disabled (interval 0); otherwise up to ``NETWORK_JITTER_FRAC`` of the
    interval, capped at ``NETWORK_JITTER_MAX`` seconds."""
    if interval <= 0:
        return 0.0
    return random.uniform(0.0, min(NETWORK_JITTER_MAX, interval * NETWORK_JITTER_FRAC))  # noqa: S311 - not crypto


class Runtime:
    """The worker's main-loop runtime. Owns the inbound queue, the outbound host
    RPC correlation, and the periodic refresh. The reader thread is the only
    stdin reader; everything here runs on the main thread."""

    def __init__(self, send: Sink = _send, stdin: Any = None) -> None:
        self.send = send
        self.stdin = stdin if stdin is not None else sys.stdin
        self.inbox: queue.Queue[Any] = queue.Queue()
        self.stopped = False
        self.refresh_due = False
        # Session ids we last pushed UI state for, so a vanished session's
        # row-badge + pane can be removed (ui.state.remove) rather than linger.
        self.pushed_session_ids: set[str] = set()
        # Loop state, initialized in run(): the session-id set seen by the fast
        # tick, the resolved poll interval, and the next-fire monotonic deadlines.
        self._seen_ids: set[str] = set()
        self._interval = 0
        self._next_network: float | None = None
        self._next_poll: float | None = None

    def _read_stdin(self) -> None:
        """Reader thread: drain stdin into the queue, then post ``_EOF``."""
        for raw in self.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.inbox.put(msg)
        self.inbox.put(_EOF)

    def call_host(self, method: str, params: dict[str, Any], timeout: float = HOST_RPC_TIMEOUT) -> Any:
        """Blocking worker->host RPC: send the request, then drain the queue
        until its reply arrives, servicing any inbound host requests meanwhile.
        Returns the result, ``None`` on an error reply, on EOF (the stream
        closed -- the runtime is then stopping), or on ``timeout`` (a wedged
        host must never freeze the worker; the caller falls back)."""
        req_id = next(_outbound_ids)
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                item = self.inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            if item is _EOF:
                self.stopped = True
                self.inbox.put(_EOF)  # re-arm so the main loop also sees EOF
                return None
            if item.get("id") == req_id and "method" not in item:
                return item.get("result")
            self.handle_inbound(item)

    def handle_inbound(self, msg: dict[str, Any]) -> None:
        """Service one host->worker message: answer a request, ignore a stray
        reply. ``github.refresh`` also flags an aggregate refresh for the loop."""
        if "method" not in msg:
            return  # a host reply we are not currently waiting on
        method = msg.get("method", "")
        params = msg.get("params") or {}
        msg_id = msg.get("id")
        try:
            result = dispatch(method, params)
        except Exception as exc:  # noqa: BLE001 - any failure becomes a JSON-RPC error
            if isinstance(msg_id, int):
                self.send(error_response(msg_id, exc))
            return
        if isinstance(msg_id, int):
            self.send(result_response(msg_id, result))
        if method == "github.refresh":
            self.refresh_due = True

    def list_sessions(self, timeout: float = HOST_RPC_TIMEOUT) -> list[dict[str, Any]] | None:
        """Authoritative session list, or ``None`` if the host did not answer
        with one. ``None`` (timeout/error/garbage) is distinct from an empty
        list: a transient failure must never be read as "no sessions", which
        would prune every session's UI."""
        result = self.call_host(SESSIONS_LIST, {}, timeout=timeout)
        sessions = result.get("sessions") if isinstance(result, dict) else None
        if not isinstance(sessions, list):
            return None
        # Each entry must carry a string id. A malformed entry (e.g. `{}`) would
        # otherwise drop out of the snapshot and be read as a vanished session,
        # pruning live UI; treat a garbage list as "no answer" instead.
        if not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in sessions):
            return None
        return sessions

    def run_refresh(self, sessions: list[dict[str, Any]] | None = None, *, force: bool = False) -> None:
        """Build the aggregate snapshot from the session list and reconcile UI
        state: push each current session's row-badge + pane, then remove the
        slots of any session that has since vanished. Fail-soft: a host error, a
        bad workspace, or a closed pipe never raises. Skips entirely (no prune)
        when the session list is unavailable, so a transient failure cannot wipe
        live UI. ``force`` bypasses the GraphQL TTL so a user-clicked refresh
        fetches live CI/review data instead of a cache hit, and a rate-limited
        forced refresh also emits one ``ui.notify`` with the reset countdown."""
        if sessions is None:
            sessions = self.list_sessions()
        if sessions is None:
            return
        with contextlib.suppress(Exception):
            snapshot = refresh.build_snapshot(sessions, force=force)
            current_ids: set[str] = set()
            for params in uistate.snapshot_ui_state_params(snapshot):
                sid = params.get("session_id")
                if isinstance(sid, str):
                    current_ids.add(sid)
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": next(_outbound_ids),
                        "method": UI_STATE_SET,
                        "params": params,
                    }
                )
            for sid in self.pushed_session_ids - current_ids:
                for slot, slot_id in (uistate.ROW_BADGE_SLOT, uistate.PANE_SLOT):
                    self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": next(_outbound_ids),
                            "method": UI_STATE_REMOVE,
                            "params": {"slot": slot, "id": slot_id, "session_id": sid},
                        }
                    )
            self.pushed_session_ids = current_ids
            # A forced refresh that hit a rate limit carries a one-shot notice
            # (build_snapshot only adds it on force=True). Tell the user why the
            # refresh showed no change, at most once per backoff window.
            notice = snapshot.get("rate_limit_notice")
            if notice:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": next(_outbound_ids),
                        "method": UI_NOTIFY,
                        "params": _rate_limit_notify_params(notice),
                    }
                )

    def resolve_interval(self) -> int:
        """Poll period in seconds. Precedence: the host-persisted
        ``ui_refresh_secs`` setting (``config.get``) > the env override >
        ``DEFAULT_REFRESH_SECS``."""
        if self.stopped:
            return _env_interval()
        result = self.call_host(CONFIG_GET, {"key": REFRESH_SETTING_KEY})
        if isinstance(result, dict):
            value = result.get("value")
            # bool is an int subclass; a toggle value is not a valid interval.
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return _env_interval()

    def _refresh_and_reset(self, sessions: list[dict[str, Any]] | None = None, *, force: bool = False) -> None:
        """Refresh, re-baseline the seen-id set, and push the network tick out."""
        self.run_refresh(sessions, force=force)
        self._seen_ids = set(self.pushed_session_ids)
        if self._next_network is not None:
            self._next_network = time.monotonic() + self._interval + _network_jitter(self._interval)

    def _service_ticks(self) -> None:
        """Run whichever refresh is due: an inbound github.refresh, a fast local
        tick that noticed a session change, or the slower network tick."""
        now = time.monotonic()
        if self.refresh_due:
            self.refresh_due = False
            self._refresh_and_reset(force=True)
        if self._next_poll is not None and now >= self._next_poll:
            sessions = self.list_sessions(SESSION_LIST_TIMEOUT)
            if sessions is not None:
                ids = {s["id"] for s in sessions if isinstance(s, dict) and isinstance(s.get("id"), str)}
                if ids != self._seen_ids:
                    self._refresh_and_reset(sessions)
            self._next_poll = now + SESSION_POLL_SECS
        if self._next_network is not None and now >= self._next_network:
            self._refresh_and_reset()

    def run(self) -> None:
        threading.Thread(target=self._read_stdin, daemon=True).start()
        # Proactive refresh on startup so slots populate before any user action.
        self.run_refresh()
        self._seen_ids = set(self.pushed_session_ids)
        self._interval = self.resolve_interval()
        # interval 0 disables all background polling (startup + on github.refresh
        # still push); otherwise a fast local tick notices session changes and a
        # slower network tick catches external PR/state changes.
        now = time.monotonic()
        self._next_network = now + self._interval + _network_jitter(self._interval) if self._interval > 0 else None
        self._next_poll = now + SESSION_POLL_SECS if self._interval > 0 else None
        while not self.stopped:
            wakes = [t for t in (self._next_network, self._next_poll) if t is not None]
            timeout = max(0.0, min(wakes) - time.monotonic()) if wakes else None
            try:
                item = self.inbox.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is _EOF:
                break
            if item is not None:
                self.handle_inbound(item)
            self._service_ticks()


def main() -> None:
    Runtime().run()


if __name__ == "__main__":
    main()

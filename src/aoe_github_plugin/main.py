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
import time
import queue
import itertools
import threading
import contextlib
from typing import Any
from collections.abc import Callable

from aoe_github_plugin import refresh
from aoe_github_plugin import uistate
from aoe_github_plugin import handlers
from aoe_github_plugin.utils.rpc import error_response
from aoe_github_plugin.utils.rpc import result_response

Sink = Callable[[dict[str, Any]], None]

UI_STATE_SET = "ui.state.set"
SESSIONS_LIST = "sessions.list"
CONFIG_GET = "config.get"
REFRESH_SETTING_KEY = "ui_refresh_secs"
DEFAULT_REFRESH_SECS = 300
# A wedged host must never freeze the worker: outbound host RPCs time out and
# the caller falls back (default interval, empty session list).
HOST_RPC_TIMEOUT = 10.0

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

    def run_refresh(self) -> None:
        """Enumerate sessions, build the aggregate snapshot, and push UI state.
        Fail-soft: a host error, a bad workspace, or a closed pipe never raises."""
        with contextlib.suppress(Exception):
            sessions_result = self.call_host(SESSIONS_LIST, {})
            sessions = sessions_result.get("sessions") or [] if isinstance(sessions_result, dict) else []
            snapshot = refresh.build_snapshot(sessions)
            for params in uistate.snapshot_ui_state_params(snapshot):
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": next(_outbound_ids),
                        "method": UI_STATE_SET,
                        "params": params,
                    }
                )

    def resolve_interval(self) -> int:
        """Poll period in seconds. Precedence: the host-persisted
        ``ui_refresh_secs`` setting (``config.get``) > the env override > 300."""
        if self.stopped:
            return _env_interval()
        result = self.call_host(CONFIG_GET, {"key": REFRESH_SETTING_KEY})
        if isinstance(result, dict):
            value = result.get("value")
            # bool is an int subclass; a toggle value is not a valid interval.
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return _env_interval()

    def run(self) -> None:
        threading.Thread(target=self._read_stdin, daemon=True).start()
        # Proactive refresh on startup so slots populate before any user action.
        self.run_refresh()
        interval = self.resolve_interval()
        next_refresh = time.monotonic() + interval if interval > 0 else None
        while not self.stopped:
            timeout = None if next_refresh is None else max(0.0, next_refresh - time.monotonic())
            try:
                item = self.inbox.get(timeout=timeout)
            except queue.Empty:
                self.run_refresh()
                next_refresh = time.monotonic() + interval
                continue
            if item is _EOF:
                break
            self.handle_inbound(item)
            if self.refresh_due:
                self.refresh_due = False
                self.run_refresh()
                if interval > 0:
                    next_refresh = time.monotonic() + interval


def main() -> None:
    Runtime().run()


if __name__ == "__main__":
    main()

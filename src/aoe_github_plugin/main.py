"""Tier 1 worker entrypoint.

Speaks ndjson JSON-RPC 2.0 over stdio, both directions (see the worker host
"Transport and supervision" section of the core plugin-system doc). The worker
*answers* host requests on stdin (``github.status`` / ``github.refresh`` /
``github.open``) and also *initiates* requests of its own to the host: it pushes
PR status to the rendered UI slots via the ``ui.state.set`` host RPC, and reads
its ``ui_refresh_secs`` setting back via ``config.get`` (#2399). The host replies
to worker-initiated calls on stdin; a ``ui.state.set`` push ignores its reply
(fail-soft), while ``config.get`` blocks for its reply at startup. Exits on stdin
EOF, which is how the host shuts the worker down.

This file is transport + dispatch + the proactive UI push. The GitHub features
live in ``handlers``; the status -> display-state mapping lives in ``uistate``.
Run via the ``aoe-github-worker`` console script or
``python -m aoe_github_plugin.main``.
"""

from __future__ import annotations

import os
import sys
import json
import itertools
import threading
import contextlib
from typing import Any
from collections.abc import Callable
from collections.abc import Iterator

from aoe_github_plugin import uistate
from aoe_github_plugin import handlers
from aoe_github_plugin.utils.rpc import error_response
from aoe_github_plugin.utils.rpc import result_response

Sink = Callable[[dict[str, Any]], None]
Lines = Iterator[str]

UI_STATE_SET = "ui.state.set"
CONFIG_GET = "config.get"
REFRESH_SETTING_KEY = "ui_refresh_secs"
DEFAULT_REFRESH_SECS = 300

# Host-bound request ids live in their own high range so they never collide
# with the ids the host assigns to its requests to us.
_outbound_ids = itertools.count(1_000_000)
_stdout_lock = threading.Lock()


def _checkout_path(params: dict[str, Any]) -> str:
    """The checkout the request applies to. The host will supply this; until
    the worker/host contract lands (#2095) it falls back to the worker CWD."""
    args = params.get("args") or {}
    return args.get("path") or params.get("cwd") or "."


def _send(message: dict[str, Any]) -> None:
    """Write one JSON-RPC message line to stdout, serialized across threads
    (the poll thread and the main loop both write)."""
    with _stdout_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def dispatch(method: str, params: dict[str, Any]) -> Any:
    """Return the result for ``method``, or raise. ``LookupError`` signals an
    unknown method; ``errors.GitHubError`` carries an actionable hint."""
    if method in ("github.status", "github.refresh"):
        return handlers.github_status(_checkout_path(params))
    if method == "github.open":
        return handlers.github_open(_checkout_path(params))
    raise LookupError(method)


def push_ui_state(status: dict[str, Any], send: Sink = _send) -> None:
    """Push the PR status to every UI slot via ``ui.state.set``. Fail-soft:
    never raises, so a bad status or a closed pipe cannot take down the worker.
    Fire-and-forget; the host's reply is ignored by the read loop.
    """
    # A UI push is best-effort: a bad status, a closed pipe, or a host that
    # rejects the call must never take down the worker.
    with contextlib.suppress(Exception):
        for params in uistate.ui_state_params(status):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": next(_outbound_ids),
                    "method": UI_STATE_SET,
                    "params": params,
                }
            )


def _handle_inbound(msg: dict[str, Any], send: Sink = _send) -> None:
    """Service one host->worker message: answer a request, ignore a stray reply.
    A ``github.status`` / ``github.refresh`` also re-pushes the UI slots
    (notifications too: a refresh with no id should still update the badge)."""
    if "method" not in msg:
        return  # a host reply to one of our pushes / calls; not ours to answer
    method = msg.get("method", "")
    params = msg.get("params") or {}
    msg_id = msg.get("id")
    try:
        result = dispatch(method, params)
    except Exception as exc:  # noqa: BLE001 - any failure becomes a JSON-RPC error
        if isinstance(msg_id, int):
            send(error_response(msg_id, exc))
        return
    if isinstance(msg_id, int):
        send(result_response(msg_id, result))
    if method in ("github.status", "github.refresh"):
        push_ui_state(result, send)


def _call_host(method: str, params: dict[str, Any], send: Sink, lines: Lines) -> Any:
    """Make a blocking worker->host RPC: send the request, then read ``lines``
    until its response arrives, servicing any host requests seen meanwhile so
    none are dropped. Returns the result, or ``None`` on an error reply or if
    the stream closes first. Safe from a hang: the host is the server and always
    replies (an unknown method comes back as an error, not silence)."""
    req_id = next(_outbound_ids)
    send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == req_id and "method" not in msg:
            return msg.get("result")  # None on an error reply: caller defaults
        _handle_inbound(msg, send)
    return None


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


def _resolve_interval(send: Sink, lines: Lines) -> int:
    """Poll period in seconds. Precedence: the host-persisted ``ui_refresh_secs``
    plugin setting (read via ``config.get``, #2399) > the env override > 300.
    ponytail: a worker-side poll, not host-driven; the long default keeps
    unauthenticated runs clear of GitHub's 60 req/hr ceiling."""
    result = _call_host(CONFIG_GET, {"key": REFRESH_SETTING_KEY}, send, lines)
    if isinstance(result, dict):
        value = result.get("value")
        # bool is an int subclass; a toggle value is not a valid interval.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return _env_interval()


def _poll_loop(interval: int, path: str, stop: threading.Event, send: Sink = _send) -> None:
    while not stop.wait(interval):
        push_ui_state(handlers.github_status(path), send)


def main() -> None:
    path = "."
    lines = iter(sys.stdin)
    # Proactive push on startup so the slot is populated before any user action.
    push_ui_state(handlers.github_status(path))

    interval = _resolve_interval(_send, lines)
    if interval > 0:
        stop = threading.Event()
        threading.Thread(target=_poll_loop, args=(interval, path, stop), daemon=True).start()

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        _handle_inbound(msg)


if __name__ == "__main__":
    main()

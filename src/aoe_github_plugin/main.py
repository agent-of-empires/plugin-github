"""Tier 1 worker entrypoint.

Speaks ndjson JSON-RPC over stdio: one JSON object per line on stdin, one
response object per line on stdout. Requests carrying an integer ``id`` get a
reply; notifications (no ``id``) are processed silently. Exits on stdin EOF,
which is how the host shuts the worker down.

This file is only transport + dispatch; the GitHub features live in
``handlers``. Run via the ``aoe-github-worker`` console script or
``python -m aoe_github_plugin.main``.
"""

from __future__ import annotations

import sys
import json
from typing import Any

from aoe_github_plugin import handlers
from aoe_github_plugin.utils.rpc import error_response
from aoe_github_plugin.utils.rpc import result_response


def _checkout_path(params: dict[str, Any]) -> str:
    """The checkout the request applies to. The host will supply this; until
    the worker/host contract lands (#2095) it falls back to the worker CWD."""
    args = params.get("args") or {}
    return args.get("path") or params.get("cwd") or "."


def dispatch(method: str, params: dict[str, Any]) -> Any:
    """Return the result for ``method``, or raise. ``LookupError`` signals an
    unknown method; ``errors.GitHubError`` carries an actionable hint."""
    if method in ("github.status", "github.refresh"):
        return handlers.github_status(_checkout_path(params))
    raise LookupError(method)


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        if not isinstance(msg_id, int):
            continue  # notification: nothing to subscribe to in this foundation
        method = msg.get("method", "")
        params = msg.get("params") or {}
        try:
            response = result_response(msg_id, dispatch(method, params))
        except Exception as exc:  # noqa: BLE001 - any failure becomes a JSON-RPC error
            response = error_response(msg_id, exc)
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

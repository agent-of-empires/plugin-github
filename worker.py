#!/usr/bin/env python3
"""Tier 1 worker for the Agent of Empires GitHub plugin.

Speaks ndjson JSON-RPC over stdio: one JSON object per line on stdin, one
response object per line on stdout. Requests carrying an integer ``id`` get a
reply; notifications (no ``id``) are processed silently. Exits on stdin EOF,
which is how the host shuts the worker down.

The handlers below return placeholder text. Replace them with real GitHub API
calls (add the ``net-fetch`` capability to aoe-plugin.toml first).
"""

import json
import sys


def handle(method, params):
    """Return the JSON-RPC result for a method call.

    ``params`` for a CLI command is ``{"args": {<arg-name>: <value>, ...}}``.
    Raise to return a JSON-RPC error to the host.
    """
    if method == "github.status":
        # ponytail: placeholder. Real impl: read the checkout's remote, query
        # the GitHub API for open PRs/issues, format a one-line summary.
        return "GitHub: no integration configured yet (scaffold)."
    if method == "github.refresh":
        return "GitHub: refreshed (scaffold)."
    raise ValueError(f"unknown method {method!r}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        if not isinstance(msg_id, int):
            # Notification: nothing to subscribe to in this scaffold.
            continue
        method = msg.get("method", "")
        params = msg.get("params") or {}
        try:
            result = handle(method, params)
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception as exc:  # noqa: BLE001 - report any handler failure
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

"""JSON-RPC 2.0 response builders.

Keeps ``main`` thin: a typed ``GitHubError`` becomes an application error
(``-32000``) carrying the actionable hint plus a machine-branchable
``data.kind``; an unknown method is ``-32601``; anything else is ``-32603``.
"""

from __future__ import annotations

from typing import Any

from aoe_github_plugin.errors import GitHubError

ERR_GITHUB = -32000
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL = -32603


def result_response(msg_id: int, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id: int, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, GitHubError):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": ERR_GITHUB,
                "message": str(exc),
                "data": {"kind": exc.kind},
            },
        }
    if isinstance(exc, LookupError):
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {
                "code": ERR_METHOD_NOT_FOUND,
                "message": f"unknown method {str(exc)!r}",
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": ERR_INTERNAL, "message": str(exc)},
    }

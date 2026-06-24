"""Drive the worker as a subprocess over stdio, like the host will."""

import sys
import json
import subprocess


def _run(*lines):
    proc = subprocess.run(
        [sys.executable, "-m", "aoe_github_plugin.main"],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return [json.loads(out) for out in proc.stdout.splitlines() if out.strip()]


def test_unknown_method_returns_method_not_found():
    out = _run(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "github.nope"}))
    assert len(out) == 1
    assert out[0]["error"]["code"] == -32601


def test_notification_produces_no_output():
    out = _run(json.dumps({"jsonrpc": "2.0", "method": "github.status"}))
    assert out == []


def test_status_is_failsoft_outside_a_repo():
    # path "/" is not a git checkout; status must always return a structured
    # result, never an error (issue #1667 fail-soft requirement).
    out = _run(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "github.status",
                "params": {"args": {"path": "/"}},
            }
        )
    )
    assert len(out) == 1
    result = out[0]["result"]
    assert isinstance(result, dict)
    assert isinstance(result["summary"], str)
    assert result["pulls"] == []


def test_open_outside_a_repo_returns_error():
    # "/" has no github.com remote; github.open surfaces a typed error.
    out = _run(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "github.open",
                "params": {"args": {"path": "/"}},
            }
        )
    )
    assert len(out) == 1
    assert out[0]["error"]["code"] == -32000

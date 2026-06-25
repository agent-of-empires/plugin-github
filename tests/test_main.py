"""Drive the worker as a subprocess over stdio, like the host will."""

import os
import sys
import json
import subprocess


def _run(*lines):
    """Run the worker and return (responses, pushes).

    responses are replies to the requests we sent (``result``/``error``);
    pushes are the worker-initiated ``ui.state.set`` requests. cwd is a
    non-repo dir so the startup push fail-softs without a network call, and the
    poll thread is disabled so output is deterministic.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "aoe_github_plugin.main"],
        input="".join(line + "\n" for line in lines),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        cwd="/",
        env={**os.environ, "AOE_GITHUB_UI_REFRESH_SECS": "0"},
    )
    msgs = [json.loads(out) for out in proc.stdout.splitlines() if out.strip()]
    responses = [m for m in msgs if "result" in m or "error" in m]
    pushes = [m for m in msgs if m.get("method") == "ui.state.set"]
    return responses, pushes


def test_unknown_method_returns_method_not_found():
    responses, _ = _run(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "github.nope"}))
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == -32601


def test_notification_produces_no_response():
    responses, _ = _run(json.dumps({"jsonrpc": "2.0", "method": "github.status"}))
    assert responses == []


def test_status_is_failsoft_outside_a_repo():
    # path "/" is not a git checkout; status must always return a structured
    # result, never an error (issue #1667 fail-soft requirement).
    responses, _ = _run(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "github.status",
                "params": {"args": {"path": "/"}},
            }
        )
    )
    assert len(responses) == 1
    result = responses[0]["result"]
    assert isinstance(result, dict)
    assert isinstance(result["summary"], str)
    assert result["pulls"] == []


def test_open_outside_a_repo_returns_error():
    # "/" has no github.com remote; github.open surfaces a typed error.
    responses, _ = _run(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "github.open",
                "params": {"args": {"path": "/"}},
            }
        )
    )
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == -32000


def test_startup_pushes_ui_state_to_every_slot():
    # No input: the worker still proactively pushes the UI slots on startup.
    _, pushes = _run()
    slots = {p["params"]["slot"] for p in pushes}
    assert slots == {"status-bar", "row-badge"}
    for p in pushes:
        assert p["method"] == "ui.state.set"
        assert isinstance(p["id"], int)
        assert "tone" in p["params"]["payload"]

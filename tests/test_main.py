"""Drive the worker as a subprocess over stdio, like the host will."""

import os
import sys
import json
import subprocess

from aoe_github_plugin import main


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


def test_startup_without_a_host_pushes_nothing():
    # No input: sessions.list gets no reply (EOF), so there are no sessions and
    # thus no per-session badges/panes to push. The UI is push-only per session;
    # there is no global slot.
    _, pushes = _run()
    assert pushes == []


# --- rate-limit notification on forced refresh (issue #20) ---


def _runtime_with_snapshot(monkeypatch, snapshot):
    """A Runtime whose build_snapshot returns ``snapshot`` (when forced) and a
    capture sink for the messages it sends."""
    sent = []
    rt = main.Runtime(send=sent.append)

    def fake_build_snapshot(sessions, force=False):
        out = {"sessions": [], "auth": {"present": True}}
        if force and snapshot.get("rate_limit_notice") is not None:
            out["rate_limit_notice"] = snapshot["rate_limit_notice"]
        return out

    monkeypatch.setattr(main.refresh, "build_snapshot", fake_build_snapshot)
    return rt, sent


def _notifies(sent):
    return [m for m in sent if m.get("method") == "ui.notify"]


def test_forced_refresh_emits_one_notify_with_countdown(monkeypatch):
    rt, sent = _runtime_with_snapshot(monkeypatch, {"rate_limit_notice": {"seconds": 1800, "reset_known": True}})
    rt.run_refresh(sessions=[], force=True)
    notifies = _notifies(sent)
    assert len(notifies) == 1
    params = notifies[0]["params"]
    assert params["tone"] == "warning"
    assert params["title"] == "GitHub rate limited"
    assert "Resets in" in params["body"]


def test_forced_refresh_unknown_reset_is_generic(monkeypatch):
    rt, sent = _runtime_with_snapshot(monkeypatch, {"rate_limit_notice": {"seconds": 60, "reset_known": False}})
    rt.run_refresh(sessions=[], force=True)
    notifies = _notifies(sent)
    assert len(notifies) == 1
    assert "Resets in" not in notifies[0]["params"]["body"]


def test_background_refresh_emits_no_notify(monkeypatch):
    rt, sent = _runtime_with_snapshot(monkeypatch, {"rate_limit_notice": {"seconds": 1800, "reset_known": True}})
    rt.run_refresh(sessions=[], force=False)
    assert _notifies(sent) == []

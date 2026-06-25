"""Worker-side config.get round-trip and poll-interval resolution (#2399)."""

import json

from aoe_github_plugin import main


def _reply(sent, **fields):
    """A JSON line replying to the config.get request the worker just sent.
    ``sent[0]`` is that request (the first thing _resolve_interval emits); its
    id is captured so an interleaved host request cannot shift it."""
    return json.dumps({"jsonrpc": "2.0", "id": sent[0]["id"], **fields})


def test_setting_value_drives_the_interval():
    sent = []

    def lines():
        # The config.get request has been sent by the time we are iterated.
        assert sent[0]["method"] == "config.get"
        assert sent[0]["params"] == {"key": "ui_refresh_secs"}
        yield _reply(sent, result={"value": 42})

    assert main._resolve_interval(sent.append, lines()) == 42


def test_unset_setting_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("AOE_GITHUB_UI_REFRESH_SECS", "0")
    sent = []

    def lines():
        yield _reply(sent, result={"value": None})

    assert main._resolve_interval(sent.append, lines()) == 0


def test_missing_setting_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("AOE_GITHUB_UI_REFRESH_SECS", raising=False)
    sent = []

    def lines():
        yield _reply(sent, error={"code": -32601, "message": "nope"})

    assert main._resolve_interval(sent.append, lines()) == main.DEFAULT_REFRESH_SECS


def test_bool_value_is_not_a_valid_interval(monkeypatch):
    monkeypatch.setenv("AOE_GITHUB_UI_REFRESH_SECS", "120")
    sent = []

    def lines():
        yield _reply(sent, result={"value": True})

    assert main._resolve_interval(sent.append, lines()) == 120


def test_closed_stream_before_reply_falls_back(monkeypatch):
    monkeypatch.delenv("AOE_GITHUB_UI_REFRESH_SECS", raising=False)
    assert main._resolve_interval([].append, iter(())) == main.DEFAULT_REFRESH_SECS


def test_host_request_during_the_wait_is_serviced():
    # A github.status request arriving before the config.get reply must be
    # answered (and its UI pushed), not dropped.
    sent = []

    def lines():
        yield json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "github.status",
                "params": {"args": {"path": "/"}},
            }
        )
        yield _reply(sent, result={"value": 5})

    assert main._resolve_interval(sent.append, lines()) == 5
    answered = [m for m in sent if m.get("id") == 99 and "result" in m]
    assert len(answered) == 1
    assert answered[0]["result"]["pulls"] == []
    pushes = [m for m in sent if m.get("method") == "ui.state.set"]
    assert pushes  # the serviced status also pushed UI state

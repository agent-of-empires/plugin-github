"""Worker-side config.get round-trip and poll-interval resolution (#2399).

`resolve_interval` is a `Runtime` method that sends config.get then drains the
inbound queue for its reply (servicing host requests meanwhile). These tests
run it on a thread and feed the queue replies from the test thread, mirroring
how the reader thread would deliver them in production.
"""

import time
import threading

from aoe_github_plugin import main


def _wait_request(sent):
    for _ in range(2000):
        if sent:
            return
        time.sleep(0.001)
    raise AssertionError("no request was sent")


def _resolve_with(feed):
    """Run `resolve_interval` on a thread; `feed(rt, sent)` delivers replies."""
    sent = []
    rt = main.Runtime(send=sent.append, stdin=iter(()))
    box = {}
    t = threading.Thread(target=lambda: box.__setitem__("v", rt.resolve_interval()))
    t.start()
    feed(rt, sent)
    t.join(timeout=5)
    assert not t.is_alive()
    return box["v"], sent


def test_setting_value_drives_the_interval():
    def feed(rt, sent):
        _wait_request(sent)
        assert sent[0]["method"] == "config.get"
        assert sent[0]["params"] == {"key": "ui_refresh_secs"}
        rt.inbox.put({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"value": 42}})

    assert _resolve_with(feed)[0] == 42


def test_unset_setting_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("AOE_GITHUB_UI_REFRESH_SECS", "0")

    def feed(rt, sent):
        _wait_request(sent)
        rt.inbox.put({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"value": None}})

    assert _resolve_with(feed)[0] == 0


def test_missing_setting_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("AOE_GITHUB_UI_REFRESH_SECS", raising=False)

    def feed(rt, sent):
        _wait_request(sent)
        rt.inbox.put({"jsonrpc": "2.0", "id": sent[0]["id"], "error": {"code": -32601, "message": "nope"}})

    assert _resolve_with(feed)[0] == main.DEFAULT_REFRESH_SECS


def test_ignore_submodules_setting_defaults_on():
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: {"value": None}
    assert rt.resolve_ignore_submodules() is True


def test_ignore_submodules_setting_respects_false():
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: {"value": False}
    assert rt.resolve_ignore_submodules() is False


def test_bool_value_is_not_a_valid_interval(monkeypatch):
    monkeypatch.setenv("AOE_GITHUB_UI_REFRESH_SECS", "120")

    def feed(rt, sent):
        _wait_request(sent)
        rt.inbox.put({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"value": True}})

    assert _resolve_with(feed)[0] == 120


def test_closed_stream_before_reply_falls_back(monkeypatch):
    monkeypatch.delenv("AOE_GITHUB_UI_REFRESH_SECS", raising=False)

    def feed(rt, sent):
        _wait_request(sent)
        rt.inbox.put(main._EOF)

    assert _resolve_with(feed)[0] == main.DEFAULT_REFRESH_SECS


def test_host_request_during_the_wait_is_serviced():
    # A github.status request arriving before the config.get reply must be
    # answered, not dropped. (Single-path status no longer pushes UI state;
    # only the scheduled refresh does.)
    def feed(rt, sent):
        rt.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "github.status",
                "params": {"args": {"path": "/"}},
            }
        )
        _wait_request(sent)
        rt.inbox.put({"jsonrpc": "2.0", "id": sent[0]["id"], "result": {"value": 5}})

    value, sent = _resolve_with(feed)
    assert value == 5
    answered = [m for m in sent if m.get("id") == 99 and "result" in m]
    assert len(answered) == 1
    assert answered[0]["result"]["pulls"] == []

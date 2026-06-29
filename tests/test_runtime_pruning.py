"""Runtime UI reconciliation: push current sessions, prune vanished ones, and
never wipe UI when the session list is unavailable."""

from aoe_github_plugin import main as m
from aoe_github_plugin import refresh
from aoe_github_plugin import uistate


def _const(value):
    """A stub host call that ignores its args and returns a fixed value."""

    def stub(*args, **kwargs):
        return value

    return stub


def _runtime():
    sent: list = []
    rt = m.Runtime(send=sent.append)
    rt.call_host = _const({"value": True})
    return rt, sent


def _fake_params(monkeypatch, session_ids):
    monkeypatch.setattr(refresh, "build_snapshot", lambda sessions, **_kwargs: {"sessions": sessions})
    monkeypatch.setattr(
        uistate,
        "snapshot_ui_state_params",
        lambda _snap, **_kwargs: [
            {"slot": slot, "id": slot_id, "session_id": sid, "payload": {}}
            for sid in session_ids
            for slot, slot_id in (uistate.ROW_BADGE_SLOT, uistate.ROW_COLUMN_SLOT, uistate.PANE_SLOT)
        ],
    )


def test_run_refresh_pushes_current_and_prunes_vanished(monkeypatch):
    rt, sent = _runtime()
    rt.pushed_session_ids = {"old"}
    _fake_params(monkeypatch, ["s1"])

    rt.run_refresh(sessions=[{"id": "s1"}])

    sets = [x for x in sent if x["method"] == "ui.state.set"]
    removes = [x for x in sent if x["method"] == "ui.state.remove"]
    assert {x["params"]["session_id"] for x in sets} == {"s1"}
    assert {x["params"]["session_id"] for x in removes} == {"old"}
    # Every per-session slot is removed for the vanished session.
    assert {x["params"]["slot"] for x in removes} == {
        uistate.ROW_BADGE_SLOT[0],
        uistate.ROW_COLUMN_SLOT[0],
        uistate.PANE_SLOT[0],
    }
    assert rt.pushed_session_ids == {"s1"}


def test_run_refresh_skips_when_session_list_unavailable(monkeypatch):
    rt, sent = _runtime()
    rt.pushed_session_ids = {"s1"}
    monkeypatch.setattr(rt, "list_sessions", _const(None))

    rt.run_refresh()

    # A transient failure must not prune: nothing sent, tracked set intact.
    assert sent == []
    assert rt.pushed_session_ids == {"s1"}


def test_list_sessions_rejects_bad_shapes(monkeypatch):
    rt, _ = _runtime()
    for bad in (None, {}, {"sessions": "nope"}, "garbage", 42):
        monkeypatch.setattr(rt, "call_host", _const(bad))
        assert rt.list_sessions() is None
    # A list whose entries lack a string id is garbage, not "no sessions":
    # reading it as empty would prune live UI.
    for bad in ({"sessions": [{}]}, {"sessions": [{"id": 1}]}, {"sessions": [{"id": "s1"}, "x"]}):
        monkeypatch.setattr(rt, "call_host", _const(bad))
        assert rt.list_sessions() is None
    monkeypatch.setattr(rt, "call_host", _const({"sessions": [{"id": "s1"}]}))
    assert rt.list_sessions() == [{"id": "s1"}]

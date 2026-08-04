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
    rt.call_host = lambda *_a, **_kw: {"value": True}

    def fake_build_snapshot(sessions, force=False, **_kwargs):
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


def test_resolve_chip_flags_defaults_all_on():
    # No host answer (None) -> every category stays on, so nothing is hidden by
    # accident when the setting is unset or the host does not reply.
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: None
    assert rt.resolve_chip_flags() == frozenset({"review", "ci", "comments"})


def test_resolve_chip_flags_respects_disabled_setting():
    rt = main.Runtime(send=lambda _m: None)
    off = {"show_ci_status"}
    rt.call_host = lambda _method, params, **_kw: {"value": params["key"] not in off}
    assert rt.resolve_chip_flags() == frozenset({"review", "comments"})


def test_required_checks_setting_reaches_refresh(monkeypatch):
    seen = []

    def spy_build_snapshot(sessions, **kwargs):
        seen.append(kwargs["settings"].required_checks_only)
        return {"sessions": [], "auth": {"present": True}}

    monkeypatch.setattr(main.refresh, "build_snapshot", spy_build_snapshot)
    rt = main.Runtime(send=lambda _m: None)
    rt._required_checks_only = True
    rt.call_host = lambda *_a, **_kw: {"value": True}
    rt.run_refresh(sessions=[])
    assert seen == [True]


# --- archived/snoozed sessions are skipped (issue #41) ---


def _list_sessions_with(reply):
    """list_sessions over a stubbed host reply."""
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: reply
    return rt.list_sessions()


def test_list_sessions_drops_archived_and_snoozed():
    # Archived and snoozed are inactive; only active sessions survive so the
    # network refresh never spends GitHub quota on them.
    reply = {
        "sessions": [
            {"id": "active", "project_path": "/a"},
            {"id": "arch", "project_path": "/b", "archived": True, "snoozed": False},
            {"id": "snoozed", "project_path": "/c", "archived": False, "snoozed": True},
        ]
    }
    assert [s["id"] for s in _list_sessions_with(reply)] == ["active"]


def test_list_sessions_keeps_sessions_when_flags_absent():
    # A host that predates the flags (#2504) omits them; every session is polled
    # as before, no accidental pruning.
    reply = {"sessions": [{"id": "s1", "project_path": "/a"}, {"id": "s2", "project_path": "/b"}]}
    assert [s["id"] for s in _list_sessions_with(reply)] == ["s1", "s2"]


def test_list_sessions_requests_server_side_exclude():
    # The worker asks the host to drop archived/snoozed/trashed server-side, so a
    # workspace with hundreds of trashed sessions never enumerates them and cannot
    # exhaust the host's per-plugin UI-state quota.
    captured = {}

    def fake_call_host(method, params, timeout=None):
        captured["method"] = method
        captured["params"] = params
        return {"sessions": [{"id": "s1", "project_path": "/a"}]}

    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = fake_call_host
    rt.list_sessions()
    assert captured["method"] == main.SESSIONS_LIST
    assert set(captured["params"]["exclude"]) == {"archived", "snoozed", "trashed"}


def test_archived_session_never_reaches_build_snapshot(monkeypatch):
    # End-to-end: an archived session in the host reply triggers zero network
    # work, it is filtered before build_snapshot, which is what does the HTTP.
    seen_sessions = []

    def spy_build_snapshot(sessions, force=False, **_kwargs):
        seen_sessions.append([s.get("id") for s in sessions])
        return {"sessions": [], "auth": {"present": True}}

    monkeypatch.setattr(main.refresh, "build_snapshot", spy_build_snapshot)
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: {
        "sessions": [
            {"id": "active", "project_path": "/a"},
            {"id": "arch", "project_path": "/b", "archived": True},
        ]
    }
    rt.run_refresh()
    assert seen_sessions == [["active"]]


def test_run_refresh_passes_ignore_submodules_setting(monkeypatch):
    seen = []

    def spy_build_snapshot(_sessions, **kwargs):
        seen.append(kwargs["settings"].ignore_submodules)
        return {"sessions": [], "auth": {"present": True}}

    monkeypatch.setattr(main.refresh, "build_snapshot", spy_build_snapshot)
    rt = main.Runtime(send=lambda _m: None)
    rt._ignore_submodules = False
    rt.call_host = lambda *_a, **_kw: {"value": False}
    rt.run_refresh(sessions=[])
    assert seen == [False]


def test_default_network_interval_is_120():
    # Network tick default sized for the rate-limit budget (#22). The fast local
    # session tick is a separate, smaller constant.
    assert main.DEFAULT_REFRESH_SECS == 120
    assert main.SESSION_POLL_SECS < main.DEFAULT_REFRESH_SECS


def test_network_jitter_is_positive_and_bounded():
    # Disabled polling: no jitter.
    assert main._network_jitter(0) == 0.0
    cap = min(main.NETWORK_JITTER_MAX, 120 * main.NETWORK_JITTER_FRAC)
    for _ in range(200):
        j = main._network_jitter(120)
        assert 0.0 <= j <= cap  # never negative (must not poll faster than configured)
    # The cap holds for a large interval too (absolute ceiling, not just a fraction).
    assert main._network_jitter(100000) <= main.NETWORK_JITTER_MAX


def _fake_per_session_params(monkeypatch):
    """build_snapshot + snapshot_ui_state_params stubs that emit one row-badge +
    pane per session in the list passed to build_snapshot, so a test can assert
    exactly which sessions were refreshed."""
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda sessions, **_k: {"sessions": sessions})
    monkeypatch.setattr(
        main.uistate,
        "snapshot_ui_state_params",
        lambda snap, **_k: [
            {"slot": slot, "id": slot_id, "session_id": s["id"], "payload": {"text": "x"}}
            for s in snap["sessions"]
            for slot, slot_id in (main.uistate.ROW_BADGE_SLOT, main.uistate.ROW_COLUMN_SLOT, main.uistate.PANE_SLOT)
        ],
    )


def test_github_refresh_captures_session_target():
    rt = main.Runtime(send=lambda _m: None)
    rt.handle_inbound({"method": "github.refresh", "params": {"session_id": "s1"}})
    assert rt.refresh_due is True
    assert rt._refresh_target == "s1"
    # No / non-string session id falls back to a full refresh.
    rt.handle_inbound({"method": "github.refresh", "params": {}})
    assert rt._refresh_target is None
    rt.handle_inbound({"method": "github.refresh", "params": {"session_id": 5}})
    assert rt._refresh_target is None


def test_scoped_refresh_targets_only_the_clicked_session(monkeypatch):
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.pushed_session_ids = {"s1", "s2"}
    rt.call_host = lambda *_a, **_kw: {"value": True}
    _fake_per_session_params(monkeypatch)

    rt.run_refresh(sessions=[{"id": "s1"}, {"id": "s2"}], force=True, only_session="s1")

    sets = {x["params"]["session_id"] for x in sent if x["method"] == "ui.state.set"}
    removes = [x for x in sent if x["method"] == "ui.state.remove"]
    # Only the clicked session is fetched and pushed; the other is untouched, not
    # pruned, so its slots keep showing.
    assert sets == {"s1"}
    assert removes == []
    assert rt.pushed_session_ids == {"s1", "s2"}


def test_full_refresh_still_prunes_vanished(monkeypatch):
    # The unscoped path is unchanged: a session that drops out is pruned.
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.pushed_session_ids = {"s1", "gone"}
    rt.call_host = lambda *_a, **_kw: {"value": True}
    _fake_per_session_params(monkeypatch)

    rt.run_refresh(sessions=[{"id": "s1"}], force=True)

    removes = {x["params"]["session_id"] for x in sent if x["method"] == "ui.state.remove"}
    assert removes == {"gone"}
    assert rt.pushed_session_ids == {"s1"}


def test_pr_less_session_removes_row_column_not_empty_set(monkeypatch):
    # A session whose repos have no open PRs and no error yields an empty
    # row-column payload from the pure mapper (issue #66). It must dispatch as a
    # ui.state.remove (which clears any stale cell), never a ui.state.set with an
    # empty payload, which the host rejects with `missing field text`.
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    snapshot = {
        "sessions": [{"session_id": "s1", "repos": [{"name": "r", "repo": "o/r", "pulls": []}]}],
        "auth": {"present": True},
    }
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda _sessions, **_k: snapshot)

    rt.run_refresh(sessions=[{"id": "s1"}], force=True)

    columns = [m for m in sent if m["params"].get("slot") == "row-column"]
    assert columns, "expected a row-column push for the session"
    for m in columns:
        assert not (m["method"] == "ui.state.set" and not m["params"]["payload"]), (
            "empty row-column must not be a ui.state.set (host rejects missing text)"
        )
    assert any(m["method"] == "ui.state.remove" for m in columns), "empty row-column should clear via ui.state.remove"


def _two_pr_snapshot():
    def pull(number, review):
        return {
            "number": number,
            "url": f"https://github.com/o/r/pull/{number}",
            "title": f"T{number}",
            "state": "OPEN",
            "draft": False,
            "merged": False,
            "review_state": review,
            "comments": {"unresolved": 0, "items": []},
        }

    return {
        "sessions": [
            {
                "session_id": "s1",
                "repos": [
                    {"name": "r", "repo": "o/r", "branch": "b", "pulls": [pull(1, "approved"), pull(2, "waiting")]}
                ],
            }
        ],
        "auth": {"present": True},
    }


def _pane_callout(messages):
    pane = [m for m in messages if m["params"].get("slot") == "pane"][-1]
    return next(b for b in pane["params"]["payload"]["blocks"] if b.get("kind") == "callout")


def test_select_pr_repaints_from_cache_without_a_network_refresh(monkeypatch):
    # Picking a PR only changes which one the pane details, so it must repaint from
    # the snapshot already on screen: a refresh here would spend GitHub quota on
    # every click, and the click's spinner only needs the UI revision to move.
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda _sessions, **_k: _two_pr_snapshot())
    rt.run_refresh(sessions=[{"id": "s1"}], force=True)
    # Default selection is the most-actionable PR (awaiting review beats approved).
    assert _pane_callout(sent)["title"] == "Awaiting review"

    sent.clear()
    rt.handle_inbound({"method": main.SELECT_PR_METHOD, "params": {"session_id": "s1", "pr": "o/r#1"}})

    assert rt._selected_pr == {"s1": "o/r#1"}
    assert rt.refresh_due is False, "selecting a PR must not schedule a network refresh"
    assert _pane_callout(sent)["title"] == "Ready to merge"


def test_select_pr_ignores_a_malformed_click_and_acks_it(monkeypatch):
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda _sessions, **_k: _two_pr_snapshot())
    rt.run_refresh(sessions=[{"id": "s1"}], force=True)
    sent.clear()

    for params in (
        {"session_id": "s1"},
        {"pr": "o/r#1"},
        {"session_id": 5, "pr": "o/r#1"},
        {"session_id": "s1", "pr": ""},
    ):
        rt.handle_inbound({"method": main.SELECT_PR_METHOD, "params": params})
    assert rt._selected_pr == {}
    assert sent == [], "a malformed selection changes nothing and repaints nothing"

    # The method itself is still a known request, so a host that sends it with an
    # id gets a result rather than an unknown-method error.
    assert main.dispatch(main.SELECT_PR_METHOD, {}) == {"accepted": True}


# --- instant repaint from a persisted snapshot on restart (#63) ---


def test_replay_cached_pushes_only_live_sessions_marked_stale(tmp_path, monkeypatch):
    # A session that vanished while the daemon was down is filtered out (no ghost
    # row), and the replayed row is marked stale so it does not claim freshness.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    main.refresh.save_snapshot(
        {
            "sessions": [
                {"session_id": "s1", "freshness": {"refreshed_at": "t", "stale": False}, "repos": []},
                {"session_id": "gone", "repos": []},
            ],
            "auth": {"present": True},
        }
    )
    seen = {}

    def fake_params(snapshot, **_kw):
        seen["ids"] = [s["session_id"] for s in snapshot["sessions"]]
        seen["stale"] = [s.get("freshness", {}).get("stale") for s in snapshot["sessions"]]
        return [
            {"session_id": s["session_id"], "slot": "row-badge", "payload": {"items": []}} for s in snapshot["sessions"]
        ]

    monkeypatch.setattr(main.uistate, "snapshot_ui_state_params", fake_params)
    sent = []
    rt = main.Runtime(send=sent.append)
    rt._replay_cached([{"id": "s1", "project_path": "/a"}])
    assert seen["ids"] == ["s1"]
    assert seen["stale"] == [True]
    pushes = [m for m in sent if m["method"] == "ui.state.set"]
    assert [p["params"]["session_id"] for p in pushes] == ["s1"]
    assert rt.pushed_session_ids == {"s1"}


def test_replay_cached_without_cache_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    sent = []
    rt = main.Runtime(send=sent.append)
    rt._replay_cached([{"id": "s1", "project_path": "/a"}])
    assert sent == []
    assert rt.pushed_session_ids == set()


def test_full_refresh_persists_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    snap = {"sessions": [{"session_id": "s1", "repos": []}], "auth": {"present": True}}
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda *_a, **_kw: snap)
    monkeypatch.setattr(main.uistate, "snapshot_ui_state_params", lambda *_a, **_kw: [])
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    rt.run_refresh(sessions=[{"id": "s1"}])
    assert main.refresh.load_snapshot() == snap


def test_scoped_refresh_does_not_persist(tmp_path, monkeypatch):
    # A scoped (single-pane) refresh is partial; it must not overwrite the full
    # aggregate cache used for the next restart's repaint.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    snap = {"sessions": [{"session_id": "s1", "repos": []}], "auth": {"present": True}}
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda *_a, **_kw: snap)
    monkeypatch.setattr(main.uistate, "snapshot_ui_state_params", lambda *_a, **_kw: [])
    rt = main.Runtime(send=lambda _m: None)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    rt.run_refresh(sessions=[{"id": "s1"}], only_session="s1")
    assert main.refresh.load_snapshot() is None


def test_startup_replays_cached_before_network(monkeypatch):
    # Acceptance #1: the cached repaint happens before the (network) refresh.
    order = []
    rt = main.Runtime(send=lambda _m: None, stdin=iter([]))
    rt.call_host = lambda *_a, **_kw: {"value": True}
    rt.list_sessions = lambda *_a, **_kw: [{"id": "s1", "project_path": "/a"}]
    monkeypatch.setattr(rt, "_replay_cached", lambda _sessions: order.append("replay"))
    monkeypatch.setattr(rt, "run_refresh", lambda *_a, **_kw: order.append("refresh"))
    rt.run()
    assert order == ["replay", "refresh"]


def test_select_pr_survives_a_drifted_cached_snapshot(monkeypatch):
    # handle_inbound wraps only its dispatch call, and run() does not wrap
    # handle_inbound, so an unguarded raise here would end the main loop and freeze
    # every session's pane. A drifted cached snapshot (a truthy non-dict `error`,
    # which the mapper reads with .get) is a reachable source of one.
    sent: list = []
    rt = main.Runtime(send=sent.append)
    rt.call_host = lambda *_a, **_kw: {"value": True}
    monkeypatch.setattr(main.refresh, "build_snapshot", lambda _sessions, **_k: _two_pr_snapshot())
    rt.run_refresh(sessions=[{"id": "s1"}], force=True)
    rt._last_snapshot = {
        "sessions": [{"session_id": "s1", "repos": [{"name": "r", "repo": "o/r", "error": "not-a-dict"}]}],
        "auth": {"present": True},
    }
    sent.clear()

    rt.handle_inbound({"method": main.SELECT_PR_METHOD, "params": {"session_id": "s1", "pr": "o/r#1"}})

    # The click is absorbed: the selection is still recorded, nothing was pushed,
    # and crucially no exception escaped to the caller.
    assert rt._selected_pr == {"s1": "o/r#1"}
    assert not [m for m in sent if m["method"] == "ui.state.set"]

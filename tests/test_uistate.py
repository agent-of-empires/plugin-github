"""Aggregate snapshot -> ui.state.set params mapping (pure)."""

from aoe_github_plugin import uistate


def _pull(number=7, draft=False, title="t"):
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": title,
        "state": "open",
        "draft": draft,
    }


def _repo(name="r", repo="o/r", branch="feature", pulls=(), error=None):
    return {
        "path": f"/ws/{name}",
        "name": name,
        "repo": repo,
        "branch": branch,
        "pulls": list(pulls),
        "error": error,
    }


def _session(session_id="s1", title="sess", repos=()):
    return {"session_id": session_id, "title": title, "project_path": "/ws", "repos": list(repos)}


def _snapshot(*sessions):
    return {"sessions": list(sessions)}


def _badge(params, sid="s1"):
    return next(p for p in params if p["slot"] == "row-badge" and p["session_id"] == sid)


def _pane(params, sid="s1"):
    return next(p for p in params if p["slot"] == "detail-panel" and p["session_id"] == sid)


def test_each_session_gets_a_badge_and_a_pane_with_session_id():
    params = uistate.snapshot_ui_state_params(_snapshot(_session("s1"), _session("s2")))
    badges = {p["session_id"] for p in params if p["slot"] == "row-badge"}
    panes = {p["session_id"] for p in params if p["slot"] == "detail-panel"}
    assert badges == {"s1", "s2"}
    assert panes == {"s1", "s2"}
    assert all("session_id" in p for p in params)


def test_no_global_status_bar():
    params = uistate.snapshot_ui_state_params(_snapshot(_session()))
    assert all(p["slot"] != "status-bar" for p in params)


def test_badge_item_per_pr_with_icon_tone_href():
    session = _session(
        repos=[
            _repo(name="a", pulls=[_pull(number=1)]),
            _repo(name="b", pulls=[_pull(number=2, draft=True)]),
            _repo(name="c"),  # no PR -> no badge
        ]
    )
    items = _badge(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["items"]
    assert len(items) == 2  # the no-PR repo is omitted from the row
    assert items[0]["icon"] == "git-pull-request-arrow"
    assert items[0]["tone"] == "success"
    assert items[0]["href"] == "https://github.com/o/r/pull/1"
    assert items[1]["icon"] == "git-pull-request-draft"
    assert items[1]["tone"] == "warn"


def test_error_repo_gets_alert_badge_no_href():
    session = _session(repos=[_repo(error={"kind": "rate_limited", "hint": "rate limited"})])
    items = _badge(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["items"]
    assert items[0]["icon"] == "circle-alert"
    assert items[0]["tone"] == "danger"
    assert "href" not in items[0]


def test_badge_items_empty_when_no_prs():
    items = _badge(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo()]))))["payload"]["items"]
    assert items == []


def test_pane_has_heading_and_a_row_per_repo():
    session = _session(repos=[_repo(name="a", pulls=[_pull(number=9, title="Add x")]), _repo(name="b")])
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]
    assert blocks[0] == {"kind": "heading", "text": "GitHub"}
    rows = [b for b in blocks if b["kind"] == "row"]
    assert [r["label"] for r in rows] == ["a", "b"]
    assert rows[0]["value"] == "PR #9 Add x"
    assert rows[0]["href"] == "https://github.com/o/r/pull/9"
    assert rows[1]["value"] == "no open PR"


def test_pane_payload_carries_title():
    pane = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))
    assert pane["payload"]["title"] == "GitHub"


def test_empty_snapshot_yields_no_pushes():
    assert uistate.snapshot_ui_state_params({}) == []


def test_session_without_id_is_skipped():
    assert uistate.snapshot_ui_state_params(_snapshot(_session(session_id=None))) == []

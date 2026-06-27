"""Aggregate snapshot -> ui.state.set params mapping (pure)."""

import json

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
    return next(p for p in params if p["slot"] == "pane" and p["session_id"] == sid)


def test_each_session_gets_a_badge_and_a_pane_with_session_id():
    params = uistate.snapshot_ui_state_params(_snapshot(_session("s1"), _session("s2")))
    badges = {p["session_id"] for p in params if p["slot"] == "row-badge"}
    panes = {p["session_id"] for p in params if p["slot"] == "pane"}
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


def test_pane_ends_with_a_refresh_action():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))["payload"]["blocks"]
    action = blocks[-1]
    assert action["kind"] == "action"
    assert action["method"] == "github.refresh"
    assert action["label"] == "Refresh"


def test_pane_payload_carries_title_and_default_location():
    pane = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))
    assert pane["payload"]["title"] == "GitHub"
    assert pane["payload"]["default_location"] == "right"
    assert pane["payload"]["icon"] == "message-square-diff"


def test_empty_snapshot_yields_no_pushes():
    assert uistate.snapshot_ui_state_params({}) == []


def test_session_without_id_is_skipped():
    assert uistate.snapshot_ui_state_params(_snapshot(_session(session_id=None))) == []


# --- rich (token) rendering ---


def _rich_pull(state="OPEN", merged=False, review="approved", checks=None, comments=None):
    number = 5
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": "T",
        "state": state,
        "draft": False,
        "merged": merged,
        "review_state": review,
        "checks": checks,
        "comments": comments or {"unresolved": 0, "items": []},
    }


def _auth_snapshot(*sessions, present=True):
    return {"sessions": list(sessions), "auth": {"present": present}}


def test_merged_pr_uses_purple_color_and_is_omitted_from_badge():
    session = _session(repos=[_repo(name="a", pulls=[_rich_pull(state="MERGED", merged=True)])])
    params = uistate.snapshot_ui_state_params(_auth_snapshot(session))
    # Badge: merged-only repo contributes no actionable badge.
    assert _badge(params)["payload"]["items"] == []
    # Pane: headline row is purple with the merge icon.
    head = next(b for b in _pane(params)["payload"]["blocks"] if b.get("kind") == "row")
    assert head["icon"] == "git-merge"
    assert head["color"] == uistate.MERGED_COLOR
    assert head["value"].startswith("MERGED #5")


def test_pane_renders_review_checks_and_comments():
    checks = {"state": "failing", "runs": [{"name": "test", "state": "failing", "url": "https://ci/1"}]}
    comments = {
        "unresolved": 1,
        "items": [{"author": "al", "body": "fix", "path": "a.py", "line": 2, "url": "https://c/1", "resolved": False}],
    }
    pull = _rich_pull(review="changes-requested", checks=checks, comments=comments)
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    review_row = next(b for b in blocks if b.get("kind") == "row" and b.get("label") == "Review")
    assert review_row["value"] == "changes requested"
    assert review_row["tone"] == "danger"
    sections = [b for b in blocks if b.get("kind") == "section"]
    checks_section = next(s for s in sections if s["title"].startswith("Checks"))
    assert checks_section["children"][0]["label"] == "test"
    assert checks_section["children"][0]["tone"] == "danger"
    comments_section = next(s for s in sections if s["title"].startswith("Unresolved comments"))
    comment = comments_section["children"][0]
    assert comment["kind"] == "comment"
    assert comment["author"] == "al"
    assert comment["href"] == "https://c/1"
    # Both sections fold. These checks are failing, so the section stays open;
    # the comments section is open because there are unresolved comments.
    assert checks_section["collapsible"] is True
    assert "collapsed" not in checks_section
    assert comments_section["collapsible"] is True
    assert "collapsed" not in comments_section


def test_passing_checks_section_starts_collapsed():
    checks = {"state": "succeeded", "runs": [{"name": "test", "state": "succeeded"}]}
    pull = _rich_pull(review="approved", checks=checks, comments={"unresolved": 0, "items": []})
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    checks_section = next(b for b in blocks if b.get("kind") == "section" and b["title"].startswith("Checks"))
    assert checks_section["collapsed"] is True


def test_pane_payload_stays_under_host_size_cap():
    # A many-repo workspace with long comments would blow the 8KB/entry host cap;
    # the pane must trim to fit (and keep the heading + refresh action).
    big_comment = {
        "unresolved": 1,
        "items": [{"author": "a", "body": "x" * 300, "path": "p.py", "line": 1, "resolved": False}],
    }
    repos = [_repo(name=f"r{i}", repo=f"o/r{i}", pulls=[_rich_pull(comments=big_comment)]) for i in range(40)]
    pane = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=repos))))
    blocks = pane["payload"]["blocks"]
    assert len(json.dumps(blocks)) <= uistate._PANE_BUDGET + 200  # under cap (+ truncation note slack)
    assert blocks[0] == {"kind": "heading", "text": "GitHub"}
    assert blocks[-1]["method"] == "github.refresh"
    assert any(b.get("text", "").startswith("more not shown") for b in blocks)


def test_no_token_note_shown_only_when_a_github_repo_exists():
    # github repo + no token -> a warn note banner.
    with_repo = uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo()]), present=False))
    notes = [b for b in _pane(with_repo)["payload"]["blocks"] if b.get("kind") == "note" and b.get("tone") == "warn"]
    assert len(notes) == 1
    # no github repo -> no nag.
    no_repo = uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(repo=None)]), present=False))
    assert not [b for b in _pane(no_repo)["payload"]["blocks"] if b.get("kind") == "note" and b.get("tone") == "warn"]

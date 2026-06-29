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


def _column(params, sid="s1"):
    return next(p for p in params if p["slot"] == "row-column" and p["session_id"] == sid)


def _sort_key(params):
    return next(p for p in params if p["slot"] == "sort-key")


def _rows(blocks):
    rows = []
    for block in blocks:
        if block.get("kind") == "row":
            rows.append(block)
        if block.get("kind") == "section":
            rows.extend(child for child in block.get("children", []) if child.get("kind") == "row")
    return rows


def test_each_session_gets_a_badge_and_a_pane_with_session_id():
    params = uistate.snapshot_ui_state_params(_snapshot(_session("s1"), _session("s2")))
    badges = {p["session_id"] for p in params if p["slot"] == "row-badge"}
    panes = {p["session_id"] for p in params if p["slot"] == "pane"}
    assert badges == {"s1", "s2"}
    assert panes == {"s1", "s2"}
    assert all("session_id" in p for p in params if p["slot"] != "sort-key")
    assert "session_id" not in _sort_key(params)


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


def test_badge_carries_top_level_href_to_primary_pr():
    # The `open_pr` command (open-ui-link on the row-badge) opens this href.
    session = _session(repos=[_repo(name="a", pulls=[_pull(number=7)])])
    payload = _badge(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]
    assert payload["href"] == "https://github.com/o/r/pull/7"


def test_badge_omits_href_when_no_pr():
    payload = _badge(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo()]))))["payload"]
    assert "href" not in payload


def test_badge_keeps_href_even_when_status_column_hidden():
    # show_column=False clears the row-column words, but the badge href (what the
    # open_pr command opens) must survive, so opening a PR never depends on the
    # status-text toggle.
    session = _session(repos=[_repo(name="a", pulls=[_pull(number=7)])])
    params = uistate.snapshot_ui_state_params(_snapshot(session), show_column=False)
    assert _badge(params)["payload"]["href"] == "https://github.com/o/r/pull/7"
    assert _column(params)["payload"] == {"text": "", "sort_value": uistate._ATTENTION_VISUAL["open"][0]}


def test_pane_has_heading_and_a_row_per_repo():
    session = _session(repos=[_repo(name="b"), _repo(name="a", pulls=[_pull(number=9, title="Add x")])])
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]
    assert blocks[0] == {"kind": "heading", "text": "GitHub"}
    rows = _rows(blocks)
    assert [r["label"] for r in rows] == ["a", "b"]
    assert rows[0]["value"] == "PR #9 Add x"
    assert rows[0]["href"] == "https://github.com/o/r/pull/9"
    assert rows[1]["value"] == "no open PR"
    section = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Repos without open PRs (1)")
    assert section["collapsed"] is True


def test_pane_ends_with_a_refresh_action():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))["payload"]["blocks"]
    action = blocks[-1]
    assert action["kind"] == "action"
    assert action["method"] == "github.refresh"
    assert action["label"] == "Refresh"


def test_pane_shows_freshness_before_refresh_action():
    session = _session(repos=[_repo()])
    session["freshness"] = {"refreshed_at": "2026-06-29T14:32:10Z", "stale": False}
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]

    assert blocks[-2] == {
        "kind": "row",
        "label": "Last refreshed",
        "value": "14:32 UTC",
        "icon": "clock",
        "tone": "neutral",
    }
    assert blocks[-1]["method"] == "github.refresh"


def test_pane_marks_stale_freshness_before_refresh_action():
    session = _session(repos=[_repo()])
    session["freshness"] = {"refreshed_at": "2026-06-29T14:20:03Z", "stale": True}
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]

    assert blocks[-2] == {
        "kind": "row",
        "label": "Last successful refresh",
        "value": "14:20 UTC",
        "icon": "clock",
        "tone": "warn",
    }
    assert blocks[-1]["method"] == "github.refresh"


def test_pane_omits_freshness_without_timestamp():
    session = _session(repos=[_repo()])
    session["freshness"] = {"stale": False}
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]

    assert not any(b.get("label") == "Last refreshed" for b in blocks)
    assert blocks[-1]["method"] == "github.refresh"


def test_pane_payload_carries_title_and_default_location():
    pane = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))
    assert pane["payload"]["title"] == "GitHub"
    assert pane["payload"]["default_location"] == "right"
    assert pane["payload"]["icon"] == "message-square-diff"


def test_empty_snapshot_yields_no_pushes():
    assert uistate.snapshot_ui_state_params({}) == []


def test_empty_sessions_snapshot_emits_sort_key_only():
    params = uistate.snapshot_ui_state_params({"sessions": []})
    assert params == [
        {
            "slot": uistate.SORT_KEY_SLOT[0],
            "id": uistate.SORT_KEY_SLOT[1],
            "payload": uistate.SORT_KEY_PAYLOAD,
        }
    ]


def test_session_without_id_is_skipped():
    params = uistate.snapshot_ui_state_params(_snapshot(_session(session_id=None)))
    assert params == [
        {
            "slot": uistate.SORT_KEY_SLOT[0],
            "id": uistate.SORT_KEY_SLOT[1],
            "payload": uistate.SORT_KEY_PAYLOAD,
        }
    ]


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


def test_merged_pr_suppresses_review_checks_and_comments():
    # A merged PR is terminal: the headline says MERGED, but its historical
    # review/CI/comment data must NOT render as active badges/sections.
    checks = {"state": "failing", "runs": [{"name": "test", "state": "failing", "url": "https://ci/1"}]}
    comments = {
        "unresolved": 1,
        "items": [{"author": "al", "body": "fix", "path": "a.py", "line": 2, "url": "https://c/1", "resolved": False}],
    }
    pull = _rich_pull(state="MERGED", merged=True, review="changes-requested", checks=checks, comments=comments)
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    # Headline still present and merged.
    head = next(b for b in blocks if b.get("kind") == "row" and b.get("label") == "r")
    assert head["value"].startswith("MERGED #5")
    # No active review row, no Checks section, no Unresolved-comments section.
    assert not [b for b in blocks if b.get("kind") == "row" and b.get("label") == "Review"]
    assert not [b for b in blocks if b.get("kind") == "section"]
    assert not [b for b in blocks if b.get("kind") == "comment"]


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
    # The section title carries the rollup's icon + tone for an at-a-glance state.
    assert checks_section["icon"] == "circle-x"
    assert checks_section["tone"] == "danger"
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


def test_pane_keeps_no_pr_repos_direct_when_nothing_is_actionable():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo(name="a"), _repo(name="b")]))))[
        "payload"
    ]["blocks"]
    rows = _rows(blocks)
    assert [r["label"] for r in rows] == ["a", "b"]
    assert not [b for b in blocks if b.get("title") == "Repos without open PRs (2)"]


def test_pane_treats_merged_only_repo_as_no_open_pr_for_ordering():
    session = _session(
        repos=[
            _repo(name="merged", pulls=[_rich_pull(state="MERGED", merged=True)]),
            _repo(name="open", pulls=[_pull(number=1)]),
        ]
    )
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(session)))["payload"]["blocks"]
    rows = _rows(blocks)
    assert [r["label"] for r in rows] == ["open", "merged"]
    section = next(b for b in blocks if b.get("title") == "Repos without open PRs (1)")
    assert section["collapsed"] is True


def test_required_rollup_keeps_optional_failure_visible():
    checks = {
        "state": "succeeded",
        "runs": [
            {"name": "required-build", "state": "succeeded", "required": True},
            {"name": "optional-lint", "state": "failing", "required": False},
        ],
    }
    pull = _rich_pull(review="approved", checks=checks, comments={"unresolved": 0, "items": []})
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    checks_section = next(b for b in blocks if b.get("kind") == "section" and b["title"].startswith("Checks"))
    assert "collapsed" not in checks_section
    rows = {child["label"]: child for child in checks_section["children"]}
    assert rows["required-build"]["sublabel"] == "required"
    assert rows["optional-lint"]["tone"] == "danger"
    assert rows["optional-lint"]["sublabel"] == "optional"


def test_pane_payload_stays_under_host_size_cap():
    # A many-repo workspace with long comments would blow the 64KB/entry host cap;
    # the pane must trim to fit (and keep the heading + refresh action).
    big_comment = {
        "unresolved": 1,
        "items": [{"author": "a", "body": "x" * 2000, "path": "p.py", "line": 1, "resolved": False}],
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


# --- attention status in session rows (#36): badge + row-column ---


def _badge_items_for(repos, sid="s1"):
    params = uistate.snapshot_ui_state_params(_auth_snapshot(_session(session_id=sid, repos=repos)))
    return _badge(params, sid)["payload"]["items"]


def _column_payload(repos, sid="s1"):
    params = uistate.snapshot_ui_state_params(_auth_snapshot(_session(session_id=sid, repos=repos)))
    return _column(params, sid)["payload"]


def test_each_session_gets_a_row_column():
    params = uistate.snapshot_ui_state_params(_snapshot(_session("s1"), _session("s2")))
    cols = {p["session_id"] for p in params if p["slot"] == "row-column"}
    assert cols == {"s1", "s2"}


def test_snapshot_emits_global_sort_key():
    params = uistate.snapshot_ui_state_params(_snapshot(_session()))
    assert _sort_key(params) == {
        "slot": uistate.SORT_KEY_SLOT[0],
        "id": uistate.SORT_KEY_SLOT[1],
        "payload": uistate.SORT_KEY_PAYLOAD,
    }


def test_badge_emits_pr_review_ci_comment_chip_sequence():
    checks = {"state": "failing", "runs": [{"name": "t", "state": "failing"}]}
    comments = {"unresolved": 3, "items": [{"author": "a", "body": "x", "resolved": False}]}
    items = _badge_items_for([_repo(pulls=[_rich_pull(review="changes-requested", checks=checks, comments=comments)])])
    # PR affordance, then review (changes-requested), CI (failing), comments.
    assert [i["icon"] for i in items] == ["git-pull-request-arrow", "circle-x", "circle-x", "message-square"]
    assert [i["tone"] for i in items] == ["success", "danger", "danger", "warn"]
    assert all(i["href"] == "https://github.com/o/r/pull/5" for i in items)


def test_badge_healthy_pr_has_no_comment_chip():
    checks = {"state": "succeeded", "runs": [{"name": "t", "state": "succeeded"}]}
    items = _badge_items_for([_repo(pulls=[_rich_pull(review="approved", checks=checks)])])
    # PR + approved review + passing CI; no comment chip when nothing unresolved.
    assert [i["icon"] for i in items] == ["git-pull-request-arrow", "badge-check", "circle-check"]
    assert "message-square" not in [i["icon"] for i in items]


def test_badge_no_token_pr_is_chip_only():
    # No rich fields -> the PR chip alone, no review/CI/comment noise.
    items = _badge_items_for([_repo(pulls=[_pull()])])
    assert [i["icon"] for i in items] == ["git-pull-request-arrow"]


def test_badge_draft_is_chip_only_no_rich_chips():
    checks = {"state": "failing", "runs": []}
    pull = _rich_pull(review="changes-requested", checks=checks)
    pull["draft"] = True
    items = _badge_items_for([_repo(pulls=[pull])])
    assert [i["icon"] for i in items] == ["git-pull-request-draft"]
    assert items[0]["tone"] == "warn"


def test_badge_concatenates_chips_across_repos():
    checks = {"state": "failing", "runs": []}
    repos = [
        _repo(name="a", repo="o/a", pulls=[_rich_pull(review="approved")]),
        _repo(name="b", repo="o/b", pulls=[_rich_pull(review="approved", checks=checks)]),
    ]
    icons = [i["icon"] for i in _badge_items_for(repos)]
    # a: PR + approved ; b: PR + approved + failing CI.
    assert icons == ["git-pull-request-arrow", "badge-check", "git-pull-request-arrow", "badge-check", "circle-x"]


def test_badge_chip_tooltips_name_repo_and_state():
    checks = {"state": "failing", "runs": []}
    items = _badge_items_for([_repo(name="a", pulls=[_rich_pull(review="changes-requested", checks=checks)])])
    tips = [i["tooltip"] for i in items]
    assert tips[0].startswith("a: PR #5")
    assert "changes requested" in tips[1]
    assert "CI failing" in tips[2]


# --- chip-visibility settings (#36 follow-up): per-category toggles ---


def _badge_icons_with(repos, chips_on, sid="s1"):
    params = uistate.snapshot_ui_state_params(_auth_snapshot(_session(session_id=sid, repos=repos)), chips_on=chips_on)
    return [i["icon"] for i in _badge(params, sid)["payload"]["items"]]


def _column_with(repos, chips_on, sid="s1"):
    params = uistate.snapshot_ui_state_params(_auth_snapshot(_session(session_id=sid, repos=repos)), chips_on=chips_on)
    return _column(params, sid)["payload"]


def test_ci_toggle_off_hides_ci_chip():
    checks = {"state": "failing", "runs": []}
    repos = [_repo(pulls=[_rich_pull(review="approved", checks=checks)])]
    assert "circle-x" in _badge_icons_with(repos, frozenset({"review", "ci", "comments"}))
    assert _badge_icons_with(repos, frozenset({"review", "comments"})) == ["git-pull-request-arrow", "badge-check"]


def test_review_toggle_off_hides_review_chip():
    repos = [_repo(pulls=[_rich_pull(review="changes-requested")])]
    assert _badge_icons_with(repos, frozenset({"ci", "comments"})) == ["git-pull-request-arrow"]


def test_comments_toggle_off_hides_comment_chip():
    comments = {"unresolved": 2, "items": [{"author": "a", "body": "x", "resolved": False}]}
    repos = [_repo(pulls=[_rich_pull(review="approved", comments=comments)])]
    icons = _badge_icons_with(repos, frozenset({"review", "ci"}))
    assert "message-square" not in icons
    assert icons == ["git-pull-request-arrow", "badge-check"]


def test_column_skips_disabled_category_and_falls_through():
    checks = {"state": "failing", "runs": []}
    repos = [_repo(pulls=[_rich_pull(review="changes-requested", checks=checks)])]
    # review off, ci on: the changes-requested signal is skipped, CI failing wins.
    assert _column_with(repos, frozenset({"ci"}))["text"] == "CI failing"
    # everything off: no rich signal survives, so it degrades to healthy open.
    assert _column_with(repos, frozenset())["text"] == "open PR"


def test_show_column_false_clears_status_text_but_keeps_badge():
    repos = [_repo(pulls=[_rich_pull(review="changes-requested")])]
    params = uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=repos)), show_column=False)
    # The row-column hides words but keeps the sort scalar; the badge still has chips.
    assert _column(params)["payload"] == {"text": "", "sort_value": uistate._ATTENTION_VISUAL["changes-requested"][0]}
    assert _badge(params)["payload"]["items"]


def test_column_summarizes_top_attention_with_text_and_tone():
    payload = _column_payload([_repo(pulls=[_rich_pull(review="changes-requested")])])
    assert payload["text"] == "changes requested"
    assert payload["tone"] == "danger"
    assert "icon" not in payload
    assert "href" not in payload
    assert payload["sort_value"] == uistate._ATTENTION_VISUAL["changes-requested"][0]


def test_column_picks_most_urgent_repo_across_workspace():
    comments = {"unresolved": 1, "items": [{"author": "a", "body": "x", "resolved": False}]}
    repos = [
        _repo(name="a", repo="o/a", pulls=[_rich_pull(review="approved", comments=comments)]),  # unresolved
        _repo(name="b", repo="o/b", pulls=[_rich_pull(review="changes-requested")]),  # outranks
    ]
    assert _column_payload(repos)["text"] == "changes requested"


def test_column_shows_healthy_state_not_empty():
    payload = _column_payload([_repo(pulls=[_rich_pull(review="approved")])])
    assert payload["text"] == "approved"
    assert payload["tone"] == "success"


def test_column_is_empty_when_no_prs_to_clear_stale_state():
    assert _column_payload([_repo()]) == {}


def test_column_is_empty_for_merged_only_repo():
    assert _column_payload([_repo(pulls=[_rich_pull(state="MERGED", merged=True)])]) == {}


def test_column_surfaces_repo_error():
    payload = _column_payload([_repo(error={"kind": "rate_limited", "hint": "rate limited"})])
    assert payload["text"] == "error"
    assert payload["tone"] == "danger"
    assert "icon" not in payload
    assert payload["sort_value"] == uistate._ATTENTION_VISUAL["error"][0]


def test_row_degrades_without_token():
    # No rich fields (no-token shape): badge + column fall back to plain open,
    # never inventing a reviewed/healthy state they cannot see.
    params = uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo(pulls=[_pull()])])))
    assert _badge(params)["payload"]["items"][0]["icon"] == "git-pull-request-arrow"
    assert _column(params)["payload"]["text"] == "open PR"

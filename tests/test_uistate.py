"""Aggregate snapshot -> ui.state.set params mapping (pure)."""

import os
import json
import time

import pytest

from aoe_github_plugin import uistate


@pytest.fixture(autouse=True)
def _restore_process_tz():
    """Reset the process timezone after each test. ``monkeypatch`` reverts the
    ``TZ`` env var but never re-runs ``time.tzset()``, so without this the tz a
    test pinned would leak into later tests until one reset it."""
    original = os.environ.get("TZ")
    yield
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _set_tz(monkeypatch, tz):
    """Pin the process local timezone so ``astimezone()`` is deterministic."""
    monkeypatch.setenv("TZ", tz)
    time.tzset()


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


def test_pane_leads_with_a_pr_selector_and_folds_repos_without_prs():
    session = _session(repos=[_repo(name="b"), _repo(name="a", pulls=[_pull(number=9, title="Add x")])])
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]["blocks"]
    # No heading block: the dock tab already names the pane.
    assert blocks[0]["kind"] == "section"
    selector = blocks[0]["children"]
    assert [r["prefix"] for r in selector] == ["#9"]
    assert selector[0]["label"] == "Add x"
    assert selector[0]["href"] == "https://github.com/o/r/pull/9"
    # The only PR is selected by default, and clicking a row names it by key.
    assert selector[0]["selected"] is True
    assert selector[0]["method"] == uistate.SELECT_METHOD
    assert selector[0]["params"] == {"pr": "o/r#9"}
    other = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Other repos (1)")
    assert other["collapsed"] is True
    assert other["children"][0]["value"] == "no open PR"


def test_pane_ends_with_a_refresh_action():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))["payload"]["blocks"]
    action = blocks[-1]
    assert action["kind"] == "action"
    assert action["method"] == "github.refresh"
    assert action["label"] == "Refresh"


def test_pane_footer_carries_freshness_and_the_selected_verdict(monkeypatch):
    # Freshness lives in the pinned footer, not a block, so it stays visible while
    # the block list scrolls.
    _set_tz(monkeypatch, "UTC")
    session = _session(repos=[_repo(pulls=[_rich_pull(review="approved")])])
    session["freshness"] = {"refreshed_at": "2026-06-29T14:32:10Z", "stale": False}
    payload = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(session)))["payload"]
    assert payload["footer"] == {
        "text": "refreshed 14:32",
        "icon": "refresh-cw",
        "value": "ready",
        "tone": "success",
    }
    assert payload["blocks"][-1]["method"] == "github.refresh"


def test_pane_footer_marks_stale_freshness(monkeypatch):
    _set_tz(monkeypatch, "UTC")
    session = _session(repos=[_repo()])
    session["freshness"] = {"refreshed_at": "2026-06-29T14:20:03Z", "stale": True}
    payload = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]
    # No PR in this session, so the footer carries only the refresh half.
    assert payload["footer"] == {"text": "last good refresh 14:20", "icon": "clock"}


def test_format_refreshed_at_converts_utc_to_local(monkeypatch):
    # 14:32 UTC is 10:32 in America/New_York (EDT, UTC-4) on this date.
    _set_tz(monkeypatch, "America/New_York")
    assert uistate._format_refreshed_at("2026-06-29T14:32:10Z") == "10:32"


def test_format_refreshed_at_treats_naive_stamp_as_utc(monkeypatch):
    _set_tz(monkeypatch, "America/New_York")
    assert uistate._format_refreshed_at("2026-06-29T14:32:10") == "10:32"


def test_format_refreshed_at_rejects_garbage():
    assert uistate._format_refreshed_at("not-a-timestamp") is None
    assert uistate._format_refreshed_at(None) is None


def test_pane_omits_freshness_without_timestamp():
    session = _session(repos=[_repo()])
    session["freshness"] = {"stale": False}
    payload = _pane(uistate.snapshot_ui_state_params(_snapshot(session)))["payload"]
    # Nothing to pin: no timestamp and no PR verdict, so no footer at all.
    assert "footer" not in payload
    assert payload["blocks"][-1]["method"] == "github.refresh"


def test_pane_payload_carries_title_and_default_location():
    pane = _pane(uistate.snapshot_ui_state_params(_snapshot(_session())))
    assert pane["payload"]["title"] == "GitHub"
    assert pane["payload"]["default_location"] == "right"
    # No per-pane icon: the manifest's icon_asset/icon (api_version 7) is the
    # plugin's identity, and always wins in the activity bar/dock tab.
    assert "icon" not in pane["payload"]


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


def _rich_pull(  # noqa: PLR0913
    state="OPEN", merged=False, review="approved", checks=None, comments=None, merge_state=None, draft=False
):
    number = 5
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": "T",
        "state": state,
        "draft": draft,
        "merged": merged,
        "merge_state": merge_state,
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
    # Pane: the selector row is purple with the merge icon.
    head = _rows(_pane(params)["payload"]["blocks"])[0]
    assert head["icon"] == "git-merge"
    assert head["color"] == uistate.MERGED_COLOR
    assert head["prefix"] == "#5"


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
    # The selector still lists it, and its verdict says merged.
    assert _rows(blocks)[0]["icon"] == "git-merge"
    callout = next(b for b in blocks if b.get("kind") == "callout")
    assert callout["title"] == "Merged"
    # No Review or Checks card, and no unresolved comments: all of it is history.
    titles = {b.get("title") for b in blocks if b.get("kind") == "section"}
    assert "Review" not in titles
    assert "Checks" not in titles
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
    sections = [b for b in blocks if b.get("kind") == "section"]
    # No per-reviewer data here, so the Review card degrades to the aggregate state.
    review = next(s for s in sections if s.get("title") == "Review")
    assert review["value"] == "changes requested"
    assert review["value_tone"] == "danger"
    checks_section = next(s for s in sections if s.get("title") == "Checks")
    assert checks_section["children"][0]["label"] == "test"
    assert checks_section["badges"] == [{"text": "1 failing", "tone": "danger"}]
    assert checks_section["scroll"] is True
    # The blocking reason leads the pane as a callout with an inert button.
    callout = next(b for b in blocks if b.get("kind") == "callout")
    assert callout["title"] == "Changes requested"
    assert callout["tone"] == "danger"
    assert callout["actions"][0]["disabled"] is True
    # The count pills carry the rollup state now, so the title needs no icon/tone.
    assert checks_section["children"][0]["tone"] == "danger"
    assert "icon" not in checks_section
    comments_section = next(s for s in sections if s.get("title", "").startswith("Unresolved comments"))
    comment = comments_section["children"][0]
    assert comment["kind"] == "comment"
    assert comment["author"] == "al"
    assert comment["href"] == "https://c/1"
    # The Checks card no longer folds as a whole (only its passing group does), so
    # a failing check can never be hidden behind a collapsed header. The comments
    # section still folds, and stays open because there are unresolved comments.
    assert "collapsible" not in checks_section
    assert comments_section["collapsible"] is True
    assert "collapsed" not in comments_section


def test_selection_points_the_detail_at_the_chosen_pr_and_survives_nothing_else():
    a = _rich_pull(review="approved")
    a["number"] = 1
    b = _rich_pull(review="changes-requested")
    b["number"] = 2
    session = _session(repos=[_repo(pulls=[a, b])])
    snapshot = _auth_snapshot(session)

    # Default: the most-actionable PR (changes-requested outranks approved).
    default_pane = _pane(uistate.snapshot_ui_state_params(snapshot))["payload"]
    assert next(x for x in default_pane["blocks"] if x.get("kind") == "callout")["title"] == "Changes requested"

    # Choosing the other PR re-points the detail without touching the selector order.
    chosen = _pane(uistate.snapshot_ui_state_params(snapshot, selected={"s1": "o/r#1"}))["payload"]
    assert next(x for x in chosen["blocks"] if x.get("kind") == "callout")["title"] == "Ready to merge"
    selector = chosen["blocks"][0]["children"]
    assert [r["params"]["pr"] for r in selector] == ["o/r#2", "o/r#1"]
    assert [r["selected"] for r in selector] == [False, True]

    # A key naming a PR that is no longer in the snapshot falls back to the top of
    # the list rather than stranding the pane on a dead selection.
    stale = _pane(uistate.snapshot_ui_state_params(snapshot, selected={"s1": "o/r#999"}))["payload"]
    assert stale["blocks"][0]["children"][0]["selected"] is True


def test_review_card_lists_each_reviewer_with_initials():
    pull = _rich_pull(review="changes-requested")
    pull["reviewers"] = [
        {"name": "Nate Brake", "state": "approved"},
        {"name": "njbrake", "state": "changes-requested"},
    ]
    pull["review_summary"] = "changes requested"
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    review = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Review")
    assert review["value"] == "changes requested"
    assert [(r["avatar"], r["label"], r["value"]) for r in review["children"]] == [
        ("NB", "Nate Brake", "approved"),
        ("NJ", "njbrake", "changes requested"),
    ]


def test_diff_and_linked_issues_share_a_columns_row():
    pull = _rich_pull(review="approved")
    pull["diff"] = {"added": 842, "removed": 317, "files": 18}
    pull["issues"] = [{"number": 3180, "title": "Stale daemon", "url": "https://gh/i/3180", "state": "open"}]
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    columns = next(b for b in blocks if b.get("kind") == "columns")
    diff, linked = columns["children"]
    assert diff["badges"] == [{"text": "+842", "tone": "success"}, {"text": "-317", "tone": "danger"}]
    bar = diff["children"][0]
    assert bar["kind"] == "bar"
    assert [s["value"] for s in bar["segments"]] == [842, 317]
    assert bar["caption"] == "18 files"
    assert linked["children"][0]["prefix"] == "#3180"
    assert linked["children"][0]["href"] == "https://gh/i/3180"

    # With no linked issues the Diff card is the lone column, so it spans the pane.
    pull["issues"] = []
    solo = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    assert len(next(b for b in solo if b.get("kind") == "columns")["children"]) == 1


def test_activity_card_renders_timeline_rows_newest_first(monkeypatch):
    _set_tz(monkeypatch, "UTC")
    pull = _rich_pull(review="approved")
    pull["timeline"] = [
        {"kind": "commit", "text": "njbrake pushed 4f9a1c2", "at": "2026-06-29T12:04:00Z"},
        {"kind": "comment", "text": "seluj78 commented", "at": "2026-06-29T11:52:00Z"},
    ]
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    activity = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Activity")
    assert [(r["label"], r["value"]) for r in activity["children"]] == [
        ("njbrake pushed 4f9a1c2", "12:04"),
        ("seluj78 commented", "11:52"),
    ]


def test_merge_verdict_ladder_orders_blockers_by_who_can_clear_them():
    # One case per rung, so the precedence between them is pinned in one place.
    running = {"state": "running", "runs": [{"name": "Cargo Test", "state": "running"}]}
    failing = {"state": "failing", "runs": [{"name": "Clippy", "state": "failing"}]}
    unresolved = {"unresolved": 2, "items": [{"author": "a", "body": "x", "resolved": False}]}
    cases = [
        (_rich_pull(draft=True, merge_state="conflicts"), "Draft", "draft"),
        (_rich_pull(review="approved", merge_state="conflicts"), "Conflicts with base", "conflicts"),
        (_rich_pull(review="changes-requested", checks=failing), "Changes requested", "changes requested"),
        (_rich_pull(review="approved", checks=failing), "1 check failing", "blocked"),
        (_rich_pull(review="approved", checks=running), "1 check running", "in progress"),
        (_rich_pull(review="approved", comments=unresolved), "2 unresolved comments", "unresolved"),
        (_rich_pull(review="approved"), "Ready to merge", "ready"),
        (_rich_pull(review="waiting"), "Awaiting review", "awaiting review"),
    ]
    for pull, title, short in cases:
        verdict = uistate._merge_verdict(pull)
        assert verdict["title"] == title, pull
        assert verdict["short"] == short, pull
    # Only the ready rung offers a live affordance, and it is a link out.
    assert uistate._merge_verdict(_rich_pull(review="approved"))["ready"] is True


def test_passing_checks_fold_into_a_collapsed_group():
    checks = {
        "state": "succeeded",
        "runs": [
            {"name": "test", "state": "succeeded", "duration": "48s"},
            {"name": "close-stale", "state": "succeeded", "skipped": True},
        ],
    }
    pull = _rich_pull(review="approved", checks=checks, comments={"unresolved": 0, "items": []})
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=[_repo(pulls=[pull])]))))["payload"][
        "blocks"
    ]
    checks_section = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Checks")
    # The card itself stays open; the passes fold into a nested group so the
    # attention rows (none here) would sit above them.
    assert "collapsed" not in checks_section
    group = checks_section["children"][0]
    assert group["title"] == "1 check passing"
    assert group["collapsed"] is True
    assert group["children"][0] == {"kind": "row", "label": "test", "mono": True, "value": "48s"}
    # A skipped run counts as passing for the rollup but is listed apart from it.
    assert checks_section["children"][1]["label"] == "1 skipped"
    assert checks_section["badges"] == [
        {"text": "1 passing", "tone": "success"},
        {"text": "1 skipped", "tone": "neutral"},
    ]


def test_pane_keeps_no_pr_repos_direct_when_nothing_is_actionable():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo(name="a"), _repo(name="b")]))))[
        "payload"
    ]["blocks"]
    rows = _rows(blocks)
    assert [r["label"] for r in rows] == ["a", "b"]
    assert not [b for b in blocks if b.get("title") == "Repos without open PRs (2)"]


def test_pane_selector_ranks_open_prs_above_merged_ones():
    session = _session(
        repos=[
            _repo(name="merged", repo="o/merged", pulls=[_rich_pull(state="MERGED", merged=True)]),
            _repo(name="open", repo="o/open", pulls=[_pull(number=1)]),
        ]
    )
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(session)))["payload"]["blocks"]
    selector = blocks[0]["children"]
    # The open PR outranks the merged one and is what the pane details by default.
    assert [r["params"]["pr"] for r in selector] == ["o/open#1", "o/merged#5"]
    assert selector[0]["selected"] is True
    assert selector[1]["selected"] is False


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
    checks_section = next(b for b in blocks if b.get("kind") == "section" and b.get("title") == "Checks")
    assert "collapsed" not in checks_section
    # The optional failure is an attention row, listed outright above the fold.
    failing = checks_section["children"][0]
    assert failing["label"] == "optional-lint"
    assert failing["tone"] == "danger"
    # The required pass folds into the passing group.
    group = checks_section["children"][1]
    assert group["title"] == "1 check passing"
    assert group["children"][0]["label"] == "required-build"


def test_pane_payload_stays_under_host_size_cap():
    # Detailing one selected PR bounds the pane by construction, but a single
    # pathological block (a review with hundreds of long comments) can still blow
    # the 64KB/entry host cap. It must be trimmed to fit, with the refresh action
    # and a truncation note surviving, rather than rejected and the pane blanked.
    many_comments = {
        "unresolved": 400,
        "items": [
            {"author": "a", "body": "x" * 2000, "path": f"p{i}.py", "line": i, "resolved": False} for i in range(400)
        ],
    }
    repos = [_repo(name=f"r{i}", repo=f"o/r{i}", pulls=[_rich_pull(comments=many_comments)]) for i in range(4)]
    pane = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=repos))))
    blocks = pane["payload"]["blocks"]
    assert len(json.dumps(blocks)) <= uistate._PANE_BUDGET + 200  # under cap (+ truncation note slack)
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


# --- merge conflicts (#80) ---


def test_conflict_outranks_review_and_ci_in_column():
    # A merge conflict is a hard blocker: it wins over changes-requested + failing
    # CI on the same PR and drives the row-column summary.
    checks = {"state": "failing", "runs": []}
    pull = _rich_pull(review="changes-requested", checks=checks, merge_state="conflicts")
    payload = _column_payload([_repo(pulls=[pull])])
    assert payload["text"] == "conflicts"
    assert payload["tone"] == "danger"
    assert payload["sort_value"] == uistate._ATTENTION_VISUAL["conflicts"][0]


def test_conflict_chip_is_ungated_by_categories():
    # Conflicts are never user-suppressible: the danger chip shows even with every
    # toggle category disabled.
    repos = [_repo(pulls=[_rich_pull(review="approved", merge_state="conflicts")])]
    assert "triangle-alert" in _badge_icons_with(repos, frozenset())


def _callout(repos):
    blocks = _pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=repos))))["payload"]["blocks"]
    return next(b for b in blocks if b.get("kind") == "callout")


def test_conflict_becomes_the_pane_verdict():
    # A conflict is a hard blocker no reviewer can clear, so it outranks the
    # changes-requested decision in the verdict.
    pull = _rich_pull(review="changes-requested", merge_state="conflicts")
    pull["base"] = "main"
    callout = _callout([_repo(pulls=[pull])])
    assert callout["title"] == "Conflicts with base"
    assert callout["detail"] == "This branch conflicts with main."
    assert callout["tone"] == "danger"


def test_clean_pr_has_no_conflict_chip_and_reads_ready():
    pull = _rich_pull(review="approved", merge_state=None)
    repos = [_repo(pulls=[pull])]
    assert "triangle-alert" not in _badge_icons_with(repos, frozenset({"review", "ci", "comments"}))
    callout = _callout(repos)
    assert callout["title"] == "Ready to merge"
    assert callout["tone"] == "success"
    # Read-only: the primary affordance links out to GitHub rather than merging.
    action = callout["actions"][0]
    assert action["variant"] == "primary"
    assert action["href"] == "https://github.com/o/r/pull/5"
    assert "disabled" not in action


def test_draft_conflict_asymmetry_quiet_badge_visible_pane_verdict():
    # A draft keeps the established asymmetry: the session badge stays draft-only
    # (WIP, no conflict chip), but the pane still says the branch is blocked.
    pull = _rich_pull(draft=True, merge_state="conflicts")
    repos = [_repo(pulls=[pull])]
    assert _badge_icons_with(repos, frozenset({"review", "ci", "comments"})) == ["git-pull-request-draft"]
    # Draft is the more actionable thing to say: open it before worrying about the
    # conflict, and the conflict signal still rides the selector row's glyph strip.
    callout = _callout(repos)
    assert callout["title"] == "Draft"
    row = _rows(_pane(uistate.snapshot_ui_state_params(_auth_snapshot(_session(repos=repos))))["payload"]["blocks"])[0]
    assert any(b.get("icon") == "triangle-alert" for b in row["badges"])


def test_merged_pr_verdict_replaces_the_conflict_signal():
    # Merged PRs suppress all live detail, including a stale conflict signal.
    pull = _rich_pull(state="MERGED", merged=True, merge_state="conflicts")
    assert _callout([_repo(pulls=[pull])])["title"] == "Merged"


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


def _rate_limited_snapshot(budget, *, repos=None):
    repos = repos if repos is not None else [_repo(pulls=[_pull()])]
    return {"sessions": [_session(repos=repos)], "rate_limit": {"budget": budget, "seconds": 100, "reset_known": False}}


def test_pane_shows_rate_limit_note_on_background_snapshot():
    # #62: the always-on rate_limit snapshot key surfaces a warn note in the pane,
    # not only on a forced-refresh toast.
    params = uistate.snapshot_ui_state_params(_rate_limited_snapshot("graphql"))
    blocks = _pane(params)["payload"]["blocks"]
    notes = [b for b in blocks if b.get("kind") == "note"]
    assert any(b["tone"] == "warn" and "GraphQL" in b["text"] for b in notes)


def test_rate_limit_note_wording_is_budget_aware():
    def note_text(budget):
        blocks = _pane(uistate.snapshot_ui_state_params(_rate_limited_snapshot(budget)))["payload"]["blocks"]
        return next(b["text"] for b in blocks if b.get("kind") == "note")

    assert "still updates" in note_text("graphql")  # REST-driven list stays fresh
    assert "list may be stale" in note_text("rest")
    assert note_text("mixed") != note_text("graphql")


def test_no_rate_limit_note_when_clear():
    blocks = _pane(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo(pulls=[_pull()])]))))["payload"][
        "blocks"
    ]
    assert not [b for b in blocks if b.get("kind") == "note"]


def test_rate_limit_note_suppressed_without_github_repo():
    # A non-github checkout (repo=None) is not gated by GitHub limits, so no note.
    repos = [_repo(repo=None)]
    blocks = _pane(uistate.snapshot_ui_state_params(_rate_limited_snapshot("graphql", repos=repos)))["payload"][
        "blocks"
    ]
    assert not [b for b in blocks if b.get("kind") == "note"]

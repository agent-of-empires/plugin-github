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
    return {
        "session_id": session_id,
        "title": title,
        "project_path": "/ws",
        "repos": list(repos),
    }


def _snapshot(*sessions):
    return {"sessions": list(sessions)}


def _badges(params):
    return [p for p in params if p["slot"] == "row-badge"]


def _statusbar(params):
    return next(p for p in params if p["slot"] == "status-bar")


def test_shape_one_badge_per_session_plus_one_status_bar():
    params = uistate.snapshot_ui_state_params(_snapshot(_session("s1"), _session("s2")))
    badges = _badges(params)
    assert {b["session_id"] for b in badges} == {"s1", "s2"}
    assert all(b["id"] == "github_pr_badge" for b in badges)
    bar = _statusbar(params)
    assert "session_id" not in bar
    assert bar["id"] == "github_status"


def test_payload_is_text_payload_only():
    params = uistate.snapshot_ui_state_params(_snapshot(_session(repos=[_repo(pulls=[_pull()])])))
    for p in params:
        assert set(p["payload"]) == {"text", "tone", "tooltip"}


def test_tone_open_pr_is_success():
    session = _session(repos=[_repo(pulls=[_pull()])])
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["tone"] == "success"
    assert badge["payload"]["text"] == "1 PR"


def test_tone_draft_only_is_warn():
    session = _session(repos=[_repo(pulls=[_pull(draft=True)])])
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["tone"] == "warn"
    assert badge["payload"]["text"] == "1 draft"


def test_hard_error_is_danger_and_beats_an_open_pr():
    # A session with a real open PR in one repo AND a hard error in another must
    # surface danger, not a misleading green.
    session = _session(
        repos=[
            _repo(name="a", pulls=[_pull()]),
            _repo(name="b", error={"kind": "rate_limited", "hint": "rate limited"}),
        ]
    )
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["tone"] == "danger"
    assert badge["payload"]["text"] == "GitHub !"


def test_benign_non_github_never_warns():
    # A workspace of non-github checkouts is neutral, not an error.
    session = _session(repos=[_repo(name="x", repo=None, branch=None)])
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["tone"] == "neutral"
    assert badge["payload"]["text"] == "no GitHub"


def test_no_prs_is_neutral():
    session = _session(repos=[_repo()])
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["tone"] == "neutral"
    assert badge["payload"]["text"] == "no PRs"


def test_multi_repo_counts_open_prs():
    session = _session(
        repos=[
            _repo(name="a", pulls=[_pull(number=1)]),
            _repo(name="b", pulls=[_pull(number=2)]),
            _repo(name="c", pulls=[_pull(number=3, draft=True)]),
            _repo(name="d"),
        ]
    )
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert badge["payload"]["text"] == "2 PRs"
    assert badge["payload"]["tone"] == "success"


def test_empty_snapshot_still_pushes_a_global_status_bar():
    params = uistate.snapshot_ui_state_params({})
    assert _badges(params) == []
    bar = _statusbar(params)
    assert "tone" in bar["payload"]


def test_session_without_id_is_skipped_for_badges():
    params = uistate.snapshot_ui_state_params(_snapshot(_session(session_id=None)))
    assert _badges(params) == []


def test_global_text_reflects_drafts_not_just_pr_count():
    session = _session(repos=[_repo(pulls=[_pull(draft=True)])])
    bar = _statusbar(uistate.snapshot_ui_state_params(_snapshot(session)))
    assert bar["payload"]["text"] == "GitHub: 1 draft"
    assert bar["payload"]["tone"] == "warn"


def test_global_text_for_no_repos():
    bar = _statusbar(uistate.snapshot_ui_state_params(_snapshot(_session(repos=[]))))
    assert bar["payload"]["text"] == "GitHub: no repos"


def test_global_tooltip_marks_truncated_sessions():
    sessions = [_session(session_id=f"s{i}") for i in range(uistate._MAX_TOOLTIP_LINES + 5)]
    bar = _statusbar(uistate.snapshot_ui_state_params(_snapshot(*sessions)))
    assert "... +5 more" in bar["payload"]["tooltip"]


def test_detached_head_tooltip_does_not_claim_no_pr():
    # repo present, branch None => detached; the line must not lie about PRs.
    session = _session(repos=[_repo(name="d", branch=None)])
    badge = _badges(uistate.snapshot_ui_state_params(_snapshot(session)))[0]
    assert "detached HEAD" in badge["payload"]["tooltip"]
    assert "no PR" not in badge["payload"]["tooltip"]

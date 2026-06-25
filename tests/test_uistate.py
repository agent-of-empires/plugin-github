"""ui.state.set payload mapping (pure) and the fail-soft push path."""

from aoe_github_plugin import main
from aoe_github_plugin import uistate


def _status(pulls=(), error=None, summary="", repo="o/r", branch="feature"):
    return {
        "summary": summary,
        "repo": repo,
        "branch": branch,
        "pulls": list(pulls),
        "error": error,
    }


def _pull(number=7, draft=False):
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": "t",
        "state": "open",
        "draft": draft,
    }


def test_params_cover_every_declared_slot():
    params = uistate.ui_state_params(_status())
    assert [p["slot"] for p in params] == [s for s, _ in uistate.SLOTS]
    assert all(set(p) == {"slot", "id", "state"} for p in params)


def test_open_pr_state():
    state = uistate.ui_state_params(_status(pulls=[_pull(number=12)], summary="o/r: PR #12 open for feature"))[0][
        "state"
    ]
    assert state["tone"] == "open"
    assert state["text"] == "PR #12"
    assert state["url"] == "https://github.com/o/r/pull/12"
    assert state["tooltip"] == "o/r: PR #12 open for feature"


def test_draft_pr_tone():
    state = uistate.ui_state_params(_status(pulls=[_pull(draft=True)]))[0]["state"]
    assert state["tone"] == "draft"


def test_no_pr_state_has_no_url():
    state = uistate.ui_state_params(_status(summary="o/r: no open PR"))[0]["state"]
    assert state["tone"] == "none"
    assert state["text"] == "no PR"
    assert "url" not in state


def test_error_state():
    state = uistate.ui_state_params(_status(error={"kind": "unauthorized", "hint": "x"}, summary="GitHub: ..."))[0][
        "state"
    ]
    assert state["tone"] == "error"
    assert state["text"] == "GitHub !"


def test_partial_status_still_yields_valid_params():
    # A malformed status (missing keys) must not raise; the worker stays
    # fail-soft even if a future status shape drifts.
    params = uistate.ui_state_params({})
    assert len(params) == len(uistate.SLOTS)
    assert params[0]["state"]["tooltip"] == ""


def test_push_emits_one_request_per_slot_through_the_sink():
    sent = []
    main.push_ui_state(_status(pulls=[_pull()]), send=sent.append)
    assert len(sent) == len(uistate.SLOTS)
    methods = {m["method"] for m in sent}
    assert methods == {"ui.state.set"}
    ids = [m["id"] for m in sent]
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == len(ids)  # distinct ids per request
    assert all(m["jsonrpc"] == "2.0" for m in sent)


def test_push_is_failsoft_when_the_sink_raises():
    def boom(_message):
        raise BrokenPipeError("host went away")

    # Must not propagate: a dead host pipe cannot crash the worker.
    main.push_ui_state(_status(pulls=[_pull()]), send=boom)

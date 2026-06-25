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


def test_global_push_fills_the_status_bar_without_a_session_id():
    params = uistate.ui_state_params(_status())
    assert [p["slot"] for p in params] == [s for s, _ in uistate.GLOBAL_SLOTS]
    assert all(set(p) == {"slot", "id", "payload"} for p in params)


def test_per_session_push_fills_the_row_badge_with_a_session_id():
    params = uistate.ui_state_params(_status(), session_id="s1")
    assert [p["slot"] for p in params] == [s for s, _ in uistate.SESSION_SLOTS]
    assert all(p["session_id"] == "s1" for p in params)
    assert all(set(p) == {"slot", "id", "payload", "session_id"} for p in params)


def test_payload_carries_only_text_payload_fields():
    # The host parses TextPayload deny_unknown_fields: text + optional
    # tone/tooltip, nothing else (no url).
    payload = uistate.ui_state_params(_status(pulls=[_pull(number=12)]))[0]["payload"]
    assert set(payload) <= {"text", "tone", "tooltip"}


def test_open_pr_state():
    payload = uistate.ui_state_params(_status(pulls=[_pull(number=12)], summary="o/r: PR #12 open for feature"))[0][
        "payload"
    ]
    assert payload["tone"] == "success"
    assert payload["text"] == "PR #12"
    assert payload["tooltip"] == "o/r: PR #12 open for feature"


def test_draft_pr_tone():
    payload = uistate.ui_state_params(_status(pulls=[_pull(draft=True)]))[0]["payload"]
    assert payload["tone"] == "warn"


def test_no_pr_state():
    payload = uistate.ui_state_params(_status(summary="o/r: no open PR"))[0]["payload"]
    assert payload["tone"] == "neutral"
    assert payload["text"] == "no PR"


def test_error_state():
    payload = uistate.ui_state_params(_status(error={"kind": "unauthorized", "hint": "x"}, summary="GitHub: ..."))[0][
        "payload"
    ]
    assert payload["tone"] == "danger"
    assert payload["text"] == "GitHub !"


def test_partial_status_still_yields_valid_params():
    # A malformed status (missing keys) must not raise; the worker stays
    # fail-soft even if a future status shape drifts.
    params = uistate.ui_state_params({})
    assert len(params) == len(uistate.GLOBAL_SLOTS)
    assert params[0]["payload"]["tooltip"] == ""


def test_push_emits_one_request_per_slot_through_the_sink():
    sent = []
    main.push_ui_state(_status(pulls=[_pull()]), send=sent.append)
    assert len(sent) == len(uistate.GLOBAL_SLOTS)
    methods = {m["method"] for m in sent}
    assert methods == {"ui.state.set"}
    ids = [m["id"] for m in sent]
    assert all(isinstance(i, int) for i in ids)
    assert len(set(ids)) == len(ids)  # distinct ids per request
    assert all(m["jsonrpc"] == "2.0" for m in sent)


def test_per_session_push_carries_the_session_id_through_the_sink():
    sent = []
    main.push_ui_state(_status(pulls=[_pull()]), send=sent.append, session_id="s1")
    assert len(sent) == len(uistate.SESSION_SLOTS)
    assert all(m["params"]["session_id"] == "s1" for m in sent)


def test_push_is_failsoft_when_the_sink_raises():
    def boom(_message):
        raise BrokenPipeError("host went away")

    # Must not propagate: a dead host pipe cannot crash the worker.
    main.push_ui_state(_status(pulls=[_pull()]), send=boom)

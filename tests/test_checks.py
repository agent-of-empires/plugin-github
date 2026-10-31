import re

import pytest

from aoe_github_plugin import checks
from aoe_github_plugin import refresh
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.errors import ApiError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.errors import UnauthorizedError
from aoe_github_plugin.errors import InsufficientScopeError


class FakeEnv(TokenEnvironment):
    def __init__(self, *, env=None, gh=True, gh_token=("ok", "gh-token")):
        self.env = env or {}
        self.gh = gh
        self.gh_token = gh_token

    def env_var(self, key):
        return self.env.get(key)

    def gh_available(self):
        return self.gh

    def gh_auth_token(self):
        state, token = self.gh_token
        return state == "ok", token, "" if state == "ok" else "no oauth token found"


class FakeClient:
    """Answers the batched lookup and the notifications probe; records both."""

    def __init__(self, *, null_aliases=(), graphql_error=None, rest_error=None, envelope=None):
        self.null_aliases = set(null_aliases)
        self.graphql_error = graphql_error
        self.rest_error = rest_error
        self.envelope = envelope
        self.queries = []
        self.rest = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def post_graphql(self, query, variables):
        self.queries.append((query, variables))
        if self.graphql_error:
            raise self.graphql_error
        if self.envelope is not None:
            return self.envelope
        data = {"viewer": {"login": "me"}} if "viewer" in query else {}
        for index in map(int, re.findall(r"\ba(\d+):", query)):
            data[f"a{index}"] = None if index in self.null_aliases else {"id": "x"}
        return {"data": data}

    def get_json(self, path, params=None):
        self.rest.append((path, params))
        if self.rest_error:
            raise self.rest_error
        return []


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(refresh, "backoff_until", lambda _api: 0.0)


def _run(monkeypatch, client=None, env=None, **kwargs):
    client = client or FakeClient()
    built = []
    monkeypatch.setattr(checks, "GitHubClient", lambda token: built.append(token) or client)
    kwargs = {"mentioned_enabled": True, "repos_raw": "", "orgs_raw": "", **kwargs}
    result = checks.run_checks(env=env or FakeEnv(env={"GITHUB_TOKEN": "t"}), **kwargs)
    return {check["id"]: check for check in result}, client, built


def test_everything_ok_publishes_four_checks_in_order(monkeypatch):
    by_id, client, _ = _run(monkeypatch)
    assert list(by_id) == ["gh_installed", "logged_in", "read_repositories", "notifications_scope"]
    assert {check["status"] for check in by_id.values()} == {"ok"}
    assert all("fix" not in check for check in by_id.values())
    assert by_id["logged_in"]["detail"] == "Signed in as me."
    assert client.rest == [("/notifications", {"per_page": "1"})]


def test_no_token_makes_no_request_and_explains_the_fix(monkeypatch):
    env = FakeEnv(gh=False)
    by_id, client, built = _run(monkeypatch, env=env)
    assert built == []
    assert client.queries == []
    assert client.rest == []
    assert by_id["gh_installed"]["status"] == "fail"
    assert by_id["logged_in"]["status"] == "fail"
    assert "gh auth login" in by_id["logged_in"]["fix"]
    assert by_id["read_repositories"]["status"] == "warn"
    assert by_id["notifications_scope"]["status"] == "warn"


def test_gh_not_logged_in_fails_logged_in_but_gh_is_installed(monkeypatch):
    by_id, _, built = _run(monkeypatch, env=FakeEnv(gh_token=("err", "")))
    assert built == []
    assert by_id["gh_installed"]["status"] == "ok"
    assert by_id["logged_in"]["status"] == "fail"


def test_env_token_makes_a_missing_gh_only_a_warning(monkeypatch):
    by_id, _, _ = _run(monkeypatch, env=FakeEnv(env={"GH_TOKEN": "t"}, gh=False))
    assert by_id["gh_installed"]["status"] == "warn"
    assert by_id["logged_in"]["status"] == "ok"


def test_no_notifications_check_request_when_mentions_are_off(monkeypatch):
    by_id, client, _ = _run(monkeypatch, mentioned_enabled=False)
    assert client.rest == []
    assert by_id["notifications_scope"]["status"] == "ok"


def test_no_token_with_mentions_off_does_not_warn_about_notifications(monkeypatch):
    by_id, _, _ = _run(monkeypatch, env=FakeEnv(gh=False), mentioned_enabled=False)
    assert by_id["notifications_scope"]["status"] == "ok"


def test_notifications_403_fails_with_the_refresh_fix(monkeypatch):
    by_id, _, _ = _run(monkeypatch, client=FakeClient(rest_error=InsufficientScopeError("notifications")))
    assert by_id["notifications_scope"]["status"] == "fail"
    assert "gh auth refresh -s notifications" in by_id["notifications_scope"]["fix"]
    assert by_id["logged_in"]["status"] == "ok"


def test_scope_entries_are_looked_up_in_one_request_with_variables(monkeypatch):
    by_id, client, _ = _run(monkeypatch, repos_raw="Acme/Widgets", orgs_raw="acme")
    assert len(client.queries) == 1
    query, variables = client.queries[0]
    assert "viewer" in query
    assert "repository(" in query
    assert "repositoryOwner(" in query
    assert "acme" not in query
    assert variables == {"o0": "acme", "n0": "widgets", "l1": "acme"}
    assert by_id["read_repositories"]["status"] == "ok"


def test_unreadable_entries_are_named(monkeypatch):
    client = FakeClient(null_aliases={0})
    by_id, _, _ = _run(monkeypatch, client=client, repos_raw="acme/private", orgs_raw="acme")
    assert by_id["read_repositories"]["status"] == "fail"
    assert "acme/private" in by_id["read_repositories"]["detail"]
    assert "fix" in by_id["read_repositories"]
    assert by_id["logged_in"]["status"] == "ok"


def test_scope_is_chunked_and_only_the_first_chunk_asks_for_the_viewer(monkeypatch):
    repos = ",".join(f"acme/r{n}" for n in range(checks.SCOPE_CHUNK + 1))
    by_id, client, _ = _run(monkeypatch, repos_raw=repos)
    assert len(client.queries) == 2
    assert "viewer" in client.queries[0][0]
    assert "viewer" not in client.queries[1][0]
    assert by_id["read_repositories"]["status"] == "ok"


def test_unauthorized_token_fails_and_skips_the_notifications_probe(monkeypatch):
    by_id, client, _ = _run(monkeypatch, client=FakeClient(graphql_error=UnauthorizedError()))
    assert by_id["logged_in"]["status"] == "fail"
    assert by_id["read_repositories"]["status"] == "warn"
    assert by_id["notifications_scope"]["status"] == "warn"
    assert client.rest == []


def test_graphql_failure_is_only_a_warning(monkeypatch):
    by_id, _, _ = _run(monkeypatch, client=FakeClient(graphql_error=ApiError(502, "bad gateway")))
    assert by_id["logged_in"]["status"] == "warn"
    assert by_id["read_repositories"]["status"] == "warn"
    assert by_id["notifications_scope"]["status"] == "ok"


def test_graphql_rate_limit_arms_the_gate_and_warns(monkeypatch):
    armed = []
    monkeypatch.setattr(refresh, "arm_backoff", lambda api, _reset_at=None: armed.append(api))
    by_id, _, _ = _run(monkeypatch, client=FakeClient(graphql_error=RateLimitedError()))
    assert armed == ["graphql"]
    assert by_id["logged_in"]["status"] == "warn"


def test_each_budget_gate_skips_only_its_own_probe(monkeypatch):
    monkeypatch.setattr(checks.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(refresh, "backoff_until", lambda api: 99.0 if api == "graphql" else 0.0)
    by_id, client, _ = _run(monkeypatch)
    assert client.queries == []
    assert by_id["logged_in"]["status"] == "warn"
    assert by_id["notifications_scope"]["status"] == "ok"
    assert len(client.rest) == 1

    monkeypatch.setattr(refresh, "backoff_until", lambda api: 99.0 if api == "rest" else 0.0)
    by_id, client, _ = _run(monkeypatch)
    assert client.rest == []
    assert by_id["notifications_scope"]["status"] == "warn"
    assert by_id["logged_in"]["status"] == "ok"


def test_strings_are_clamped_by_utf8_bytes():
    check = checks.check("i" * 300, "é" * 400, "warn", "d" * 5000, "f" * 5000)
    assert len(check["id"].encode()) <= checks.ID_BYTES
    assert len(check["label"].encode()) <= checks.LABEL_BYTES
    assert len(check["detail"].encode()) <= checks.TEXT_BYTES
    assert len(check["fix"].encode()) <= checks.TEXT_BYTES


def _envelope(errors=None, **data):
    return {"data": {"viewer": {"login": "me"}, **data}, "errors": errors}


def test_a_not_found_alias_error_is_unreadable_not_a_failure_to_verify(monkeypatch):
    errors = [{"type": "NOT_FOUND", "path": ["a0"], "message": "Could not resolve to a Repository"}]
    client = FakeClient(envelope=_envelope(errors, a0=None))
    by_id, _, _ = _run(monkeypatch, client=client, repos_raw="acme/gone")
    assert by_id["read_repositories"]["status"] == "fail"
    assert "acme/gone" in by_id["read_repositories"]["detail"]


def test_a_transient_alias_error_is_unverified_with_its_message_not_a_grant_access_fix(monkeypatch):
    errors = [{"type": "INTERNAL", "path": ["a0"], "message": "Something went wrong while executing your query"}]
    client = FakeClient(envelope=_envelope(errors, a0=None))
    by_id, _, _ = _run(monkeypatch, client=client, repos_raw="acme/widgets")
    assert by_id["read_repositories"]["status"] == "warn"
    assert "Something went wrong" in by_id["read_repositories"]["detail"]
    assert "fix" not in by_id["read_repositories"]
    assert by_id["logged_in"]["status"] == "warn"


def test_a_null_data_envelope_keeps_the_error_message(monkeypatch):
    errors = [{"type": "FORBIDDEN", "message": "Resource protected by organization SAML enforcement."}]
    by_id, _, _ = _run(monkeypatch, client=FakeClient(envelope={"data": None, "errors": errors}))
    assert by_id["logged_in"]["status"] == "warn"
    assert "SAML enforcement" in by_id["logged_in"]["detail"]


def test_a_rate_limited_envelope_arms_the_gate_but_never_shortens_it(monkeypatch):
    gate = {"until": 0.0}
    seen, armed = [], []

    def arm_from_envelope(envelope):
        seen.append(envelope)
        gate["until"] = 99.0  # the exact reset GitHub reported
        return True

    monkeypatch.setattr(refresh, "arm_if_graphql_rate_limited", arm_from_envelope)
    monkeypatch.setattr(refresh, "arm_backoff", lambda api, _reset_at=None: armed.append(api))
    monkeypatch.setattr(refresh, "backoff_until", lambda _api: gate["until"])
    monkeypatch.setattr(checks.time, "monotonic", lambda: 10.0)
    envelope = {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
    by_id, client, _ = _run(monkeypatch, client=FakeClient(envelope=envelope))
    assert len(client.queries) == 1
    assert seen == [envelope]
    assert armed == []
    assert by_id["logged_in"]["status"] == "warn"


def test_scope_round_trips_are_bounded_and_the_rest_is_reported(monkeypatch):
    repos = ",".join(f"acme/r{n:03}" for n in range(checks.MAX_SCOPE_ENTRIES + 7))
    by_id, client, _ = _run(monkeypatch, repos_raw=repos)
    assert len(client.queries) == 5
    assert by_id["read_repositories"]["status"] == "ok"
    assert "and 7 more not checked." in by_id["read_repositories"]["detail"]


def test_a_scope_with_only_invalid_entries_is_not_reported_as_unconfigured(monkeypatch):
    by_id, _, _ = _run(monkeypatch, repos_raw="not a repo, is:private", orgs_raw="-bad")
    assert by_id["read_repositories"]["status"] == "warn"
    assert "3 entries ignored (invalid owner/repo)." in by_id["read_repositories"]["detail"]
    assert "owner/repo" in by_id["read_repositories"]["fix"]


def test_ignored_entries_are_noted_next_to_valid_ones(monkeypatch):
    by_id, _, _ = _run(monkeypatch, repos_raw="acme/widgets, bad entry")
    assert by_id["read_repositories"]["status"] == "warn"
    assert "1 entries exist and are visible." in by_id["read_repositories"]["detail"]
    assert "1 entries ignored" in by_id["read_repositories"]["detail"]


def test_owner_entries_claim_visibility_not_readability(monkeypatch):
    by_id, _, _ = _run(monkeypatch, orgs_raw="acme")
    assert "exist and are visible" in by_id["read_repositories"]["detail"]


@pytest.mark.parametrize("env", [FakeEnv(env={"GITHUB_TOKEN": "t"}), FakeEnv(gh=False)])
def test_a_silent_host_leaves_notifications_unknown_not_ok(monkeypatch, env):
    by_id, client, _ = _run(monkeypatch, env=env, mentioned_enabled=None)
    assert by_id["notifications_scope"]["status"] == "warn"
    assert by_id["notifications_scope"]["detail"] == checks.UNKNOWN_SOURCES
    assert client.rest == []

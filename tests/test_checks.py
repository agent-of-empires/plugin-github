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
        for index in range(len(variables)):
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

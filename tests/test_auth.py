"""Token resolution + hint wording. No network, no real ``gh``. Mirrors the
#1681 Rust unit tests."""

import subprocess

import pytest

from aoe_github_plugin import auth
from aoe_github_plugin import errors
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.auth import resolve_token
from aoe_github_plugin.hosts import GitHubHost


class FakeEnv(TokenEnvironment):
    """``gh_result`` is ``(success, stdout, stderr)`` or an Exception to raise;
    ``None`` means ``gh`` must not be consulted (raises if it is)."""

    def __init__(self, github_token=None, gh_token=None, gh_available=False, gh_result=None):
        self._vars = {"GITHUB_TOKEN": github_token, "GH_TOKEN": gh_token}
        self._gh_available = gh_available
        self._gh_result = gh_result

    def env_var(self, key):
        return self._vars.get(key)

    def gh_available(self):
        return self._gh_available

    def gh_auth_token(self, hostname="github.com"):
        if self._gh_result is None:
            raise AssertionError("gh_auth_token consulted unexpectedly")
        if isinstance(self._gh_result, Exception):
            raise self._gh_result
        return self._gh_result


def test_github_token_env_wins_without_touching_gh():
    env = FakeEnv(github_token="env-tok", gh_available=True, gh_result=None)
    assert resolve_token(env) == ("env-tok", "env:GITHUB_TOKEN")


def test_gh_token_env_used_when_github_token_absent():
    env = FakeEnv(gh_token="gh-env-tok", gh_available=True, gh_result=None)
    assert resolve_token(env) == ("gh-env-tok", "env:GH_TOKEN")


def test_empty_env_token_is_skipped():
    env = FakeEnv(github_token="   ", gh_available=True, gh_result=(True, "cli-tok\n", ""))
    assert resolve_token(env) == ("cli-tok", "gh-cli")


def test_gh_authenticated_reuses_token_no_prompt():
    env = FakeEnv(gh_available=True, gh_result=(True, "gho_abc123\n", ""))
    assert resolve_token(env) == ("gho_abc123", "gh-cli")


def test_no_token_and_no_gh():
    with pytest.raises(errors.NoTokenNoGhError):
        resolve_token(FakeEnv(gh_available=False))


def test_gh_not_authenticated_empty_stderr():
    with pytest.raises(errors.GhNotAuthenticatedError):
        resolve_token(FakeEnv(gh_available=True, gh_result=(False, "", "")))


def test_gh_not_authenticated_canonical_message():
    env = FakeEnv(gh_available=True, gh_result=(False, "", "no oauth token found"))
    with pytest.raises(errors.GhNotAuthenticatedError):
        resolve_token(env)


def test_gh_other_failure_is_command_failed():
    env = FakeEnv(gh_available=True, gh_result=(False, "", "gh: connection reset"))
    with pytest.raises(errors.GhCommandFailedError):
        resolve_token(env)


def test_gh_empty_token():
    env = FakeEnv(gh_available=True, gh_result=(True, "   \n", ""))
    with pytest.raises(errors.GhReturnedEmptyTokenError):
        resolve_token(env)


def test_gh_oserror_is_command_failed():
    env = FakeEnv(gh_available=True, gh_result=OSError("boom"))
    with pytest.raises(errors.GhCommandFailedError):
        resolve_token(env)


def test_gh_auth_token_timeout_returns_failure_tuple(monkeypatch):
    # A hung `gh` must not raise TimeoutExpired out of gh_auth_token (it is not
    # an OSError, so resolve_token would not catch it and the whole refresh would
    # abort). It degrades to a failure tuple instead.
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh auth token", timeout=auth.GH_AUTH_TIMEOUT)

    monkeypatch.setattr(auth.subprocess, "run", _raise)
    success, stdout, stderr = SystemEnvironment().gh_auth_token()
    assert success is False
    assert stdout == ""
    assert "timed out" in stderr


def test_gh_auth_token_timeout_maps_to_command_failed():
    # The failure tuple a timeout produces is a non-canonical stderr, so it is a
    # real gh failure, mapped to GhCommandFailedError (swallowed by the refresh
    # path's _resolve_optional_token, which falls back to unauthenticated).
    env = FakeEnv(gh_available=True, gh_result=(False, "", "gh auth token timed out after 5s"))
    with pytest.raises(errors.GhCommandFailedError):
        resolve_token(env)


def test_no_token_no_gh_hint_mentions_token_and_install():
    msg = str(errors.NoTokenNoGhError())
    assert "GITHUB_TOKEN" in msg
    assert "brew install gh" in msg or "install the GitHub CLI" in msg


def test_gh_not_authenticated_hint_says_login_not_install():
    msg = str(errors.GhNotAuthenticatedError())
    assert "gh auth login" in msg
    assert "brew install" not in msg
    assert "install the GitHub CLI" not in msg


def test_gh_token_precedes_github_token():
    assert resolve_token(FakeEnv(github_token="old", gh_token="preferred")) == ("preferred", "env:GH_TOKEN")


class MultiHostEnv(TokenEnvironment):
    def __init__(self, variables=None):
        self.variables = variables or {}
        self.lookups = []

    def env_var(self, key):
        return self.variables.get(key)

    def gh_available(self):
        return True

    def gh_auth_token(self, hostname="github.com"):
        self.lookups.append(hostname)
        return True, f"stored-{hostname}", ""


def test_enterprise_env_token_is_bound_to_one_host():

    env = MultiHostEnv({"GH_ENTERPRISE_TOKEN": "only-a", "GH_HOST": "A.Corp.", "GH_TOKEN": "public"})
    assert resolve_token(env, GitHubHost.enterprise("a.corp")) == ("only-a", "env:GH_ENTERPRISE_TOKEN")
    assert resolve_token(env, GitHubHost.enterprise("b.corp")) == ("stored-b.corp", "gh-cli")
    assert resolve_token(env, GitHubHost.dotcom()) == ("public", "env:GH_TOKEN")
    assert env.lookups == ["b.corp"]


def test_unbound_enterprise_env_token_never_broadcasts():

    env = MultiHostEnv({"GH_ENTERPRISE_TOKEN": "unbound"})
    for name in ("a.corp", "b.corp"):
        assert resolve_token(env, GitHubHost.enterprise(name)) == (f"stored-{name}", "gh-cli")


def test_system_gh_fallback_strips_token_env_and_passes_hostname(monkeypatch):
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("GH_CONFIG_DIR", "/fixture-config")

    def run(argv, **kwargs):
        assert argv == ["gh", "auth", "token", "--hostname", "b.corp"]
        assert not any(
            k in kwargs["env"] for k in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        )
        assert kwargs["env"]["GH_CONFIG_DIR"] == "/fixture-config"
        return subprocess.CompletedProcess(argv, 0, "stored-b", "")

    monkeypatch.setattr(auth.subprocess, "run", run)
    assert SystemEnvironment().gh_auth_token("b.corp") == (True, "stored-b", "")


@pytest.mark.parametrize(
    "error", [errors.NoTokenNoGhError, errors.GhNotAuthenticatedError, errors.GhReturnedEmptyTokenError]
)
def test_enterprise_auth_hints(error):
    message = str(error(GitHubHost.enterprise("gh.corp")))
    assert "gh auth login --hostname gh.corp" in message
    assert "GH_HOST=gh.corp" in message
    assert "GH_ENTERPRISE_TOKEN" in message


def test_failed_helper_cannot_echo_credentials_into_hints():
    env = FakeEnv(gh_available=True, gh_result=(False, "", "secret-credential"))
    with pytest.raises(errors.GhCommandFailedError) as exc:
        resolve_token(env)
    assert "secret-credential" not in str(exc.value)

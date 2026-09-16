"""Token resolution + hint wording. No network, no real ``gh``. Mirrors the
#1681 Rust unit tests."""

import subprocess

import pytest

from aoe_github_plugin import auth
from aoe_github_plugin import errors
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.auth import resolve_token


class FakeEnv(TokenEnvironment):
    """``gh_result`` is ``(success, stdout, stderr)`` or an Exception to raise;
    ``None`` means ``gh`` must not be consulted (raises if it is)."""

    def __init__(self, github_token=None, gh_token=None, gh_available=False, gh_result=None, gh_user_result=None):
        self._vars = {"GITHUB_TOKEN": github_token, "GH_TOKEN": gh_token}
        self._gh_available = gh_available
        self._gh_result = gh_result
        self._gh_user_result = gh_user_result
        self.asked_for = []

    def env_var(self, key):
        return self._vars.get(key)

    def gh_available(self):
        return self._gh_available

    def gh_auth_token(self):
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


# --- per-owner accounts (#91) -------------------------------------------------


class _UserEnv(FakeEnv):
    """``gh_auth_token_for_user`` seam; ``gh_auth_token`` must not be consulted."""

    def gh_auth_token(self):
        raise AssertionError("the account path must not fall back to the default gh token")

    def gh_auth_token_for_user(self, login):
        self.asked_for.append(login)
        if isinstance(self._gh_user_result, Exception):
            raise self._gh_user_result
        return self._gh_user_result


def test_account_token_comes_from_gh_for_that_login():
    env = _UserEnv(gh_available=True, gh_user_result=(True, "acct-tok\n", ""))
    assert auth.resolve_token_for_account("octocat", env) == ("acct-tok", "gh-cli:octocat")
    assert env.asked_for == ["octocat"]


def test_account_token_ignores_env_vars():
    """``GITHUB_TOKEN`` carries no account identity, so honouring it here would
    authenticate as the wrong user -- the exact bug per-owner accounts fix."""
    env = _UserEnv(github_token="global", gh_available=True, gh_user_result=(True, "acct-tok", ""))
    token, source = auth.resolve_token_for_account("octocat", env)
    assert (token, source) == ("acct-tok", "gh-cli:octocat")


def test_account_token_without_gh_raises_no_gh():
    env = _UserEnv(github_token="global", gh_available=False)
    with pytest.raises(errors.NoTokenNoGhError):
        auth.resolve_token_for_account("octocat", env)


def test_account_token_not_signed_in():
    env = _UserEnv(gh_available=True, gh_user_result=(False, "", "no oauth token found"))
    with pytest.raises(errors.GhNotAuthenticatedError):
        auth.resolve_token_for_account("octocat", env)


def test_account_token_empty_output():
    env = _UserEnv(gh_available=True, gh_user_result=(True, "   ", ""))
    with pytest.raises(errors.GhReturnedEmptyTokenError):
        auth.resolve_token_for_account("octocat", env)


def test_account_token_real_gh_failure_is_not_mislabeled():
    env = _UserEnv(gh_available=True, gh_user_result=(False, "", "unknown user octocat"))
    with pytest.raises(errors.GhCommandFailedError) as excinfo:
        auth.resolve_token_for_account("octocat", env)
    assert "unknown user octocat" in str(excinfo.value)


def test_account_token_oserror_becomes_command_failed():
    env = _UserEnv(gh_available=True, gh_user_result=OSError("boom"))
    with pytest.raises(errors.GhCommandFailedError):
        auth.resolve_token_for_account("octocat", env)


def test_system_environment_passes_user_flag(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "tok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, out, _ = SystemEnvironment().gh_auth_token_for_user("octocat")
    assert (ok, out) == (True, "tok")
    assert seen["argv"] == ["gh", "auth", "token", "--user", "octocat"]


def test_system_environment_user_timeout_degrades(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, _, err = SystemEnvironment().gh_auth_token_for_user("octocat")
    assert ok is False
    assert "timed out" in err

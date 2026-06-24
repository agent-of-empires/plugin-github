"""Token resolution + hint wording. No network, no real ``gh``. Mirrors the
#1681 Rust unit tests."""

import pytest

from aoe_github_plugin import errors
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import resolve_token


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


def test_no_token_no_gh_hint_mentions_token_and_install():
    msg = str(errors.NoTokenNoGhError())
    assert "GITHUB_TOKEN" in msg
    assert "brew install gh" in msg or "install the GitHub CLI" in msg


def test_gh_not_authenticated_hint_says_login_not_install():
    msg = str(errors.GhNotAuthenticatedError())
    assert "gh auth login" in msg
    assert "brew install" not in msg
    assert "install the GitHub CLI" not in msg

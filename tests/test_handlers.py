"""Structured status + open-in-GitHub handlers. No network (MockTransport)."""

import httpx
import pytest

from aoe_github_plugin import errors
from aoe_github_plugin import handlers
from aoe_github_plugin.auth import TokenEnvironment


class _NoTokenEnv(TokenEnvironment):
    """No token anywhere: handlers fall back to unauthenticated requests."""

    def env_var(self, key):
        return None

    def gh_available(self):
        return False


def _pr(number=7, branch="feature", draft=False):
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "title": f"PR for {branch}",
        "state": "open",
        "draft": draft,
    }


def _transport(handler):
    return httpx.MockTransport(handler)


def test_status_returns_structured_pull(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")

    def handler(request):
        assert "head=o:feature" in str(request.url)
        return httpx.Response(200, json=[_pr()])

    result = handlers.github_status(env=_NoTokenEnv(), transport=_transport(handler))
    assert result["repo"] == "o/r"
    assert result["branch"] == "feature"
    assert result["error"] is None
    assert result["pulls"][0]["url"] == "https://github.com/o/r/pull/7"
    assert result["pulls"][0]["draft"] is False
    assert "PR #7" in result["summary"]


def test_status_no_open_pr(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")
    result = handlers.github_status(
        env=_NoTokenEnv(),
        transport=_transport(lambda _r: httpx.Response(200, json=[])),
    )
    assert result["pulls"] == []
    assert "no open PR" in result["summary"]


def test_status_not_a_github_remote(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: None)
    result = handlers.github_status(env=_NoTokenEnv())
    assert result["repo"] is None
    assert result["pulls"] == []
    assert "not a github.com remote" in result["summary"]


def test_status_is_failsoft_on_api_error(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")
    result = handlers.github_status(
        env=_NoTokenEnv(),
        transport=_transport(lambda _r: httpx.Response(401)),
    )
    assert result["error"]["kind"] == "unauthorized"
    assert "GitHub:" in result["summary"]
    assert result["pulls"] == []


def test_open_returns_existing_pull(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")
    result = handlers.github_open(
        env=_NoTokenEnv(),
        transport=_transport(lambda _r: httpx.Response(200, json=[_pr(number=12)])),
    )
    assert result == {"url": "https://github.com/o/r/pull/12", "kind": "pull"}


def test_open_falls_back_to_compare_url(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")
    result = handlers.github_open(
        env=_NoTokenEnv(),
        transport=_transport(lambda _r: httpx.Response(200, json=[])),
    )
    assert result == {"url": "https://github.com/o/r/compare/feature?expand=1", "kind": "compare"}


def test_open_compare_url_when_api_unreachable(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")

    def handler(_request):
        raise httpx.ConnectError("refused")

    result = handlers.github_open(env=_NoTokenEnv(), transport=_transport(handler))
    assert result["kind"] == "compare"


def test_open_raises_without_github_remote(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: None)
    with pytest.raises(errors.GitHubError):
        handlers.github_open(env=_NoTokenEnv())

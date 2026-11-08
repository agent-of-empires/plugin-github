"""Structured status + open-in-GitHub handlers. No network (MockTransport)."""

import subprocess

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
        "head": {"ref": branch, "repo": {"name": "r", "owner": {"login": "o"}}},
    }


def _transport(handler):
    return httpx.MockTransport(handler)


def test_status_returns_structured_pull(monkeypatch):
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("o", "r"))
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")

    def handler(request):
        # head is passed as a query param now (httpx encodes the colon).
        assert request.url.params["head"] == "o:feature"
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


def test_open_returns_upstream_pull_when_origin_errors(monkeypatch):
    # #130 review: an origin failure must not hide an upstream hit. `open_pulls`
    # keeps the origin error only when nothing found the PR, so `github.open`
    # resolves the upstream PR instead of falling back to the origin compare
    # URL that would claim no PR exists.
    monkeypatch.setattr(handlers, "remote_owner_repo", lambda _p: ("alice", "r"))
    monkeypatch.setattr(handlers, "remote_owner_repos", lambda _p: [("alice", "r"), ("upstream", "r")])
    monkeypatch.setattr(handlers, "current_branch", lambda _p: "feature")

    def handler(request):
        if request.url.path == "/repos/alice/r/pulls":
            return httpx.Response(502, json={"message": "server error"})
        if request.url.path == "/repos/upstream/r/pulls":
            assert request.url.params["head"] == "alice:feature"
            return httpx.Response(
                200,
                json=[
                    {
                        **_pr(),
                        "html_url": "https://github.com/upstream/r/pull/9",
                        "head": {"ref": "feature", "repo": {"name": "r", "owner": {"login": "alice"}}},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    result = handlers.github_open(env=_NoTokenEnv(), transport=_transport(handler))
    assert result == {"url": "https://github.com/upstream/r/pull/9", "kind": "pull"}


def test_status_discovers_fork_pull_targeting_configured_upstream(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "feature", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/alice/r.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "upstream", "git@github.com:upstream/r.git"],
        check=True,
    )

    def handler(request):
        # No repo-metadata request: the configured upstream is the only extra
        # destination, and origin is probed directly.
        assert request.url.path != "/repos/alice/r"
        if request.url.path == "/repos/upstream/r/pulls" and request.url.params["head"] == "alice:feature":
            return httpx.Response(
                200,
                json=[
                    {
                        **_pr(),
                        "html_url": "https://github.com/upstream/r/pull/7",
                        "head": {"ref": "feature", "repo": {"name": "r", "owner": {"login": "alice"}}},
                    }
                ],
            )
        return httpx.Response(200, json=[])

    result = handlers.github_status(str(tmp_path), env=_NoTokenEnv(), transport=_transport(handler))
    assert result["repo"] == "alice/r"
    assert result["pulls"][0]["url"] == "https://github.com/upstream/r/pull/7"


def test_linked_status_and_open_query_exact_pr(monkeypatch):
    monkeypatch.setattr(handlers, "worktree_pr_link", lambda _path: ("upstream", "project", 42))
    monkeypatch.setattr(handlers, "current_branch", lambda _path: "local-review")

    def handler(request):
        assert request.url.path == "/repos/upstream/project/pulls/42"
        return httpx.Response(200, json={**_pr(42), "html_url": "https://github.com/upstream/project/pull/42"})

    transport = _transport(handler)
    result = handlers.github_status(env=_NoTokenEnv(), transport=transport)
    assert (result["repo"], result["branch"], result["pulls"][0]["number"]) == ("upstream/project", "local-review", 42)
    assert handlers.github_open(env=_NoTokenEnv(), transport=transport) == {
        "url": "https://github.com/upstream/project/pull/42",
        "kind": "pull",
    }


def test_linked_merged_pr_status_reports_merged(monkeypatch):
    monkeypatch.setattr(handlers, "worktree_pr_link", lambda _path: ("upstream", "project", 42))
    monkeypatch.setattr(handlers, "current_branch", lambda _path: "review")
    transport = _transport(
        lambda _request: httpx.Response(200, json={**_pr(42), "state": "closed", "merged_at": "2026-01-01T00:00:00Z"})
    )
    result = handlers.github_status(env=_NoTokenEnv(), transport=transport)
    assert result["pulls"][0]["merged"] is True
    assert result["summary"] == "upstream/project: PR #42 merged"


def test_missing_linked_pr_never_opens_unrelated_branch(monkeypatch):
    monkeypatch.setattr(handlers, "worktree_pr_link", lambda _path: ("upstream", "project", 42))
    monkeypatch.setattr(handlers, "current_branch", lambda _path: "local-review")
    transport = _transport(lambda _request: httpx.Response(404))
    assert handlers.github_status(env=_NoTokenEnv(), transport=transport)["error"]["kind"] == "not_found"
    with pytest.raises(errors.NotFoundError):
        handlers.github_open(env=_NoTokenEnv(), transport=transport)


def test_linked_pr_with_missing_url_reports_error(monkeypatch):
    monkeypatch.setattr(handlers, "worktree_pr_link", lambda _path: ("upstream", "project", 42))
    monkeypatch.setattr(handlers, "current_branch", lambda _path: "review")
    pull = _pr(42)
    del pull["html_url"]
    transport = _transport(lambda _request: httpx.Response(200, json=pull))
    assert handlers.github_status(env=_NoTokenEnv(), transport=transport)["error"]["kind"] == "github_error"
    with pytest.raises(errors.GitHubError):
        handlers.github_open(env=_NoTokenEnv(), transport=transport)

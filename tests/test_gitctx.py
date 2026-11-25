"""Remote URL parsing. Pure, no git invocation."""

import subprocess

import pytest

from aoe_github_plugin import errors
from aoe_github_plugin import refresh
from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.utils import gitctx
from aoe_github_plugin.utils.gitctx import GhDiscoveryError
from aoe_github_plugin.utils.gitctx import current_branch
from aoe_github_plugin.utils.gitctx import gh_pr_repository
from aoe_github_plugin.utils.gitctx import parse_owner_repo


def test_ssh_with_git_suffix():
    assert parse_owner_repo("git@github.com:agent-of-empires/plugin-github.git") == (
        GitHubHost.dotcom(),
        "agent-of-empires",
        "plugin-github",
    )


def test_https_no_suffix():
    assert parse_owner_repo("https://github.com/owner/repo") == (GitHubHost.dotcom(), "owner", "repo")


def test_https_with_git_suffix():
    assert parse_owner_repo("https://github.com/owner/repo.git") == (GitHubHost.dotcom(), "owner", "repo")


def test_https_trailing_slash():
    assert parse_owner_repo("https://github.com/owner/repo/") == (GitHubHost.dotcom(), "owner", "repo")
    assert parse_owner_repo("https://github.com/owner/repo.git/") == (GitHubHost.dotcom(), "owner", "repo")


def test_ssh_protocol_form():
    assert parse_owner_repo("ssh://git@github.com/owner/repo.git") == (GitHubHost.dotcom(), "owner", "repo")


def test_non_github_is_none():
    assert parse_owner_repo("https://gitlab.com/owner/repo") is None


def test_malformed_is_none():
    assert parse_owner_repo("git@github.com:onlyone") is None


def test_remote_owner_repos_preserves_trusted_hosts_and_skips_unknown(monkeypatch):
    host = GitHubHost.enterprise("gh.corp")
    trusted = {"github.com": GitHubHost.dotcom(), host.name: host}
    urls = {
        "origin": "https://gh.corp/alice/r.git",
        "upstream": "git@gh.corp:upstream/r.git",
        "duplicate": "ssh://git@gh.corp/alice/r.git",
        "public": "https://github.com/public/r.git",
        "unknown": "https://unknown.example/owner/r.git",
    }

    def git(path, *args):
        if args == ("remote",):
            return "upstream public origin duplicate unknown broken"
        if args[-1] == "broken":
            raise errors.GitHubError("remote unavailable")
        return urls[args[-1]]

    monkeypatch.setattr(gitctx, "_git", git)
    assert gitctx.remote_owner_repos(".", trusted) == [
        (host, "alice", "r"),
        (GitHubHost.dotcom(), "public", "r"),
        (host, "upstream", "r"),
    ]


@pytest.fixture
def public_gh_checkout(monkeypatch):
    monkeypatch.setattr(gitctx, "_public_gh_environment", lambda _path: gitctx.checkout_environment())


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"state":"OPEN","url":"https://github.com/upstream/project/pull/42"}', ("upstream", "project")),
        ('{"state":"CLOSED","url":"https://github.com/upstream/project/pull/42"}', None),
    ],
)
def test_gh_pr_repository_validates_output(public_gh_checkout, monkeypatch, tmp_path, output, expected):
    def run(args, **kwargs):
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(subprocess, "run", run)
    assert gh_pr_repository(tmp_path) == expected


@pytest.mark.parametrize(
    "output", ['{"state":"OPEN","url":"https://github.com.evil/o/r/pull/1"}', "not json", '{"state":[]}']
)
def test_gh_pr_repository_rejects_bad_response(public_gh_checkout, monkeypatch, tmp_path, output):
    monkeypatch.setattr(subprocess, "run", lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=output))
    with pytest.raises(GhDiscoveryError):
        gh_pr_repository(tmp_path)


def test_gh_pr_repository_falls_back_when_gh_missing(public_gh_checkout, monkeypatch, tmp_path):
    def run(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(GhDiscoveryError):
        gh_pr_repository(tmp_path)


@pytest.mark.parametrize("branch", ["feature", r"feature\"review"])
def test_gh_no_pr_is_confirmed_absence(public_gh_checkout, monkeypatch, tmp_path, branch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f'no pull requests found for branch "{branch}"\n'
        ),
    )
    assert gh_pr_repository(tmp_path) is None


def test_gh_other_failure_is_not_confirmed_absence(public_gh_checkout, monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **_kwargs: subprocess.CompletedProcess(args, 1, stdout="", stderr="HTTP 403")
    )
    with pytest.raises(GhDiscoveryError):
        gh_pr_repository(tmp_path)


def test_git_helpers_ignore_inherited_repository_location(tmp_path, monkeypatch):
    first = tmp_path / "first"
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "first", str(first)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "other", str(other)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(first),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_COMMON_DIR", str(other / ".git"))
    assert current_branch(str(first)) == "first"
    assert refresh._git(str(first), "branch", "--show-current") == "first"


def test_gh_uses_checkout_environment(public_gh_checkout, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_REPO", "other/repo")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    token = str(tmp_path)
    monkeypatch.setenv("GH_TOKEN", token)

    def run(args, **kwargs):
        env = kwargs["env"]
        assert "GH_REPO" not in env
        assert "GIT_DIR" not in env
        assert env["GH_TOKEN"] == token
        return subprocess.CompletedProcess(args, 0, stdout='{"state":"CLOSED"}')

    monkeypatch.setattr(subprocess, "run", run)
    assert gh_pr_repository(tmp_path) is None


@pytest.mark.parametrize("other_host", [None, "gh.corp", "tenant.ghe.com"])
@pytest.mark.parametrize("push_only", [False, True])
def test_cli_discovery_keeps_public_credentials_on_public_checkouts(tmp_path, monkeypatch, other_host, push_only):
    subprocess.run(["git", "init", "-q", "-b", "feature", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/alice/r.git"], check=True
    )
    if other_host:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "other", f"https://{other_host}/other/r.git"], check=True
        )
    if other_host and push_only:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "set-url", "other", "https://github.com/other/r.git"], check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "set-url", "--push", "other", f"https://{other_host}/other/r.git"],
            check=True,
        )
    monkeypatch.setenv("GH_HOST", "gh.corp")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-secret")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "other-enterprise-secret")
    monkeypatch.setenv("GH_TOKEN", "public-secret")
    real_run = subprocess.run
    calls = []

    def run(args, **kwargs):
        if args[0] != "gh":
            return real_run(args, **kwargs)
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout='{"state":"CLOSED"}')

    monkeypatch.setattr(subprocess, "run", run)
    if other_host:
        with pytest.raises(GhDiscoveryError):
            gh_pr_repository(tmp_path)
        assert calls == []
    else:
        assert gh_pr_repository(tmp_path) is None
        assert calls[0]["GH_HOST"] == "github.com"
        assert "GH_ENTERPRISE_TOKEN" not in calls[0]
        assert "GITHUB_ENTERPRISE_TOKEN" not in calls[0]
        assert calls[0]["GH_TOKEN"] == "public-secret"  # noqa: S105 - synthetic credential

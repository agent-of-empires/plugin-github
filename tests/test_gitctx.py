"""Remote URL parsing. Pure, no git invocation."""

import subprocess

import pytest

from aoe_github_plugin import refresh
from aoe_github_plugin.utils.gitctx import GhDiscoveryError
from aoe_github_plugin.utils.gitctx import current_branch
from aoe_github_plugin.utils.gitctx import gh_pr_repository
from aoe_github_plugin.utils.gitctx import parse_owner_repo


def test_ssh_with_git_suffix():
    assert parse_owner_repo("git@github.com:agent-of-empires/plugin-github.git") == (
        "agent-of-empires",
        "plugin-github",
    )


def test_https_no_suffix():
    assert parse_owner_repo("https://github.com/owner/repo") == ("owner", "repo")


def test_https_with_git_suffix():
    assert parse_owner_repo("https://github.com/owner/repo.git") == ("owner", "repo")


def test_ssh_protocol_form():
    assert parse_owner_repo("ssh://git@github.com/owner/repo.git") == ("owner", "repo")


def test_non_github_is_none():
    assert parse_owner_repo("https://gitlab.com/owner/repo") is None


def test_malformed_is_none():
    assert parse_owner_repo("git@github.com:onlyone") is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"state":"OPEN","url":"https://github.com/upstream/project/pull/42"}', ("upstream", "project")),
        ('{"state":"CLOSED","url":"https://github.com/upstream/project/pull/42"}', None),
    ],
)
def test_gh_pr_repository_validates_output(monkeypatch, tmp_path, output, expected):
    def run(args, **kwargs):
        assert kwargs["cwd"] == tmp_path
        return subprocess.CompletedProcess(args, 0, stdout=output)

    monkeypatch.setattr(subprocess, "run", run)
    assert gh_pr_repository(tmp_path) == expected


@pytest.mark.parametrize(
    "output", ['{"state":"OPEN","url":"https://github.com.evil/o/r/pull/1"}', "not json", '{"state":[]}']
)
def test_gh_pr_repository_rejects_bad_response(monkeypatch, tmp_path, output):
    monkeypatch.setattr(subprocess, "run", lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout=output))
    with pytest.raises(GhDiscoveryError):
        gh_pr_repository(tmp_path)


def test_gh_pr_repository_falls_back_when_gh_missing(monkeypatch, tmp_path):
    def run(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(GhDiscoveryError):
        gh_pr_repository(tmp_path)


@pytest.mark.parametrize("branch", ["feature", r"feature\"review"])
def test_gh_no_pr_is_confirmed_absence(monkeypatch, tmp_path, branch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 1, stdout="", stderr=f'no pull requests found for branch "{branch}"\n'
        ),
    )
    assert gh_pr_repository(tmp_path) is None


def test_gh_other_failure_is_not_confirmed_absence(monkeypatch, tmp_path):
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


def test_gh_uses_checkout_environment(monkeypatch, tmp_path):
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

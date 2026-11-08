"""Remote URL parsing. Pure, no git invocation."""

import subprocess

import pytest

from aoe_github_plugin.utils.gitctx import GhDiscoveryError
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

"""Remote URL parsing. Pure, no git invocation."""

from aoe_github_plugin import errors
from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.utils import gitctx
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

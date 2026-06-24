"""Remote URL parsing. Pure, no git invocation."""

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

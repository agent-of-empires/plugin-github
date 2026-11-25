"""Offline host trust and URL parsing, using only local fixture config."""

import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from aoe_github_plugin import auth
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.hosts import canonical_hostname
from aoe_github_plugin.hosts import discover_trusted_hosts
from aoe_github_plugin.utils.gitctx import parse_owner_repo


class HostsEnv(TokenEnvironment):
    def __init__(self, names):
        self.names = names

    def gh_config_hosts(self):
        return self.names


def test_host_endpoints_and_kinds():
    public = GitHubHost.dotcom()
    server = GitHubHost.enterprise("GH.Corp.")
    assert (public.kind, public.rest_base, public.graphql_url) == (
        "dotcom",
        "https://api.github.com",
        "https://api.github.com/graphql",
    )
    assert (server.name, server.kind, server.rest_base, server.graphql_url, server.web_base) == (
        "gh.corp",
        "ghes",
        "https://gh.corp/api/v3",
        "https://gh.corp/api/graphql",
        "https://gh.corp",
    )


@pytest.mark.parametrize(
    "name",
    [
        "https://gh.corp",
        "a/b",
        "user@host",
        "host:443",
        "gh..corp",
        "-gh.corp",
        "gh.corp..",
        "127.0.0.1",
        "a b",
        "",
        "a\\b",
    ],
)
def test_invalid_hostname(name):
    assert canonical_hostname(name) is None


def test_discovery_filters_unsupported_products_and_canonicalizes():
    trusted = discover_trusted_hosts(HostsEnv(["GH.Corp.", "localhost", "tenant.ghe.com", "ghe.com", "https://bad"]))
    assert set(trusted) == {"github.com", "gh.corp"}
    for name in ("localhost", "tenant.ghe.com", "ghe.com"):
        with pytest.raises(ValueError, match="Unsupported"):
            GitHubHost.enterprise(name)


@pytest.mark.parametrize(
    "remote",
    [
        "git@GH.Corp.:o/r.git",
        "https://gh.corp/o/r.git",
        "https://user@gh.corp/o/r",
        "ssh://git@gh.corp/o/r.git",
        "ssh://git@gh.corp:2222/o/r.git",
        "https://gh.corp:443/o/r",
    ],
)
def test_remote_forms(remote):
    host = GitHubHost.enterprise("gh.corp")
    assert parse_owner_repo(remote, {host.name: host}) == (host, "o", "r")


@pytest.mark.parametrize(
    "remote",
    [
        "https://evil.corp/o/r",
        "http://gh.corp/o/r",
        "https://gh.corp:8443/o/r",
        "https://gh.corp/o/r?x=1",
        "https://gh.corp/o/r#x",
        "https://gh.corp/o/../r",
        "git@gh.corp:o/r/extra",
        "https://gh.corp/o/r%2fevil",
        "ssh://git@gh.corp:bad/o/r",
    ],
)
def test_remote_rejections(remote):
    assert parse_owner_repo(remote, discover_trusted_hosts(HostsEnv(["gh.corp"]))) is None


def test_failed_discovery_preserves_copy_success_replaces():
    previous = discover_trusted_hosts(HostsEnv(["gh.corp"]))
    kept = discover_trusted_hosts(HostsEnv(None), previous)
    assert kept == previous
    assert kept is not previous
    assert set(discover_trusted_hosts(HostsEnv([]), previous)) == {"github.com"}
    assert set(discover_trusted_hosts(HostsEnv(None))) == {"github.com"}


def test_system_config_is_offline_and_discards_values(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.yml").write_text("github.com: {oauth_token: ignored}\ngh.corp: {oauth_token: invalid}\n")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: pytest.fail("discovery invoked gh"))
    assert set(discover_trusted_hosts(SystemEnvironment())) == {"github.com", "gh.corp"}


@pytest.mark.parametrize("body", ["", "{}", "# empty\n"])
def test_empty_config(tmp_path, monkeypatch, body):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.yml").write_text(body)
    assert SystemEnvironment().gh_config_hosts() == []


@pytest.mark.parametrize("body", ["[broken", "- gh.corp", "123", "123: {}", "null", "gh.corp: scalar"])
def test_invalid_mapping_retains_previous(tmp_path, monkeypatch, body):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    (tmp_path / "hosts.yml").write_text(body)
    previous = discover_trusted_hosts(HostsEnv(["old.corp"]))
    assert discover_trusted_hosts(SystemEnvironment(), previous) == previous


def test_missing_config_is_empty_read_error_is_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    assert SystemEnvironment().gh_config_hosts() == []
    (tmp_path / "hosts.yml").mkdir()
    assert SystemEnvironment().gh_config_hosts() is None


def test_config_directory_precedence(tmp_path, monkeypatch):
    for var in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME", "AppData"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default = tmp_path / ".config" / "gh"
    xdg = tmp_path / "xdg" / "gh"
    explicit = tmp_path / "explicit"
    for path, host in ((default, "default.corp"), (xdg, "xdg.corp"), (explicit, "explicit.corp")):
        path.mkdir(parents=True)
        (path / "hosts.yml").write_text(f"{host}: {{}}\n")
    env = SystemEnvironment()
    assert env.gh_config_hosts() == ["default.corp"]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg.parent))
    assert env.gh_config_hosts() == ["xdg.corp"]
    monkeypatch.setenv("GH_CONFIG_DIR", str(explicit))
    assert env.gh_config_hosts() == ["explicit.corp"]
    monkeypatch.setenv("GH_CONFIG_DIR", "")
    assert env.gh_config_hosts() == ["xdg.corp"]


def test_windows_appdata_config_path(tmp_path, monkeypatch):

    directory = tmp_path / "GitHub CLI"
    directory.mkdir()
    (directory / "hosts.yml").write_text("windows.corp: {}\n")
    monkeypatch.setattr(auth, "os", SimpleNamespace(name="nt", environ={"AppData": str(tmp_path)}))
    assert SystemEnvironment().gh_config_hosts() == ["windows.corp"]


@pytest.mark.parametrize("remote", ["https://gh.\ncorp/o/r", "ssh://git@gh.\tcorp/o/r", "git@gh.corp:o/ r"])
def test_whitespace_inside_remote_is_rejected(remote):
    assert parse_owner_repo(remote, discover_trusted_hosts(HostsEnv(["gh.corp"]))) is None

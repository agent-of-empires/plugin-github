"""Git checkout introspection: resolve the GitHub owner/repo and branch from a
working copy, so a handler can talk about the right repository.
"""

from __future__ import annotations

import subprocess

from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.hosts import remote_parts
from aoe_github_plugin.errors import GitHubError


def _git(path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", path, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitHubError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def parse_owner_repo(
    remote_url: str, trusted: dict[str, GitHubHost] | None = None
) -> tuple[GitHubHost, str, str] | None:
    """Extract a trusted GitHub host, owner, and repository from a remote."""
    parts = remote_parts(remote_url)
    if parts is None:
        return None
    name, owner, repo = parts
    hosts = trusted if trusted is not None else {"github.com": GitHubHost.dotcom()}
    host = hosts.get(name)
    return (host, owner, repo) if host is not None else None


def remote_owner_repo(path: str, trusted: dict[str, GitHubHost] | None = None) -> tuple[GitHubHost, str, str] | None:
    return parse_owner_repo(_git(path, "remote", "get-url", "origin"), trusted)


def current_branch(path: str) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD")

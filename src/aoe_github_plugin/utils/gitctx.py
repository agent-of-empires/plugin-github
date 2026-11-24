"""Git checkout introspection: resolve the GitHub owner/repo and branch from a
working copy, so a handler can talk about the right repository.
"""

from __future__ import annotations

import os
import re
import json
import subprocess

from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.hosts import remote_parts
from aoe_github_plugin.errors import GitHubError


def checkout_environment() -> dict[str, str]:
    """Keep auth/config while ensuring Git and gh resolve the requested checkout."""
    excluded = {
        "GH_REPO",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_SHALLOW_FILE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def _git(path: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", path, *args],
        env=checkout_environment(),
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


class GhDiscoveryError(Exception):
    """The optional gh lookup could not produce a trustworthy result."""


def _public_gh_environment(path: str) -> dict[str, str]:
    """Only let the optional CLI discovery resolve public GitHub remotes."""
    env = checkout_environment()
    proc = subprocess.run(
        ["git", "-C", str(path), "remote", "-v"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc.returncode:
        raise GhDiscoveryError
    origin = False
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[2] not in {"(fetch)", "(push)"}:
            raise GhDiscoveryError
        parts = remote_parts(fields[1])
        if parts is None or parts[0] != "github.com":
            raise GhDiscoveryError
        origin |= fields[0] == "origin" and fields[2] == "(fetch)"
    if not origin:
        raise GhDiscoveryError
    env["GH_HOST"] = "github.com"
    env.pop("GH_ENTERPRISE_TOKEN", None)
    env.pop("GITHUB_ENTERPRISE_TOKEN", None)
    return env


def gh_pr_repository(path: str) -> tuple[str, str] | None:
    """Return the open PR destination, or None for a confirmed closed PR."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", "--json", "url,state"],
            cwd=path,
            env=_public_gh_environment(path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode:
            if re.fullmatch(r'no pull requests found for branch "(?:[^"\\\n]|\\.)+"\s*', proc.stderr):
                return None
            raise GhDiscoveryError
        result = json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise GhDiscoveryError from exc
    if not isinstance(result, dict) or result.get("state") not in ("OPEN", "CLOSED", "MERGED"):
        raise GhDiscoveryError
    if result["state"] != "OPEN":
        return None
    url = result.get("url")
    if not isinstance(url, str):
        raise GhDiscoveryError
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/pull/[1-9][0-9]*", url)
    if not match or match[2] in {".", ".."}:
        raise GhDiscoveryError
    return match[1], match[2]


def remote_owner_repos(path: str, trusted: dict[str, GitHubHost] | None = None) -> list[tuple[GitHubHost, str, str]]:
    """Every configured trusted GitHub remote's ``(host, owner, repo)``, origin first.

    The checkout's own PR may live against any of them: a fork's PR targets the
    upstream repository, so only searching origin misses it (#129). Non-GitHub
    remotes are skipped rather than failing the lookup.
    """
    try:
        names = _git(path, "remote").split()
    except GitHubError:
        return []
    out: list[tuple[GitHubHost, str, str]] = []
    for name in sorted(names, key=lambda n: (n != "origin", n)):
        try:
            repo = parse_owner_repo(_git(path, "remote", "get-url", name), trusted)
        except GitHubError:
            continue
        if repo is not None and repo not in out:
            out.append(repo)
    return out


def current_branch(path: str) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD")

"""Git checkout introspection: resolve the GitHub owner/repo and branch from a
working copy, so a handler can talk about the right repository.
"""

from __future__ import annotations

import subprocess

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


def parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a GitHub remote URL, or ``None`` if it is
    not a recognizable github.com remote. Handles ssh and https, with/without
    a ``.git`` suffix.
    """
    url = remote_url.strip()
    prefixes = ("git@github.com:", "https://github.com/", "ssh://git@github.com/")
    path = next((url[len(p) :] for p in prefixes if url.startswith(p)), None)
    if path is None:
        return None
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def remote_owner_repo(path: str) -> tuple[str, str] | None:
    return parse_owner_repo(_git(path, "remote", "get-url", "origin"))


def remote_owner_repos(path: str) -> list[tuple[str, str]]:
    """Every configured GitHub remote's ``(owner, repo)``, origin first.

    The checkout's own PR may live against any of them: a fork's PR targets the
    upstream repository, so only searching origin misses it (#129). Non-GitHub
    remotes are skipped rather than failing the lookup.
    """
    try:
        names = _git(path, "remote").split()
    except GitHubError:
        return []
    out: list[tuple[str, str]] = []
    for name in sorted(names, key=lambda n: (n != "origin", n)):
        try:
            repo = parse_owner_repo(_git(path, "remote", "get-url", name))
        except GitHubError:
            continue
        if repo is not None and repo not in out:
            out.append(repo)
    return out


def current_branch(path: str) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD")

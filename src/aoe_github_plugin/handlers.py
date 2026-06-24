"""The plugin's actual GitHub features, one function per JSON-RPC method."""

from __future__ import annotations

import contextlib

from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.auth import resolve_token
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import GitHubAuthError
from aoe_github_plugin.utils.gitctx import current_branch
from aoe_github_plugin.utils.gitctx import remote_owner_repo


def github_status(path: str = ".", env: TokenEnvironment | None = None) -> str:
    """One-line GitHub summary for the checkout at ``path``.

    Fail-soft: never raises. Resolves the remote and branch, optionally a token
    (unauthenticated if none), queries open PRs for the current branch, and
    returns a short human string. On any failure returns a degraded one-liner
    carrying the first line of the actionable hint, so a status-bar poll always
    has something to show (issue #1667's fail-soft requirement).
    """
    try:
        owner_repo = remote_owner_repo(path)
        if owner_repo is None:
            return "GitHub: not a github.com remote"
        owner, repo = owner_repo
        branch = current_branch(path)
        token = None
        # Auth is optional: fall through to an unauthenticated request if no
        # token resolves, so status stays fail-soft.
        with contextlib.suppress(GitHubAuthError):
            token, _ = resolve_token(env or SystemEnvironment())
        with GitHubClient(token=token) as client:
            pulls = client.get_json(f"/repos/{owner}/{repo}/pulls?state=open&head={owner}:{branch}&per_page=1")
        if pulls:
            pr = pulls[0]
            return f"{owner}/{repo}: PR #{pr['number']} open for {branch}"
        return f"{owner}/{repo}: no open PR for {branch}"
    except GitHubError as exc:
        return f"GitHub: {str(exc).splitlines()[0]}"

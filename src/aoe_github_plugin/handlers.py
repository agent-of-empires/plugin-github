"""The plugin's actual GitHub features, one function per JSON-RPC method.

``github.status`` is fail-soft: it always returns a structured result (never
raises), so a status poll always has something to show (issue #1667).
``github.open`` is an explicit user action: it resolves the URL to open and may
raise a typed ``GitHubError`` carrying an actionable hint when the checkout has
no github.com remote.
"""

from __future__ import annotations

import contextlib
from typing import Any
from urllib.parse import quote

import httpx

from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.auth import resolve_token
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import GitHubAuthError
from aoe_github_plugin.utils.gitctx import current_branch
from aoe_github_plugin.utils.gitctx import remote_owner_repo


def _resolve_optional_token(env: TokenEnvironment | None) -> str | None:
    """A token if one is configured, else ``None`` (unauthenticated requests).

    Auth is optional for read-only status: a missing token is not an error, so
    ``GitHubAuthError`` is swallowed and we fall through to public requests.
    """
    with contextlib.suppress(GitHubAuthError):
        token, _ = resolve_token(env or SystemEnvironment())
        return token
    return None


def _open_pulls_for_branch(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
) -> list[dict[str, Any]]:
    """Open PRs whose head is ``branch``, trimmed to the fields a UI renders."""
    raw = client.get_json(
        f"/repos/{owner}/{repo}/pulls",
        params={"state": "open", "head": f"{owner}:{branch}", "per_page": "10"},
    )
    return [
        {
            "number": pr["number"],
            "url": pr["html_url"],
            "title": pr["title"],
            "state": pr["state"],
            "draft": bool(pr.get("draft", False)),
        }
        for pr in raw
    ]


def github_status(
    path: str = ".",
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Structured GitHub summary for the checkout at ``path``.

    Fail-soft: never raises. Returns ``repo``/``branch``, the open PRs for the
    current branch (each with its ``url`` so a UI can link it), a one-line
    ``summary`` for a status bar, and ``error`` (``None`` on success, else a
    ``{kind, hint}`` so the UI can still show why). ``transport`` is an
    injection seam for tests.
    """
    result: dict[str, Any] = {
        "summary": "",
        "repo": None,
        "branch": None,
        "pulls": [],
        "error": None,
    }
    try:
        owner_repo = remote_owner_repo(path)
        if owner_repo is None:
            result["summary"] = "GitHub: not a github.com remote"
            return result
        owner, repo = owner_repo
        branch = current_branch(path)
        result["repo"] = f"{owner}/{repo}"
        result["branch"] = branch
        token = _resolve_optional_token(env)
        with GitHubClient(token=token, transport=transport) as client:
            pulls = _open_pulls_for_branch(client, owner, repo, branch)
        result["pulls"] = pulls
        if pulls:
            result["summary"] = f"{owner}/{repo}: PR #{pulls[0]['number']} open for {branch}"
        else:
            result["summary"] = f"{owner}/{repo}: no open PR for {branch}"
    except GitHubError as exc:
        result["summary"] = f"GitHub: {str(exc).splitlines()[0]}"
        result["error"] = {"kind": exc.kind, "hint": str(exc)}
    return result


def github_open(
    path: str = ".",
    env: TokenEnvironment | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Resolve the GitHub URL to open for the checkout at ``path``.

    Returns ``{"url", "kind"}`` where ``kind`` is ``"pull"`` (an open PR exists
    for the branch) or ``"compare"`` (the create-PR page for the branch).
    Raises ``GitHubError`` only when there is no github.com remote. Looking up
    an existing PR is best-effort: any API failure falls back to the compare
    URL so opening still works offline / unauthenticated.
    """
    owner_repo = remote_owner_repo(path)
    if owner_repo is None:
        raise GitHubError("not a github.com remote; cannot open in GitHub")
    owner, repo = owner_repo
    branch = current_branch(path)
    with contextlib.suppress(GitHubError):
        token = _resolve_optional_token(env)
        with GitHubClient(token=token, transport=transport) as client:
            pulls = _open_pulls_for_branch(client, owner, repo, branch)
        if pulls:
            return {"url": pulls[0]["url"], "kind": "pull"}
    quoted = quote(branch, safe="")
    return {"url": f"https://github.com/{owner}/{repo}/compare/{quoted}?expand=1", "kind": "compare"}

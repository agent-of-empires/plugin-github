"""Thin typed wrapper over the GitHub REST API, on a synchronous httpx client.

Header-driven error classification is split out as a pure ``classify_status``
function so it is unit-testable without real HTTP, and the httpx transport can
be injected for the same reason. The worker is host-supervised and serial, so a
sync client is the right model (a 4-model design debate confirmed async buys
nothing here).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from aoe_github_plugin.errors import ApiError
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import NetworkError
from aoe_github_plugin.errors import NotFoundError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.errors import UnauthorizedError
from aoe_github_plugin.errors import InsufficientScopeError

DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_USER_AGENT = "agent-of-empires-github-plugin"
DEFAULT_TIMEOUT = 10.0


def _body_message(body: str) -> str:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return data["message"]
    return ""


def classify_status(status: int, headers: Any, body: str) -> GitHubError:
    """Map a non-2xx GitHub response to a typed error.

    ``headers`` is anything with a case-insensitive ``.get(name)`` (an
    ``httpx.Headers`` in production); ``body`` is the response text.
    """
    if status == 401:
        return UnauthorizedError()
    if status in (403, 429):
        remaining = headers.get("X-RateLimit-Remaining")
        if status == 429 or remaining == "0" or "rate limit" in body.lower():
            return RateLimitedError()
        # A 403 that is not rate limiting is a missing OAuth scope. GitHub
        # echoes the scopes the token would need in this header when relevant.
        accepted = headers.get("X-Accepted-OAuth-Scopes") or "repo"
        return InsufficientScopeError(accepted.strip() or "repo")
    if status == 404:
        return NotFoundError(_body_message(body) or "resource")
    return ApiError(status, _body_message(body) or body[:200])


class GitHubClient:
    """A short-lived GitHub REST client. Use as a context manager so the
    underlying connection pool is closed.

    ``token`` is optional: ``None`` issues unauthenticated public requests
    (lower rate limit), matching the fail-soft posture of #1667. ``transport``
    is an injection seam for tests (an ``httpx.MockTransport``).
    """

    def __init__(
        self,
        token: str | None = None,
        api_base: str = DEFAULT_GITHUB_API_BASE,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, path: str) -> Any:
        """GET ``path`` (relative to the API base, or an absolute URL), return
        parsed JSON. Raises a typed ``GitHubError`` subclass on any failure.
        """
        try:
            resp = self._client.get(path)
        except httpx.RequestError as exc:
            raise NetworkError(exc) from exc
        if not resp.is_success:
            raise classify_status(resp.status_code, resp.headers, resp.text)
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(200, f"could not decode response: {exc}") from exc

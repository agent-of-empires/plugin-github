"""GitHub client / auth error taxonomy.

Each exception carries a stable ``kind`` (a machine string the host can branch
on) and a human, actionable ``message`` (== ``str(self)``), never a generic
"auth required". Wording mirrors the #1681 Rust ``GitHubAuthError`` /
``GitHubError`` Display impls.
"""

from __future__ import annotations

from aoe_github_plugin.hosts import GitHubHost


class GitHubError(Exception):
    """Base for every GitHub client / auth failure."""

    kind: str = "github_error"


class GitHubAuthError(GitHubError):
    """Failures while resolving a token from the environment or ``gh``."""

    kind = "auth_error"


class NoTokenNoGhError(GitHubAuthError):
    kind = "no_token_no_gh"

    def __init__(self, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            "No GitHub token found and the GitHub CLI is not installed.\n"
            "Install the GitHub CLI (macOS: brew install gh).\n"
            f"Sign in with: {self.host.login_hint}\n"
            f"Or set a token directly: {self.host.token_hint}."
        )


class GhNotAuthenticatedError(GitHubAuthError):
    kind = "gh_not_authenticated"

    def __init__(self, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            "The GitHub CLI is installed but not authenticated.\n"
            f"Sign in with: {self.host.login_hint}\n"
            f"Or set a token directly: {self.host.token_hint}."
        )


class GhReturnedEmptyTokenError(GitHubAuthError):
    kind = "gh_returned_empty_token"

    def __init__(self, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            "The GitHub CLI returned an empty token.\n"
            f"Sign in with: {self.host.login_hint}\n"
            f"Or set a token directly: {self.host.token_hint}."
        )


class GhCommandFailedError(GitHubAuthError):
    kind = "gh_command_failed"

    def __init__(self, detail: str, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            f"Failed to run the GitHub CLI: {detail}\n"
            f"Sign in with: {self.host.login_hint}\n"
            f"Or set a token directly: {self.host.token_hint}."
        )


class NetworkError(GitHubError):
    kind = "network"

    def __init__(self, detail: object) -> None:
        super().__init__(
            "GitHub API is unreachable.\n"
            "Check your network connection or GitHub status: "
            "https://www.githubstatus.com/\n"
            f"Details: {detail}"
        )


class UnauthorizedError(GitHubError):
    kind = "unauthorized"

    def __init__(self, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            f"GitHub rejected the credentials for {self.host.name} (HTTP 401).\n"
            "The token is missing, invalid, or expired.\n"
            f"Re-authenticate with: {self.host.login_hint}, or {self.host.token_hint}."
        )


class InsufficientScopeError(GitHubError):
    kind = "insufficient_scope"

    def __init__(self, scopes: str, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        self.scopes = scopes
        super().__init__(
            "GitHub token is missing a required scope (HTTP 403).\n"
            f"This operation needs one of: {scopes}.\n"
            "Re-authenticate with a token that carries it, for example:\n"
            f"  {self.host.login_hint} --scopes {scopes}\n"
            f"or {self.host.token_hint} with that scope."
        )


class RateLimitedError(GitHubError):
    kind = "rate_limited"

    def __init__(self, host: GitHubHost | None = None) -> None:
        self.host = host or GitHubHost.dotcom()
        super().__init__(
            f"GitHub API rate limit exceeded for {self.host.name}.\n"
            "Wait for the limit to reset (see the X-RateLimit-Reset header) and retry.\n"
            f"Authenticating raises the limit: {self.host.token_hint} or run {self.host.login_hint}."
        )


class NotFoundError(GitHubError):
    kind = "not_found"

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"GitHub resource not found: {resource}")


class ApiError(GitHubError):
    kind = "api_error"

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"GitHub API returned HTTP {status}: {message}")

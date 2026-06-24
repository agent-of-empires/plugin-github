"""GitHub client / auth error taxonomy.

Each exception carries a stable ``kind`` (a machine string the host can branch
on) and a human, actionable ``message`` (== ``str(self)``), never a generic
"auth required". Wording mirrors the #1681 Rust ``GitHubAuthError`` /
``GitHubError`` Display impls.
"""

from __future__ import annotations


class GitHubError(Exception):
    """Base for every GitHub client / auth failure."""

    kind: str = "github_error"


class GitHubAuthError(GitHubError):
    """Failures while resolving a token from the environment or ``gh``."""

    kind = "auth_error"


class NoTokenNoGhError(GitHubAuthError):
    kind = "no_token_no_gh"

    def __init__(self) -> None:
        super().__init__(
            "No GitHub token found and the GitHub CLI is not installed.\n"
            "Set a token: export GITHUB_TOKEN=<token> (or GH_TOKEN).\n"
            "Or install the GitHub CLI and sign in:\n"
            "  macOS:  brew install gh\n"
            "  Linux:  see https://github.com/cli/cli#installation\n"
            "then run: gh auth login"
        )


class GhNotAuthenticatedError(GitHubAuthError):
    kind = "gh_not_authenticated"

    def __init__(self) -> None:
        super().__init__(
            "The GitHub CLI is installed but not authenticated.\n"
            "Sign in with:\n"
            "  gh auth login\n"
            "Or set a token directly: export GITHUB_TOKEN=<token>."
        )


class GhReturnedEmptyTokenError(GitHubAuthError):
    kind = "gh_returned_empty_token"

    def __init__(self) -> None:
        super().__init__(
            "The GitHub CLI returned an empty token.\n"
            "Re-authenticate with:\n"
            "  gh auth login\n"
            "Or set a token directly: export GITHUB_TOKEN=<token>."
        )


class GhCommandFailedError(GitHubAuthError):
    kind = "gh_command_failed"

    def __init__(self, detail: str) -> None:
        super().__init__(
            f"Failed to run the GitHub CLI: {detail}\nSet a token directly to bypass it: export GITHUB_TOKEN=<token>."
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

    def __init__(self) -> None:
        super().__init__(
            "GitHub rejected the credentials (HTTP 401).\n"
            "The token is missing, invalid, or expired.\n"
            "Re-authenticate with: gh auth login, or set a fresh GITHUB_TOKEN."
        )


class InsufficientScopeError(GitHubError):
    kind = "insufficient_scope"

    def __init__(self, scopes: str) -> None:
        self.scopes = scopes
        super().__init__(
            "GitHub token is missing a required scope (HTTP 403).\n"
            f"This operation needs one of: {scopes}.\n"
            "Re-authenticate with a token that carries it, for example:\n"
            f"  gh auth login --scopes {scopes}\n"
            "or set GITHUB_TOKEN to a personal access token with that scope."
        )


class RateLimitedError(GitHubError):
    kind = "rate_limited"

    def __init__(self) -> None:
        super().__init__(
            "GitHub API rate limit exceeded.\n"
            "Wait for the limit to reset (see the X-RateLimit-Reset header) "
            "and retry.\n"
            "Authenticating raises the limit: set GITHUB_TOKEN or run "
            "gh auth login."
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

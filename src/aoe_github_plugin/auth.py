"""GitHub token resolution.

``gh`` is an optional token *source*, never a hard dependency and never a
per-call shell-out. The resolver finds a token once, in a fixed order:

1. ``GITHUB_TOKEN`` / ``GH_TOKEN`` environment variable.
2. ``gh auth token``, only when ``gh`` is installed and authenticated.
3. Device-flow login as the no-``gh`` fallback (deferred follow-up).

The process environment is abstracted behind ``TokenEnvironment`` so the
resolution order and per-failure hint selection are unit-testable without
touching real env state or needing ``gh`` in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from aoe_github_plugin.errors import NoTokenNoGhError
from aoe_github_plugin.errors import GhCommandFailedError
from aoe_github_plugin.errors import GhNotAuthenticatedError
from aoe_github_plugin.errors import GhReturnedEmptyTokenError

# Hard cap on the `gh auth token` shell-out. `gh` can block (keyring unlock, a
# credential-helper network call, a slow auth backend); without a bound it would
# stall the whole startup refresh, so a hung `gh` degrades to unauthenticated
# rather than freezing the pane.
GH_AUTH_TIMEOUT = 5.0


class TokenEnvironment:
    """Seam over the process environment so resolution is testable."""

    def env_var(self, key: str) -> str | None:
        raise NotImplementedError

    def gh_available(self) -> bool:
        raise NotImplementedError

    def gh_auth_token(self) -> tuple[bool, str, str]:
        """Return ``(success, stdout, stderr)``, or raise ``OSError``."""
        raise NotImplementedError


class SystemEnvironment(TokenEnvironment):
    """Real env vars + the ``gh`` binary."""

    def env_var(self, key: str) -> str | None:
        return os.environ.get(key)

    def gh_available(self) -> bool:
        return shutil.which("gh") is not None

    def gh_auth_token(self) -> tuple[bool, str, str]:
        # TimeoutExpired is NOT an OSError, so resolve_token's `except OSError`
        # would not catch it; map it to a failure tuple here so the caller
        # falls back the same way it does for any other gh failure.
        try:
            proc = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_AUTH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"gh auth token timed out after {GH_AUTH_TIMEOUT:g}s"
        return proc.returncode == 0, proc.stdout, proc.stderr


def resolve_token(env: TokenEnvironment) -> tuple[str, str]:
    """Resolve a token in the fixed order, raising a typed error whose hint
    matches the exact failure when none is available.

    Returns ``(token, source)`` where ``source`` is ``"env:GITHUB_TOKEN"``,
    ``"env:GH_TOKEN"``, or ``"gh-cli"``.
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        raw = env.env_var(key)
        if raw is not None:
            token = raw.strip()
            if token:
                return token, f"env:{key}"

    if not env.gh_available():
        raise NoTokenNoGhError

    try:
        success, stdout, stderr = env.gh_auth_token()
    except OSError as exc:
        raise GhCommandFailedError(str(exc)) from exc

    if success:
        token = stdout.strip()
        if not token:
            raise GhReturnedEmptyTokenError
        return token, "gh-cli"

    # Non-zero exit with the canonical "no oauth token" message (or no stderr)
    # means the user simply is not signed in. Any other stderr is a real gh
    # failure and must not be mislabeled as "not authenticated".
    err = stderr.strip()
    if not err or "no oauth token" in err.lower():
        raise GhNotAuthenticatedError
    raise GhCommandFailedError(err)


def resolve_token_from_system() -> tuple[str, str]:
    return resolve_token(SystemEnvironment())

"""GitHub token resolution.

Tokens are resolved per host on each refresh. Enterprise environment tokens
require a matching GH_HOST; gh's stored-token fallback cannot inherit token
variables, or gh itself would broadcast them to multiple enterprise servers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from aoe_github_plugin.hosts import GitHubHost
from aoe_github_plugin.hosts import canonical_hostname
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

    def gh_config_hosts(self) -> list[str] | None:
        """Offline host names; None means failure, [] means empty config."""
        return []

    def gh_auth_token(self, hostname: str = "github.com") -> tuple[bool, str, str]:
        """Return ``(success, stdout, stderr)``, or raise ``OSError``."""
        raise NotImplementedError


class SystemEnvironment(TokenEnvironment):
    """Real env vars + the ``gh`` binary."""

    def env_var(self, key: str) -> str | None:
        return os.environ.get(key)

    def gh_available(self) -> bool:
        return shutil.which("gh") is not None

    def gh_config_hosts(self) -> list[str] | None:
        directory = self.env_var("GH_CONFIG_DIR")
        if not directory:
            xdg = self.env_var("XDG_CONFIG_HOME")
            appdata = self.env_var("AppData")
            if xdg:
                directory = str(Path(xdg) / "gh")
            elif os.name == "nt" and appdata:
                directory = str(Path(appdata) / "GitHub CLI")
            else:
                directory = str(Path.home() / ".config" / "gh")
        try:
            raw = (Path(directory) / "hosts.yml").read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
            if data is None:
                # Empty documents are valid; an explicit YAML null is not a mapping.
                empty = not raw.strip() or all(
                    line.lstrip().startswith("#") for line in raw.splitlines() if line.strip()
                )
                return [] if empty else None
            if not isinstance(data, dict) or not all(
                isinstance(k, str) and isinstance(v, dict) for k, v in data.items()
            ):
                return None
            return list(data)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, yaml.YAMLError):
            return None

    def gh_auth_token(self, hostname: str = "github.com") -> tuple[bool, str, str]:
        # TimeoutExpired is NOT an OSError, so resolve_token's `except OSError`
        # would not catch it; map it to a failure tuple here so the caller
        # falls back the same way it does for any other gh failure.
        try:
            proc = subprocess.run(
                ["gh", "auth", "token", "--hostname", hostname],
                env={k: v for k, v in os.environ.items() if k not in TOKEN_VARIABLES},
                capture_output=True,
                text=True,
                check=False,
                timeout=GH_AUTH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"gh auth token timed out after {GH_AUTH_TIMEOUT:g}s"
        return proc.returncode == 0, proc.stdout, proc.stderr


TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def resolve_token(env: TokenEnvironment, host: GitHubHost | None = None) -> tuple[str, str]:
    """Resolve a token in the fixed order, raising a typed error whose hint
    matches the exact failure when none is available.

    Returns ``(token, source)`` where ``source`` is ``"env:GITHUB_TOKEN"``,
    ``"env:GH_TOKEN"``, or ``"gh-cli"``.
    """
    host = host or GitHubHost.dotcom()
    keys = ("GH_TOKEN", "GITHUB_TOKEN") if host.kind == "dotcom" else ()
    if host.kind == "ghes" and canonical_hostname(env.env_var("GH_HOST") or "") == host.name:
        keys = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    for key in keys:
        raw = env.env_var(key)
        if raw is not None:
            token = raw.strip()
            if token:
                return token, f"env:{key}"

    if not env.gh_available():
        raise NoTokenNoGhError(host)

    try:
        success, stdout, stderr = env.gh_auth_token(host.name)
    except OSError as exc:
        raise GhCommandFailedError("could not execute gh auth token", host) from exc

    if success:
        token = stdout.strip()
        if not token:
            raise GhReturnedEmptyTokenError(host)
        return token, "gh-cli"

    # Non-zero exit with the canonical "no oauth token" message (or no stderr)
    # means the user simply is not signed in. Any other stderr is a real gh
    # failure and must not be mislabeled as "not authenticated".
    err = stderr.strip()
    if not err or "no oauth token" in err.lower():
        raise GhNotAuthenticatedError(host)
    raise GhCommandFailedError("gh auth token failed (check CLI authentication or keyring)", host)


def resolve_token_from_system() -> tuple[str, str]:
    return resolve_token(SystemEnvironment())

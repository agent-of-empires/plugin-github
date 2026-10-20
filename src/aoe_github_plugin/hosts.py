"""GitHub host identity, offline trust, and credential-routing policy."""

from __future__ import annotations

import re
import ipaddress
from typing import TYPE_CHECKING
from typing import Literal
from dataclasses import dataclass
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from aoe_github_plugin.auth import TokenEnvironment

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9_.-]+\Z")


def canonical_hostname(name: str) -> str | None:
    """Canonical DNS identity; URLs, credentials, ports and IPs are not keys."""
    name = name.lower().removesuffix(".")
    if not name or len(name) > 253 or not all(_HOST_LABEL.fullmatch(part) for part in name.split(".")):
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    return None


def _unsupported(name: str) -> bool:
    return name in ("localhost", "ghe.com") or name.endswith((".localhost", ".ghe.com"))


@dataclass(frozen=True)
class GitHubHost:
    name: str
    rest_base: str
    graphql_url: str
    web_base: str
    kind: Literal["dotcom", "ghes"]

    @classmethod
    def dotcom(cls) -> GitHubHost:
        return cls(
            "github.com", "https://api.github.com", "https://api.github.com/graphql", "https://github.com", "dotcom"
        )

    @classmethod
    def enterprise(cls, name: str) -> GitHubHost:
        canonical = canonical_hostname(name)
        if canonical is None or canonical == "github.com" or _unsupported(canonical):
            raise ValueError("Unsupported GitHub Enterprise Server hostname")
        base = f"https://{canonical}"
        return cls(canonical, f"{base}/api/v3", f"{base}/api/graphql", base, "ghes")

    @property
    def login_hint(self) -> str:
        return "gh auth login" if self.kind == "dotcom" else f"gh auth login --hostname {self.name}"

    @property
    def token_hint(self) -> str:
        if self.kind == "dotcom":
            return "export GH_TOKEN=<token> (overrides GITHUB_TOKEN)"
        return f"export GH_HOST={self.name} GH_ENTERPRISE_TOKEN=<token> (or GITHUB_ENTERPRISE_TOKEN)"


def discover_trusted_hosts(
    env: TokenEnvironment, previous: dict[str, GitHubHost] | None = None
) -> dict[str, GitHubHost]:
    names = env.gh_config_hosts()
    public = GitHubHost.dotcom()
    if names is None:
        return {**(previous or {}), public.name: public}
    trusted = {public.name: public}
    for raw in names:
        name = canonical_hostname(raw)
        if name is not None and name != public.name and not _unsupported(name):
            trusted[name] = GitHubHost.enterprise(name)
    return trusted


def remote_parts(url: str) -> tuple[str, str, str] | None:
    """Parse supported HTTPS/SSH remotes without trusting the resulting host."""
    url = url.strip()
    if any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        if url.startswith("git@") and "://" not in url:
            authority, path = url[4:].split(":", 1)
        else:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("https", "ssh")
                or parsed.query
                or parsed.fragment
                or parsed.password is not None
                or parsed.port == 0
                or (parsed.scheme == "https" and parsed.port not in (None, 443))
            ):
                return None
            authority = parsed.hostname or ""
            path = parsed.path.removeprefix("/")
        name = canonical_hostname(authority)
    except ValueError:
        return None
    parts = path.removesuffix(".git").split("/")
    if name is None or len(parts) != 2 or any(p in (".", "..") or not _REPO_PART.fullmatch(p) for p in parts):
        return None
    return name, parts[0], parts[1]

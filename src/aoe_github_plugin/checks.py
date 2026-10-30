"""Setup checks the worker reports to the host (``suggestions.checks.set``).

Four checks, always in this order: the GitHub CLI is installed, the plugin is
signed in, the configured repositories and organizations are readable, and the
token can read notifications (only the ``mentioned`` criterion needs that).
Nothing here reaches GitHub without a token, and each budget's backoff gate is
honored: a skipped probe reports ``warn``, never ``fail``.
"""

from __future__ import annotations

import time
from typing import Any

from aoe_github_plugin import refresh
from aoe_github_plugin import suggestions
from aoe_github_plugin.auth import TokenEnvironment
from aoe_github_plugin.auth import SystemEnvironment
from aoe_github_plugin.auth import resolve_token
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.errors import ApiError
from aoe_github_plugin.errors import GitHubError
from aoe_github_plugin.errors import GitHubAuthError
from aoe_github_plugin.errors import NoTokenNoGhError
from aoe_github_plugin.errors import RateLimitedError
from aoe_github_plugin.errors import UnauthorizedError
from aoe_github_plugin.errors import InsufficientScopeError

# Host limits (docs/plugin-api.md, Setup checks), in UTF-8 bytes.
ID_BYTES = 256
LABEL_BYTES = 512
TEXT_BYTES = 2048
# Scope entries per GraphQL document, to stay well inside GitHub's complexity limits.
SCOPE_CHUNK = 40
# At most this many entries are looked up (five round trips); the rest is reported, not checked.
MAX_SCOPE_ENTRIES = SCOPE_CHUNK * 5
# A 200 envelope carries these per-alias when the token cannot see an entry; any other
# error type is transient or account-wide and must not read as "grant access".
READ_DENIED = frozenset({"NOT_FOUND", "FORBIDDEN"})
SIGN_IN_FIX = "Run `gh auth login`, or set GITHUB_TOKEN."
SKIPPED = "Skipped until the plugin is signed in to GitHub."
UNKNOWN_SOURCES = "Could not read which sources are on."


def check(check_id: str, label: str, status: str, detail: str = "", fix: str = "") -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": suggestions.clamp(check_id, ID_BYTES),
        "label": suggestions.clamp(label, LABEL_BYTES),
        "status": status,
    }
    if detail:
        check["detail"] = suggestions.clamp(detail, TEXT_BYTES)
    if fix:
        check["fix"] = suggestions.clamp(fix, TEXT_BYTES)
    return check


def _backed_off(api: str) -> bool:
    return time.monotonic() < refresh.backoff_until(api)


def _first_line(exc: Exception) -> str:
    return (str(exc).splitlines() or [type(exc).__name__])[0]


def _scope_chunk(entries: list[tuple[str, str]], *, first: bool) -> tuple[str, dict[str, str]]:
    """One GraphQL document looking up every entry through its own alias, with
    the names passed as variables, plus ``viewer`` in the first document."""
    decls: list[str] = []
    fields = ["viewer { login }"] if first else []
    variables: dict[str, str] = {}
    for index, (kind, name) in enumerate(entries):
        if kind == "repo":
            owner, repo = name.split("/", 1)
            decls += [f"$o{index}: String!", f"$n{index}: String!"]
            variables |= {f"o{index}": owner, f"n{index}": repo}
            fields.append(f"a{index}: repository(owner: $o{index}, name: $n{index}) {{ id }}")
        else:
            decls.append(f"$l{index}: String!")
            variables[f"l{index}"] = name
            fields.append(f"a{index}: repositoryOwner(login: $l{index}) {{ login }}")
    args = f"({', '.join(decls)})" if decls else ""
    return f"query{args} {{ {' '.join(fields)} }}", variables


def _raise_unless_denied(errors: Any) -> None:
    """Let per-alias NOT_FOUND/FORBIDDEN errors through (their alias is null);
    raise the first other error so its message reaches the user."""
    if not isinstance(errors, list):
        return
    for error in errors:
        entry = error if isinstance(error, dict) else {}
        path = entry.get("path")
        if entry.get("type") in READ_DENIED and isinstance(path, list) and path:
            continue
        raise ApiError(200, str(entry.get("message") or "GitHub returned an error"))


def _lookup(client: GitHubClient, entries: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """``(viewer login, entries the token cannot see)``; a null alias is unreadable."""
    chunks = [entries[i : i + SCOPE_CHUNK] for i in range(0, len(entries), SCOPE_CHUNK)] or [[]]
    login = ""
    unreadable: list[str] = []
    for number, chunk in enumerate(chunks):
        query, variables = _scope_chunk(chunk, first=number == 0)
        envelope = client.post_graphql(query, variables)
        if refresh.arm_if_graphql_rate_limited(envelope):
            raise RateLimitedError
        data = envelope.get("data")
        if not isinstance(data, dict):
            # e.g. SAML enforcement answers data: null; keep GitHub's own message.
            if envelope.get("errors"):
                _raise_unless_denied(envelope["errors"])
            raise TypeError("GitHub returned an invalid response")
        _raise_unless_denied(envelope.get("errors"))
        if number == 0:
            viewer = data.get("viewer")
            found = viewer.get("login") if isinstance(viewer, dict) else None
            if not isinstance(found, str) or not found:
                raise TypeError("GitHub returned an invalid viewer")
            login = found
        unreadable += [name for index, (_, name) in enumerate(chunk) if data.get(f"a{index}") is None]
    return login, unreadable


def _raw_count(raw: Any) -> int:
    """Distinct non-empty entries in a comma or newline separated setting."""
    if not isinstance(raw, str):
        return 0
    return len({part.strip().lower() for part in raw.replace("\n", ",").split(",") if part.strip()})


def _graphql_checks(
    client: GitHubClient, repos: set[str], orgs: set[str], *, ignored: int = 0
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """``(logged_in, read_repositories, rejected)`` from one batched lookup; the
    viewer proves the token, a null alias is an entry the token cannot see."""
    entries = [("repo", repo) for repo in sorted(repos)] + [("owner", org) for org in sorted(orgs)]
    unchecked = max(0, len(entries) - MAX_SCOPE_ENTRIES)
    entries = entries[:MAX_SCOPE_ENTRIES]
    try:
        login, unreadable = _lookup(client, entries)
    except UnauthorizedError:
        fix = "Sign in again with `gh auth login`, or set a fresh GITHUB_TOKEN."
        return (
            check("logged_in", "Signed in to GitHub", "fail", "GitHub rejected the token.", fix),
            check("read_repositories", "Can read repositories", "warn", SKIPPED),
            True,
        )
    except RateLimitedError:
        # An envelope rate limit already armed the gate with its exact reset; never shorten it.
        if not _backed_off("graphql"):
            refresh.arm_backoff("graphql")
        return _unverified("GitHub rate limited; will check again later.")
    except (GitHubError, TypeError) as exc:
        return _unverified(_first_line(exc))
    signed_in = check("logged_in", "Signed in to GitHub", "ok", f"Signed in as {login}.")
    notes = []
    if unchecked:
        notes.append(f"and {unchecked} more not checked.")
    if ignored:
        notes.append(f"{ignored} entries ignored (invalid owner/repo).")
    note = "".join(f" {text}" for text in notes)
    if unreadable:
        fix = "Grant the token access to them, or remove them from the repository and organization settings."
        detail = f"Not found or not visible to the token: {', '.join(unreadable)}.{note}"
        return signed_in, check("read_repositories", "Can read repositories", "fail", detail, fix), False
    if ignored:
        fix = "Use `owner/repo` for repositories and a plain name for organizations, separated by commas."
        detail = f"{len(entries)} entries exist and are visible.{note}" if entries else f"No valid scope.{note}"
        return signed_in, check("read_repositories", "Can read repositories", "warn", detail, fix), False
    if entries:
        detail = f"{len(entries)} configured entries exist and are visible to the token.{note}"
    else:
        detail = "No scope configured; every repository the token can see."
    return signed_in, check("read_repositories", "Can read repositories", "ok", detail), False


def _unverified(reason: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
    return (
        check("logged_in", "Signed in to GitHub", "warn", f"A token was found but not verified: {reason}"),
        check("read_repositories", "Can read repositories", "warn", f"Not verified: {reason}"),
        False,
    )


def _notifications_check(client: GitHubClient) -> dict[str, Any]:
    label = "Notifications scope"
    if _backed_off("rest"):
        return check("notifications_scope", label, "warn", "GitHub rate limited; will check again later.")
    try:
        client.get_json("/notifications", {"per_page": "1"})
    except InsufficientScopeError:
        fix = "Run `gh auth refresh -s notifications`, or give GITHUB_TOKEN the notifications scope."
        detail = "The token cannot read notifications, so mentions are not suggested."
        return check("notifications_scope", label, "fail", detail, fix)
    except RateLimitedError:
        refresh.arm_backoff("rest")
        return check("notifications_scope", label, "warn", "GitHub rate limited; will check again later.")
    except GitHubError as exc:
        return check("notifications_scope", label, "warn", f"Not verified: {_first_line(exc)}")
    return check("notifications_scope", label, "ok", "The token can read notifications.")


def run_checks(
    *,
    mentioned_enabled: bool | None,
    repos_raw: Any,
    orgs_raw: Any,
    env: TokenEnvironment | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the four checks. ``mentioned_enabled`` says whether the host has
    the ``mentioned`` source on (``None``: the host did not say); with it off or
    unknown the notifications probe is skipped."""
    env = env or SystemEnvironment()
    installed = env.gh_available()
    token: str | None = None
    failure: GitHubAuthError | None = None
    try:
        token, _ = resolve_token(env)
    except GitHubAuthError as exc:
        failure = exc
    if installed:
        cli = check("gh_installed", "GitHub CLI installed", "ok")
    else:
        # gh is only a token source: a token from the environment makes it optional.
        cli = check(
            "gh_installed",
            "GitHub CLI installed",
            "warn" if token else "fail",
            "The `gh` command was not found." + (" A token from the environment is in use." if token else ""),
            "Install the GitHub CLI from https://cli.github.com/.",
        )
    # What the notifications check says without probing; None means it must probe.
    if mentioned_enabled is None:
        unprobed: dict[str, Any] | None = check("notifications_scope", "Notifications scope", "warn", UNKNOWN_SOURCES)
    elif not mentioned_enabled:
        unprobed = check("notifications_scope", "Notifications scope", "ok", "Not needed: mentions are off.")
    else:
        unprobed = None
    skipped = check("notifications_scope", "Notifications scope", "warn", SKIPPED)
    if token is None:
        fix = (
            "Install the GitHub CLI and run `gh auth login`, or set GITHUB_TOKEN."
            if isinstance(failure, NoTokenNoGhError)
            else SIGN_IN_FIX
        )
        return [
            cli,
            check("logged_in", "Signed in to GitHub", "fail", _first_line(failure or RuntimeError("no token")), fix),
            check("read_repositories", "Can read repositories", "warn", SKIPPED),
            unprobed or skipped,
        ]
    repos = suggestions.parse_csv(repos_raw, suggestions.REPO_RE)
    orgs = suggestions.parse_csv(orgs_raw, suggestions.ORG_RE)
    ignored = _raw_count(repos_raw) - len(repos) + _raw_count(orgs_raw) - len(orgs)
    with GitHubClient(token) as client:
        if _backed_off("graphql"):
            logged_in, readable, rejected = _unverified("GitHub rate limited; will check again later.")
        else:
            logged_in, readable, rejected = _graphql_checks(client, repos, orgs, ignored=ignored)
        if unprobed:
            notifications = unprobed
        elif rejected:
            notifications = skipped
        else:
            notifications = _notifications_check(client)
    return [cli, logged_in, readable, notifications]

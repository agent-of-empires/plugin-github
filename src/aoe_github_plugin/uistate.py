"""Turn a refresh snapshot into host ``ui.state.set`` params (pure, no IO).

The host renders the slots; the worker only pushes typed display state. From one
aggregate snapshot (see ``refresh.build_snapshot``) this produces one
``status-bar`` push (global, no ``session_id``) plus one ``row-badge`` push per
session (per-session, carrying ``session_id``). Each ``payload`` is the host's
``TextPayload`` -- ``{text, tone, tooltip}`` only, parsed ``deny_unknown_fields``
-- and ``tone`` is one of the host's ``Tone`` set.

Tone is a severity cascade ``danger > success > warn > neutral``:
- ``danger``  a hard error is present (auth failure, rate limit, network/API
  error) -- something the user must act on;
- ``success`` at least one open non-draft PR and no hard error;
- ``warn``    only draft PRs;
- ``neutral`` no PRs (or only benign non-github checkouts / detached HEADs).

Benign states (a workspace subdir with no github.com remote, a detached HEAD, a
branch with no PR) are NOT errors and never raise the tone above neutral.
"""

from __future__ import annotations

from typing import Any

GLOBAL_SLOT = ("status-bar", "github_status")
SESSION_SLOT = ("row-badge", "github_pr_badge")

# Tooltip detail is bounded so a many-repo workspace cannot push a huge string.
_MAX_TOOLTIP_LINES = 12


def _has_hard_error(repos: list[dict[str, Any]]) -> bool:
    return any(r.get("error") for r in repos)


def _count_open(repos: list[dict[str, Any]]) -> int:
    return sum(1 for r in repos for p in (r.get("pulls") or []) if not p.get("draft"))


def _count_drafts(repos: list[dict[str, Any]]) -> int:
    return sum(1 for r in repos for p in (r.get("pulls") or []) if p.get("draft"))


def _tone(repos: list[dict[str, Any]]) -> str:
    if _has_hard_error(repos):
        return "danger"
    if _count_open(repos):
        return "success"
    if _count_drafts(repos):
        return "warn"
    return "neutral"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _text(repos: list[dict[str, Any]]) -> str:
    if _has_hard_error(repos):
        return "GitHub !"
    opened = _count_open(repos)
    if opened:
        return _plural(opened, "PR")
    drafts = _count_drafts(repos)
    if drafts:
        return _plural(drafts, "draft")
    if not repos:
        return "no repos"
    if not any(r.get("repo") for r in repos):
        return "no GitHub"
    return "no PRs"


def _repo_line(repo: dict[str, Any]) -> str:
    name = repo.get("name") or repo.get("path") or "repo"
    error = repo.get("error")
    if error:
        return f"{name}: {str(error.get('hint', 'error')).splitlines()[0]}"
    if not repo.get("repo"):
        return f"{name}: not on GitHub"
    if repo.get("branch") is None:
        return f"{name}: detached HEAD"  # no branch was looked up; do not claim "no PR"
    pulls = repo.get("pulls") or []
    if not pulls:
        return f"{name}: no PR"
    pull = pulls[0]
    kind = "draft PR" if pull.get("draft") else "PR"
    return f"{name}: {kind} #{pull.get('number', '?')} {pull.get('title', '')}".rstrip()


def _tooltip(header: str, repos: list[dict[str, Any]]) -> str:
    lines = [header]
    shown = repos[:_MAX_TOOLTIP_LINES]
    lines += [_repo_line(r) for r in shown]
    if len(repos) > len(shown):
        lines.append(f"... +{len(repos) - len(shown)} more")
    return "\n".join(lines)


def _payload(text: str, tone: str, tooltip: str) -> dict[str, Any]:
    return {"text": text, "tone": tone, "tooltip": tooltip}


def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
    repos = session.get("repos") or []
    header = session.get("title") or session.get("session_id") or "session"
    return _payload(_text(repos), _tone(repos), _tooltip(header, repos))


def _global_payload(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    all_repos = [r for s in sessions for r in (s.get("repos") or [])]
    opened = _count_open(all_repos)
    base = _text(all_repos)  # "N PRs" / "N drafts" / "no PRs" / "no repos" / "GitHub !"
    text = base if base == "GitHub !" else f"GitHub: {base}"
    summary = f"{opened} open PRs across {len(sessions)} sessions / {len(all_repos)} repos"
    lines = [summary]
    for session in sessions:
        repos = session.get("repos") or []
        name = session.get("title") or session.get("session_id") or "session"
        lines.append(f"{name}: {_text(repos)}")
    tooltip = "\n".join(lines[: _MAX_TOOLTIP_LINES + 1])
    return _payload(text, _tone(all_repos), tooltip)


def snapshot_ui_state_params(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """``ui.state.set`` params for a whole refresh snapshot: one ``row-badge``
    per session (with ``session_id``) plus one global ``status-bar`` (without).
    Pure and total: a missing/partial snapshot still yields a valid global push.
    """
    sessions = snapshot.get("sessions") or []
    params: list[dict[str, Any]] = []
    for session in sessions:
        sid = session.get("session_id")
        if sid is None:
            continue
        params.append(
            {
                "slot": SESSION_SLOT[0],
                "id": SESSION_SLOT[1],
                "session_id": sid,
                "payload": _session_payload(session),
            }
        )
    params.append(
        {
            "slot": GLOBAL_SLOT[0],
            "id": GLOBAL_SLOT[1],
            "payload": _global_payload(sessions),
        }
    )
    return params

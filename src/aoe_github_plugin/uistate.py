"""Turn a refresh snapshot into host ``ui.state.set`` params (pure, no IO).

The host renders the slots; the worker only pushes typed display state. From one
aggregate snapshot (see ``refresh.build_snapshot``) this produces, per session,
two pushes:

- a ``row-badge`` whose payload is ``{"items": [...]}`` -- one compact, colored,
  clickable PR icon per repo that has an open/draft PR (or an error marker).
  A multi-repo workspace shows several icons on its row; clicking one opens that
  PR. Repos with no PR are omitted from the row (the pane carries the detail).
- a ``pane`` (the in-session GitHub tool-window, opened in the right dock by
  default via ``default_location``) whose payload is a flexible
  ``{"title", "blocks": [...]}`` block list. Blocks are a small, extensible
  vocabulary (``heading``, ``row``, ``note``, ``divider``); the host renders the
  kinds it knows and ignores the rest, so this plugin can grow the pane (review
  state, CI, checks, ...) without a lockstep host change.

Icons are lucide names (the web frontend already depends on ``lucide-react``);
``tone`` is one of the host's ``Tone`` set and colors the icon. A PR's state maps
to ``(icon, tone)`` via ``_status``.
"""

from __future__ import annotations

from typing import Any

ROW_BADGE_SLOT = ("row-badge", "github_pr_badge")
PANE_SLOT = ("pane", "github_pane")
# Dock the GitHub pane opens in by default; the user can move it after.
PANE_DEFAULT_LOCATION = "right"

# PR state -> (lucide icon name, host Tone). Hard errors get an alert icon.
_ICON_OPEN = "git-pull-request-arrow"
_ICON_DRAFT = "git-pull-request-draft"
_ICON_ERROR = "circle-alert"


def _status(repo: dict[str, Any]) -> tuple[str, str] | None:
    """``(lucide_icon, tone)`` for a repo's headline state, or ``None`` when it
    has nothing worth a row badge (no PR, or a benign non-github checkout)."""
    if repo.get("error"):
        return _ICON_ERROR, "danger"
    pulls = repo.get("pulls") or []
    if any(not p.get("draft") for p in pulls):
        return _ICON_OPEN, "success"
    if pulls:
        return _ICON_DRAFT, "warn"
    return None


def _first_pull(repo: dict[str, Any]) -> dict[str, Any] | None:
    pulls = repo.get("pulls") or []
    # Prefer a real (non-draft) PR for the click target / headline.
    return next((p for p in pulls if not p.get("draft")), pulls[0] if pulls else None)


def _badge_tooltip(repo: dict[str, Any]) -> str:
    name = repo.get("name") or repo.get("repo") or "repo"
    if repo.get("error"):
        return f"{name}: {str(repo['error'].get('hint', 'error')).splitlines()[0]}"
    pull = _first_pull(repo)
    if pull is None:
        return f"{name}: no open PR"
    kind = "draft PR" if pull.get("draft") else "PR"
    return f"{name}: {kind} #{pull.get('number', '?')} {pull.get('title', '')}".rstrip()


def _badge_items(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One compact badge per repo with a PR or an error; others omitted."""
    items: list[dict[str, Any]] = []
    for repo in repos:
        status = _status(repo)
        if status is None:
            continue
        icon, tone = status
        item: dict[str, Any] = {"icon": icon, "tone": tone, "tooltip": _badge_tooltip(repo)}
        pull = _first_pull(repo)
        if pull and pull.get("url"):
            item["href"] = pull["url"]
        items.append(item)
    return items


def _pane_row(repo: dict[str, Any]) -> dict[str, Any]:
    """One ``row`` block: repo name, headline value, state icon/tone, open link."""
    name = repo.get("name") or repo.get("repo") or "repo"
    row: dict[str, Any] = {"kind": "row", "label": name}
    status = _status(repo)
    if status is not None:
        row["icon"], row["tone"] = status

    if repo.get("error"):
        row["value"] = str(repo["error"].get("hint", "error")).splitlines()[0]
        return row
    if not repo.get("repo"):
        row["value"] = "not a GitHub remote"
        return row

    branch = repo.get("branch")
    pull = _first_pull(repo)
    if pull is None:
        row["value"] = "no open PR"
        row["sublabel"] = f"{repo['repo']} · {branch}" if branch else repo["repo"]
        return row
    kind = "draft PR" if pull.get("draft") else "PR"
    row["value"] = f"{kind} #{pull.get('number', '?')} {pull.get('title', '')}".rstrip()
    if pull.get("url"):
        row["href"] = pull["url"]
    row["sublabel"] = f"{repo['repo']} · {branch}" if branch else repo["repo"]
    return row


def _pane_blocks(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"kind": "heading", "text": "GitHub"}]
    if not repos:
        blocks.append({"kind": "note", "text": "no repos in this workspace", "tone": "neutral"})
        return blocks
    blocks.extend(_pane_row(repo) for repo in repos)
    return blocks


def snapshot_ui_state_params(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """``ui.state.set`` params for a refresh snapshot: per session, a ``row-badge``
    (one icon per PR) and a ``pane`` (the GitHub tool-window). No global slot.
    Pure and total: a missing/partial snapshot yields no pushes rather than raising.
    """
    sessions = snapshot.get("sessions") or []
    params: list[dict[str, Any]] = []
    for session in sessions:
        sid = session.get("session_id")
        if sid is None:
            continue
        repos = session.get("repos") or []
        params.append(
            {
                "slot": ROW_BADGE_SLOT[0],
                "id": ROW_BADGE_SLOT[1],
                "session_id": sid,
                "payload": {"items": _badge_items(repos)},
            }
        )
        params.append(
            {
                "slot": PANE_SLOT[0],
                "id": PANE_SLOT[1],
                "session_id": sid,
                "payload": {
                    "title": "GitHub",
                    "default_location": PANE_DEFAULT_LOCATION,
                    "blocks": _pane_blocks(repos),
                },
            }
        )
    return params

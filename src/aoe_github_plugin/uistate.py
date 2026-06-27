"""Turn a refresh snapshot into host ``ui.state.set`` params (pure, no IO).

The host renders the slots; the worker only pushes typed display state. From one
aggregate snapshot (see ``refresh.build_snapshot``) this produces, per session,
two pushes:

- a ``row-badge`` whose payload is ``{"items": [...]}`` -- one compact, colored,
  clickable PR icon per repo that has an open/draft PR (or an error marker).
  Merged-only repos are omitted: the badge is an actionable indicator, and a
  merged PR (often on a branch left around after merge) is noise there. A
  multi-repo workspace shows several icons on its row; clicking one opens that
  PR. The pane carries the full detail.
- a ``pane`` (the in-session GitHub tool-window, opened in the right dock by
  default via ``default_location``) whose payload is a flexible
  ``{"title", "blocks": [...]}`` block list. Per PR the pane shows a headline
  row (MERGED in purple, since no semantic tone names that hue), a review-state
  row, a Checks section listing each CI run, and an unresolved-comments section.
  These reuse the host's generic block vocabulary (``heading``/``row``/
  ``section``/``note``/``divider``/``action``) plus one read-only ``comment``
  block; the host renders the kinds it knows and ignores the rest, so the pane
  can grow without a lockstep host change.

The rich fields (review_state/checks/comments/merged) are present only when the
worker had a GitHub token; without one the snapshot's ``auth.present`` is
``False``, the pulls are the basic open-PR shape, and the pane shows a banner
note telling the user a token unlocks the rest.

Icons are lucide names (the web frontend already depends on ``lucide-react``);
``tone`` is one of the host's ``Tone`` set and colors the icon, while ``color``
is an optional validated hex the web applies where a tone cannot name the hue.
"""

from __future__ import annotations

import json
from typing import Any

from aoe_github_plugin.graphql import MERGED_COLOR

ROW_BADGE_SLOT = ("row-badge", "github_pr_badge")
PANE_SLOT = ("pane", "github_pane")
# Dock the GitHub pane opens in by default; the user can move it after.
PANE_DEFAULT_LOCATION = "right"
# Lucide icon for the pane's activity-bar button (host resolves against its
# allowlist, else a generic plugin icon). Lucide dropped its GitHub brand mark.
PANE_ICON = "message-square-diff"

# PR state -> (lucide icon name, host Tone). Hard errors get an alert icon.
_ICON_OPEN = "git-pull-request-arrow"
_ICON_DRAFT = "git-pull-request-draft"
_ICON_MERGED = "git-merge"
_ICON_ERROR = "circle-alert"

# review_state -> (lucide icon, host Tone, human label).
_REVIEW_VISUAL: dict[str, tuple[str, str, str]] = {
    "approved": ("badge-check", "success", "approved"),
    "changes-requested": ("circle-x", "danger", "changes requested"),
    "commented": ("message-square", "info", "commented"),
    "waiting": ("clock", "neutral", "awaiting review"),
}

# check state -> (lucide icon, host Tone, human label). Shared by the per-run
# rows and the rollup summary in the Checks section title.
_CHECK_VISUAL: dict[str, tuple[str, str, str]] = {
    "succeeded": ("circle-check", "success", "passing"),
    "failing": ("circle-x", "danger", "failing"),
    "running": ("loader", "info", "running"),
    "queued": ("clock", "neutral", "queued"),
    "unknown": ("circle-help", "neutral", "unknown"),
}

# Per-PR caps so one pathological PR cannot blow past the host's 8KB/entry limit.
_MAX_CHECK_ROWS = 20
_MAX_COMMENTS = 10
# Whole-pane block budget (bytes of JSON), under the host's 8KB/entry cap. A pane
# that would exceed it (a many-repo workspace) is trimmed to fit rather than
# rejected wholesale by the host, which would blank the pane.
_PANE_BUDGET = 7000


def _is_merged(pull: dict[str, Any]) -> bool:
    return bool(pull.get("merged")) or str(pull.get("state") or "").upper() == "MERGED"


def _open_pulls(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (repo.get("pulls") or []) if not _is_merged(p)]


def _status(repo: dict[str, Any]) -> tuple[str, str] | None:
    """``(lucide_icon, tone)`` for a repo's headline state, or ``None`` when it
    has nothing worth a row badge (no OPEN PR, or a benign non-github checkout).
    Merged-only repos return ``None`` so they stay out of the actionable row."""
    if repo.get("error"):
        return _ICON_ERROR, "danger"
    open_pulls = _open_pulls(repo)
    if any(not p.get("draft") for p in open_pulls):
        return _ICON_OPEN, "success"
    if open_pulls:
        return _ICON_DRAFT, "warn"
    return None


def _first_pull(repo: dict[str, Any]) -> dict[str, Any] | None:
    """The PR a badge clicks through to: prefer a real (non-draft) open PR, then
    any open PR, then whatever exists (so a merged-only repo still has a target
    in the pane)."""
    open_pulls = _open_pulls(repo)
    if open_pulls:
        return next((p for p in open_pulls if not p.get("draft")), open_pulls[0])
    pulls = repo.get("pulls") or []
    return pulls[0] if pulls else None


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
    """One compact badge per repo with an OPEN/draft PR or an error; others
    (including merged-only repos) are omitted."""
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


def _pull_visual(pull: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """``(icon, tone, color)`` for a PR headline. Merged uses a hex ``color``
    (purple) because no semantic tone names it; open/draft use a tone."""
    if _is_merged(pull):
        return _ICON_MERGED, None, MERGED_COLOR
    if pull.get("draft"):
        return _ICON_DRAFT, "warn", None
    return _ICON_OPEN, "success", None


def _headline_row(repo_name: str, pull: dict[str, Any]) -> dict[str, Any]:
    icon, tone, color = _pull_visual(pull)
    prefix = "MERGED" if _is_merged(pull) else "Draft PR" if pull.get("draft") else "PR"
    row: dict[str, Any] = {
        "kind": "row",
        "label": repo_name,
        "value": f"{prefix} #{pull.get('number', '?')} {pull.get('title', '')}".rstrip(),
        "icon": icon,
    }
    if tone:
        row["tone"] = tone
    if color:
        row["color"] = color
    if pull.get("url"):
        row["href"] = pull["url"]
    return row


def _review_row(review: Any) -> dict[str, Any] | None:
    if review not in _REVIEW_VISUAL:
        return None
    icon, tone, label = _REVIEW_VISUAL[review]
    return {"kind": "row", "label": "Review", "value": label, "icon": icon, "tone": tone}


def _checks_section(checks: Any) -> dict[str, Any] | None:
    if not isinstance(checks, dict):
        return None
    rollup_icon, rollup_tone, rollup_label = _CHECK_VISUAL.get(checks.get("state") or "", _CHECK_VISUAL["unknown"])
    runs = checks.get("runs") or []
    children: list[dict[str, Any]] = []
    for run in runs[:_MAX_CHECK_ROWS]:
        icon, tone, label = _CHECK_VISUAL.get(run.get("state") or "", _CHECK_VISUAL["unknown"])
        child: dict[str, Any] = {
            "kind": "row",
            "label": run.get("name") or "check",
            "value": label,
            "icon": icon,
            "tone": tone,
        }
        if run.get("url"):
            child["href"] = run["url"]
        children.append(child)
    title = f"Checks: {rollup_label}"
    if len(runs) > _MAX_CHECK_ROWS:
        title += f" ({len(runs)} total)"
    # Icon + tone on the title give an at-a-glance rollup state even when folded.
    section: dict[str, Any] = {
        "kind": "section",
        "title": title,
        "children": children,
        "collapsible": True,
        "icon": rollup_icon,
        "tone": rollup_tone,
    }
    # Fold when everything passed; stay open when something needs attention
    # (failing/running/queued/unknown) so the actionable rows are visible.
    if checks.get("state") == "succeeded":
        section["collapsed"] = True
    return section


def _comment_block(item: dict[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {
        "kind": "comment",
        "author": item.get("author") or "",
        "body": item.get("body") or "",
        "resolved": bool(item.get("resolved")),
    }
    if item.get("path"):
        block["path"] = item["path"]
    if isinstance(item.get("line"), int):
        block["line"] = item["line"]
    if item.get("url"):
        block["href"] = item["url"]
    return block


def _comments_section(comments: Any) -> dict[str, Any] | None:
    if not isinstance(comments, dict) or not comments.get("unresolved"):
        return None
    items = comments.get("items") or []
    children = [_comment_block(item) for item in items[:_MAX_COMMENTS]]
    # Open by default: the section only exists when there are unresolved
    # comments, so surface them. Still collapsible for the user to fold away.
    return {
        "kind": "section",
        "title": f"Unresolved comments: {comments['unresolved']}",
        "children": children,
        "collapsible": True,
    }


def _pull_detail_blocks(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """Review/CI/comment blocks for one PR. Empty for the basic (no-token) shape,
    where these fields are absent, so the same code renders either source.

    A merged PR is a terminal state: its headline row already says MERGED, and
    its review decision / CI rollup / unresolved threads are historical, not
    actionable, so they are suppressed to avoid presenting a closed PR as live.
    """
    if _is_merged(pull):
        return []
    candidates = (
        _review_row(pull.get("review_state")),
        _checks_section(pull.get("checks")),
        _comments_section(pull.get("comments")),
    )
    return [b for b in candidates if b is not None]


def _pane_repo_blocks(repo: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocks for one repo: an error/empty row, or a headline plus detail per PR."""
    name = repo.get("name") or repo.get("repo") or "repo"
    branch = repo.get("branch")
    sublabel = f"{repo['repo']} · {branch}" if repo.get("repo") and branch else repo.get("repo")

    if repo.get("error"):
        row: dict[str, Any] = {"kind": "row", "label": name, "icon": _ICON_ERROR, "tone": "danger"}
        row["value"] = str(repo["error"].get("hint", "error")).splitlines()[0]
        return [row]
    if not repo.get("repo"):
        return [{"kind": "row", "label": name, "value": "not a GitHub remote"}]

    pulls = repo.get("pulls") or []
    if not pulls:
        empty: dict[str, Any] = {"kind": "row", "label": name, "value": "no open PR"}
        if sublabel:
            empty["sublabel"] = sublabel
        return [empty]

    blocks: list[dict[str, Any]] = []
    for pull in pulls:
        head = _headline_row(name, pull)
        if sublabel:
            head["sublabel"] = sublabel
        blocks.append(head)
        blocks.extend(_pull_detail_blocks(pull))
    return blocks


# A button the host renders in the pane; clicking it forwards github.refresh to
# this worker (host POST /api/plugins/{id}/action -> stdin notification), which
# re-fetches and re-pushes. The worker already handles github.refresh.
_REFRESH_ACTION = {
    "kind": "action",
    "label": "Refresh",
    "icon": "refresh-cw",
    "method": "github.refresh",
}

_TOKEN_NOTE = {
    "kind": "note",
    "tone": "warn",
    "text": (
        "No GitHub token: showing open PRs only. Set GITHUB_TOKEN or run `gh auth login` "
        "to see review state, CI checks, unresolved comments, and merged PRs."
    ),
}


def _fit_to_budget(
    head: list[dict[str, Any]], middle: list[dict[str, Any]], tail: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep ``head`` + ``tail`` (heading/banner + divider/refresh) always; admit
    ``middle`` (per-repo) blocks until the JSON byte budget is hit, appending a
    truncation note when some are dropped. Fail-soft against the host's per-entry
    size cap: a too-big pane is trimmed, not rejected and blanked."""
    used = len(json.dumps(head)) + len(json.dumps(tail))
    kept: list[dict[str, Any]] = []
    truncated = False
    for block in middle:
        size = len(json.dumps(block))
        if used + size > _PANE_BUDGET:
            truncated = True
            break
        kept.append(block)
        used += size
    if truncated:
        kept.append({"kind": "note", "text": "more not shown (truncated to fit)", "tone": "neutral"})
    return head + kept + tail


def _pane_blocks(repos: list[dict[str, Any]], *, auth_present: bool) -> list[dict[str, Any]]:
    head: list[dict[str, Any]] = [{"kind": "heading", "text": "GitHub"}]
    # Only nag when there is actually a GitHub repo whose detail the token gates.
    if not auth_present and any(repo.get("repo") for repo in repos):
        head.append(dict(_TOKEN_NOTE))
    tail = [{"kind": "divider"}, dict(_REFRESH_ACTION)]
    if not repos:
        middle = [{"kind": "note", "text": "no repos in this workspace", "tone": "neutral"}]
    else:
        middle = []
        for repo in repos:
            middle.extend(_pane_repo_blocks(repo))
    return _fit_to_budget(head, middle, tail)


def snapshot_ui_state_params(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """``ui.state.set`` params for a refresh snapshot: per session, a ``row-badge``
    (one icon per PR) and a ``pane`` (the GitHub tool-window). No global slot.
    Pure and total: a missing/partial snapshot yields no pushes rather than raising.
    """
    sessions = snapshot.get("sessions") or []
    auth_present = bool((snapshot.get("auth") or {}).get("present", True))
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
                    "icon": PANE_ICON,
                    "blocks": _pane_blocks(repos, auth_present=auth_present),
                },
            }
        )
    return params

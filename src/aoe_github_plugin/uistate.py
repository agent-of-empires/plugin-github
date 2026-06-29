"""Turn a refresh snapshot into host ``ui.state.set`` params (pure, no IO).

The host renders the slots; the worker only pushes typed display state. From one
aggregate snapshot (see ``refresh.build_snapshot``) this produces one global
sort option plus three pushes per session:

- a global ``sort-key`` whose payload points at the ``github_pr_status``
  ``row-column`` and sorts by the same PR attention rank the row already uses.
  The host keeps the sorting client-side over the pushed scalar values.

- a ``row-badge`` whose payload is ``{"items": [...]}`` -- a chip sequence per
  open PR (a PR affordance, then review-state, CI-rollup, and unresolved-comment
  chips, each shown only when a token supplies that field), concatenated across a
  workspace's repos, plus an error marker per failed repo. Each chip is colored by
  tone (failing CI / changes requested danger, unresolved comments warn, healthy
  success), so the row distinguishes a broken PR from a healthy one at a glance
  (#36). A draft shows the PR chip alone; merged-only repos are omitted, since the
  badge is an actionable indicator. Every chip's href opens that PR; the pane
  carries the full detail.
- a ``row-column`` whose payload is ``{"text", "tone", "tooltip",
  "sort_value"?}`` (or ``{}`` to clear) -- one deterministic words
  summary of the session's most-urgent PR signal, so the list is scannable
  without hovering the badge or opening the pane. Multi-repo workspaces collapse
  to their single highest-attention candidate; the pane keeps the per-repo
  breakdown.
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
from datetime import datetime
from datetime import timezone

from aoe_github_plugin.graphql import MERGED_COLOR

ROW_BADGE_SLOT = ("row-badge", "github_pr_badge")
# A single per-session text cell summarizing the highest-attention PR state, so
# the session list is scannable without opening the pane. The badge (per repo)
# carries the icon; this carries the words.
ROW_COLUMN_SLOT = ("row-column", "github_pr_status")
SORT_KEY_SLOT = ("sort-key", "github_pr_attention")
SORT_KEY_PAYLOAD = {"label": "GitHub PR attention", "column": ROW_COLUMN_SLOT[1], "direction": "asc"}
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

# PR attention kind -> (rank, lucide icon, host Tone, compact label). Lower rank
# = more attention, so it wins the badge icon and the row-column summary. The
# order follows issue #36: changes-requested outranks everything, failing checks
# outrank running/queued, unresolved comments surface even without a formal
# review. Glyphs/tones reuse the pane vocabulary above so the row and pane agree.
# "open" is a healthy non-rich PR (no token, or nothing notable); "draft" is WIP.
_ATTENTION_VISUAL: dict[str, tuple[int, str, str, str]] = {
    "error": (0, _ICON_ERROR, "danger", "error"),
    "changes-requested": (1, *_REVIEW_VISUAL["changes-requested"][:2], "changes requested"),
    "checks-failing": (2, *_CHECK_VISUAL["failing"][:2], "CI failing"),
    "unresolved": (3, "message-square", "warn", "unresolved comments"),
    "checks-running": (4, *_CHECK_VISUAL["running"][:2], "CI running"),
    "checks-queued": (5, *_CHECK_VISUAL["queued"][:2], "CI queued"),
    "awaiting-review": (6, *_REVIEW_VISUAL["waiting"][:2], "awaiting review"),
    "commented": (7, *_REVIEW_VISUAL["commented"][:2], "commented"),
    "approved": (8, *_REVIEW_VISUAL["approved"][:2], "approved"),
    "open": (9, _ICON_OPEN, "success", "open PR"),
    "draft": (10, _ICON_DRAFT, "warn", "draft"),
}


# Which toggle category an attention kind belongs to. A kind absent here (PR
# affordance, errors) is always shown -- not user-suppressible. The settings let
# a user hide a whole category from the session row; the pane still shows it.
_KIND_CATEGORY: dict[str, str] = {
    "changes-requested": "review",
    "awaiting-review": "review",
    "commented": "review",
    "approved": "review",
    "checks-failing": "ci",
    "checks-running": "ci",
    "checks-queued": "ci",
    "unresolved": "comments",
}
# Every chip category enabled: the default, and the behavior when no settings are
# supplied (keeps the function pure and total for callers/tests that pass none).
_ALL_CHIPS = frozenset(_KIND_CATEGORY.values())


def _pull_attention(pull: dict[str, Any], chips: frozenset[str]) -> str:
    """The attention kind for one non-merged PR, in issue #36 priority order,
    skipping any kind whose category the user disabled (``chips``). The rich
    (review_state/checks/comments) keys are absent in the no-token shape, so a
    token-less PR falls through to ``open``/``draft`` and the row degrades to the
    basic view rather than mislabeling state it cannot see."""
    if pull.get("draft"):
        return "draft"
    review = pull.get("review_state")
    checks = pull.get("checks")
    cstate = checks.get("state") if isinstance(checks, dict) else None
    comments = pull.get("comments")
    unresolved = comments.get("unresolved") if isinstance(comments, dict) else 0
    # Priority ladder (issue #36); first enabled match wins. A no-token PR matches
    # none of these (rich keys absent) and falls through to the healthy "open".
    rules: tuple[tuple[bool, str], ...] = (
        (review == "changes-requested", "changes-requested"),
        (cstate == "failing", "checks-failing"),
        (bool(unresolved), "unresolved"),
        (cstate == "running", "checks-running"),
        (cstate == "queued", "checks-queued"),
        (review == "waiting", "awaiting-review"),
        (review == "commented", "commented"),
        (review == "approved", "approved"),
    )
    for cond, kind in rules:
        if cond and _KIND_CATEGORY[kind] in chips:
            return kind
    return "open"


def _top_attention_pull(repo: dict[str, Any], chips: frozenset[str]) -> tuple[dict[str, Any], str] | None:
    """The ``(pull, kind)`` of the highest-attention open PR in a repo, or ``None``
    when it has no open (non-merged) PR. Drives the badge icon/tone and href."""
    candidates = [(p, _pull_attention(p, chips)) for p in _open_pulls(repo)]
    if not candidates:
        return None
    return min(candidates, key=lambda c: _ATTENTION_VISUAL[c[1]][0])


# Per-PR caps so one pathological PR cannot blow past the host's 64KB/entry limit.
_MAX_CHECK_ROWS = 20
# Generous cap: send every unresolved comment in practice, but keep a backstop
# against a pathological PR. _fit_to_budget is the real size guard.
_MAX_COMMENTS = 500
# Whole-pane block budget (bytes of JSON), under the host's 64KB/entry cap (with
# headroom). A pane that would exceed it (a many-repo workspace) is trimmed to
# fit rather than rejected wholesale by the host, which would blank the pane.
_PANE_BUDGET = 60000


def _is_merged(pull: dict[str, Any]) -> bool:
    return bool(pull.get("merged")) or str(pull.get("state") or "").upper() == "MERGED"


def _open_pulls(repo: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in (repo.get("pulls") or []) if not _is_merged(p)]


def _badge_tooltip(repo: dict[str, Any], chips: frozenset[str]) -> str:
    """The row-column tooltip for a repo: its highest-attention open PR with the
    state in parens, the error hint, or a no-PR note."""
    name = repo.get("name") or repo.get("repo") or "repo"
    if repo.get("error"):
        return f"{name}: {str(repo['error'].get('hint', 'error')).splitlines()[0]}"
    top = _top_attention_pull(repo, chips)
    if top is None:
        return f"{name}: no open PR"
    pull, kind = top
    label = _ATTENTION_VISUAL[kind][3]
    head = f"{name}: PR #{pull.get('number', '?')} {pull.get('title', '')}".rstrip()
    return f"{head} ({label})"


def _chip(icon: str, tone: str, tooltip: str, href: str | None) -> dict[str, Any]:
    chip: dict[str, Any] = {"icon": icon, "tone": tone, "tooltip": tooltip}
    if href:
        chip["href"] = href
    return chip


def _pr_badge_chips(repo_name: str, pull: dict[str, Any], chips_on: frozenset[str]) -> list[dict[str, Any]]:
    """The chip sequence for one non-merged PR (#36): a PR affordance, then review,
    CI, and unresolved-comment indicators, each present only when a token supplies
    that field and its category is enabled (``chips_on``). A no-token PR is the PR
    chip alone, so the row stays uncluttered. Every chip's href opens the PR; its
    tooltip carries the words."""
    href = pull.get("url")
    head = f"{repo_name}: PR #{pull.get('number', '?')} {pull.get('title', '')}".rstrip()

    if pull.get("draft"):
        # a draft is WIP: skip review/CI/comment noise until it opens.
        return [_chip(_ICON_DRAFT, "warn", f"{head} (draft)", href)]
    chips = [_chip(_ICON_OPEN, "success", head, href)]

    review_state = pull.get("review_state")
    review = _REVIEW_VISUAL.get(review_state) if isinstance(review_state, str) else None
    if review and "review" in chips_on:
        icon, tone, label = review
        chips.append(_chip(icon, tone, f"{head} ({label})", href))

    checks = pull.get("checks")
    cstate = checks.get("state") if isinstance(checks, dict) else None
    if isinstance(cstate, str) and "ci" in chips_on:
        icon, tone, label = _CHECK_VISUAL.get(cstate, _CHECK_VISUAL["unknown"])
        chips.append(_chip(icon, tone, f"{head} (CI {label})", href))

    comments = pull.get("comments")
    unresolved = comments.get("unresolved") if isinstance(comments, dict) else 0
    if unresolved and "comments" in chips_on:
        chips.append(_chip("message-square", "warn", f"{head} ({unresolved} unresolved)", href))

    return chips


def _badge_items(repos: list[dict[str, Any]], chips_on: frozenset[str]) -> list[dict[str, Any]]:
    """Per session: a chip sequence per open PR (PR + review + CI + comments), an
    error marker per failed repo, concatenated across repos. Merged-only repos and
    non-github checkouts contribute nothing, keeping the badge actionable."""
    items: list[dict[str, Any]] = []
    for repo in repos:
        if repo.get("error"):
            items.append(_chip(_ICON_ERROR, "danger", _badge_tooltip(repo, chips_on), None))
            continue
        name = repo.get("name") or repo.get("repo") or "repo"
        for pull in _open_pulls(repo):
            items.extend(_pr_badge_chips(name, pull, chips_on))
    return items


def _status_candidate(
    repos: list[dict[str, Any]], chips_on: frozenset[str]
) -> tuple[int, dict[str, Any], str | None] | None:
    """Highest-attention row-column payload plus the PR href the badge opens."""
    best: tuple[int, dict[str, Any], str | None] | None = None
    for repo in repos:
        tooltip = _badge_tooltip(repo, chips_on)
        href: str | None = None
        if repo.get("error"):
            rank, _icon, tone, label = _ATTENTION_VISUAL["error"]
            cell: dict[str, Any] = {"text": label, "tone": tone, "tooltip": tooltip, "sort_value": rank}
        else:
            top = _top_attention_pull(repo, chips_on)
            if top is None:
                continue
            pull, kind = top
            rank, _icon, tone, label = _ATTENTION_VISUAL[kind]
            cell = {"text": label, "tone": tone, "tooltip": tooltip, "sort_value": rank}
            href = pull.get("url") if isinstance(pull.get("url"), str) else None
        if best is None or rank < best[0]:
            best = (rank, cell, href)
    return best


def _status_column(repos: list[dict[str, Any]], chips_on: frozenset[str], *, show_text: bool = True) -> dict[str, Any]:
    """A single per-session summary cell: the highest-attention PR (or repo error)
    across the workspace, as ``{text, tone, tooltip, sort_value}``. Returns ``{}``
    when there is nothing worth showing (no open PR, no error), which clears any
    stale row state on the next push. A disabled category (``chips_on``) is skipped
    here too, so a hidden chip never resurfaces as the column text. When
    ``show_text`` is false, keep an empty text field plus the sort scalar so the
    host validates the row-column payload and the sidebar sort still works without
    rendering words.

    ponytail: one winning candidate, not per-repo detail. A multi-repo workspace
    collapses to its single most-urgent signal here; the pane keeps the full
    breakdown. Add per-repo cells only if multi-repo rows prove confusing.
    """
    candidate = _status_candidate(repos, chips_on)
    if candidate is None:
        return {}
    if show_text:
        return candidate[1]
    return {"text": "", "sort_value": candidate[0]}


def _status_href(repos: list[dict[str, Any]], chips_on: frozenset[str]) -> str | None:
    candidate = _status_candidate(repos, chips_on)
    return candidate[2] if candidate else None


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


def _pane_repo_attention_rank(repo: dict[str, Any]) -> int | None:
    """Rank for actionable repos in the pane. ``None`` means no open PR signal."""
    if repo.get("error"):
        return _ATTENTION_VISUAL["error"][0]
    top = _top_attention_pull(repo, _ALL_CHIPS)
    if top is None:
        return None
    return _ATTENTION_VISUAL[top[1]][0]


def _pane_repo_blocks_ordered(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(repos))
    active: list[tuple[int, int, dict[str, Any]]] = []
    inactive: list[tuple[int, dict[str, Any]]] = []
    for index, repo in indexed:
        rank = _pane_repo_attention_rank(repo)
        if rank is None:
            inactive.append((index, repo))
        else:
            active.append((rank, index, repo))

    blocks: list[dict[str, Any]] = []
    for _, _, repo in sorted(active, key=lambda item: (item[0], item[1])):
        blocks.extend(_pane_repo_blocks(repo))

    inactive_blocks: list[dict[str, Any]] = []
    for _, repo in inactive:
        inactive_blocks.extend(_pane_repo_blocks(repo))
    if inactive_blocks and active and len(repos) > 1:
        blocks.append(
            {
                "kind": "section",
                "title": f"Repos without open PRs ({len(inactive)})",
                "children": inactive_blocks,
                "collapsible": True,
                "collapsed": True,
                "tone": "neutral",
            }
        )
    else:
        blocks.extend(inactive_blocks)
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


def _format_refreshed_at(value: Any) -> str | None:
    """ISO UTC timestamp to compact pane text."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).strftime("%H:%M UTC")


def _freshness_block(freshness: Any) -> dict[str, Any] | None:
    """Pane row describing when the displayed GitHub data was last refreshed."""
    if not isinstance(freshness, dict):
        return None
    value = _format_refreshed_at(freshness.get("refreshed_at"))
    if value is None:
        return None
    stale = freshness.get("stale") is True
    return {
        "kind": "row",
        "label": "Last successful refresh" if stale else "Last refreshed",
        "value": value,
        "icon": "clock",
        "tone": "warn" if stale else "neutral",
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


def _pane_blocks(repos: list[dict[str, Any]], *, auth_present: bool, freshness: Any = None) -> list[dict[str, Any]]:
    head: list[dict[str, Any]] = [{"kind": "heading", "text": "GitHub"}]
    # Only nag when there is actually a GitHub repo whose detail the token gates.
    if not auth_present and any(repo.get("repo") for repo in repos):
        head.append(dict(_TOKEN_NOTE))
    tail = [{"kind": "divider"}]
    freshness_row = _freshness_block(freshness)
    if freshness_row is not None:
        tail.append(freshness_row)
    tail.append(dict(_REFRESH_ACTION))
    if not repos:
        middle = [{"kind": "note", "text": "no repos in this workspace", "tone": "neutral"}]
    else:
        middle = _pane_repo_blocks_ordered(repos)
    return _fit_to_budget(head, middle, tail)


def snapshot_ui_state_params(
    snapshot: dict[str, Any], *, chips_on: frozenset[str] = _ALL_CHIPS, show_column: bool = True
) -> list[dict[str, Any]]:
    """``ui.state.set`` params for a refresh snapshot: per session, a ``row-badge``
    (a chip sequence per PR), a ``row-column`` (the status summary), and a ``pane``
    (the GitHub tool-window), plus one global ``sort-key``. ``chips_on`` is the set
    of enabled chip categories (``review``/``ci``/``comments``); a disabled one is
    hidden from both the badge and the column. ``show_column`` False hides the
    words while preserving the sort scalar. Pure and total: a missing/partial
    snapshot yields no pushes rather than raising.
    """
    raw_sessions = snapshot.get("sessions")
    if not isinstance(raw_sessions, list):
        return []
    sessions = raw_sessions
    auth_present = bool((snapshot.get("auth") or {}).get("present", True))
    params: list[dict[str, Any]] = [
        {"slot": SORT_KEY_SLOT[0], "id": SORT_KEY_SLOT[1], "payload": dict(SORT_KEY_PAYLOAD)}
    ]
    for session in sessions:
        sid = session.get("session_id")
        if sid is None:
            continue
        repos = session.get("repos") or []
        # The row-badge also carries a top-level href: the highest-attention PR's
        # url. The host's `open_pr` command (open-ui-link) opens it, so the badge
        # is the always-present anchor (unlike the row-column, which the
        # show_status_text toggle can clear). Reuse the column's winner so the
        # icon, the words, and the opened PR all agree.
        badge_payload: dict[str, Any] = {"items": _badge_items(repos, chips_on)}
        status_payload = _status_column(repos, chips_on, show_text=show_column)
        primary_href = _status_href(repos, chips_on)
        if primary_href:
            badge_payload["href"] = primary_href
        params.append(
            {
                "slot": ROW_BADGE_SLOT[0],
                "id": ROW_BADGE_SLOT[1],
                "session_id": sid,
                "payload": badge_payload,
            }
        )
        params.append(
            {
                "slot": ROW_COLUMN_SLOT[0],
                "id": ROW_COLUMN_SLOT[1],
                "session_id": sid,
                "payload": status_payload,
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
                    "blocks": _pane_blocks(repos, auth_present=auth_present, freshness=session.get("freshness")),
                },
            }
        )
    return params

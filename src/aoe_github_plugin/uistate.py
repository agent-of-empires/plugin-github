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
  default via ``default_location``) whose payload is a
  ``{"title", "blocks": [...], "footer"}`` block list. The pane is one-PR-focused:
  a selector lists every PR in the session (most-actionable first, each row
  carrying its number, branch/author, and a CI/review/conflict glyph strip), and
  clicking a row fires ``github.select_pr`` so the detail below it re-points at
  that PR. The detail is a merge-verdict ``callout``, a Review card (one row per
  reviewer), a Checks card (count pills, the runs needing attention listed
  outright, the passes folded into a collapsible group), an unresolved-comments
  section, a ``columns`` row pairing the diff summary with any linked issues, and
  an Activity timeline. The footer pins the refresh time and the verdict in a word.
  These reuse the host's generic block vocabulary (``heading``/``row``/``section``/
  ``note``/``divider``/``action``/``callout``/``bar``/``columns``) plus one
  read-only ``comment`` block; the host renders the kinds it knows and ignores the
  rest, so the pane can grow without a lockstep host change. The block kinds and
  fields used here need ``api_version >= 12``, which the manifest declares.

The merge verdict is derived here, not fetched: GitHub's ``mergeStateStatus``
collapses conflicts, policy, CI and review into one enum, so it cannot say which
one is blocking (see ``graphql.merge_state``, #80). The ladder in
``_merge_verdict`` orders blockers by who can clear them.

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
# order follows issue #36 (#80 adds conflicts): a merge conflict outranks
# everything, a hard blocker regardless of review; then changes-requested,
# failing checks outrank running/queued, unresolved comments surface even
# without a formal review. Glyphs/tones reuse the pane vocabulary above so the
# row and pane agree. "open" is a healthy non-rich PR (no token, or nothing
# notable); "draft" is WIP.
_ATTENTION_VISUAL: dict[str, tuple[int, str, str, str]] = {
    "error": (0, _ICON_ERROR, "danger", "error"),
    "conflicts": (1, "triangle-alert", "danger", "conflicts"),
    "changes-requested": (2, *_REVIEW_VISUAL["changes-requested"][:2], "changes requested"),
    "checks-failing": (3, *_CHECK_VISUAL["failing"][:2], "CI failing"),
    "unresolved": (4, "message-square", "warn", "unresolved comments"),
    "checks-running": (5, *_CHECK_VISUAL["running"][:2], "CI running"),
    "checks-queued": (6, *_CHECK_VISUAL["queued"][:2], "CI queued"),
    "awaiting-review": (7, *_REVIEW_VISUAL["waiting"][:2], "awaiting review"),
    "commented": (8, *_REVIEW_VISUAL["commented"][:2], "commented"),
    "approved": (9, *_REVIEW_VISUAL["approved"][:2], "approved"),
    "open": (10, _ICON_OPEN, "success", "open PR"),
    "draft": (11, _ICON_DRAFT, "warn", "draft"),
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
    # A merge conflict is a hard blocker that no reviewer can clear, so it wins
    # over the whole toggle ladder and is never user-suppressible (kept out of
    # _KIND_CATEGORY, matched here before the ladder so it cannot KeyError).
    if pull.get("merge_state") == "conflicts":
        return "conflicts"
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

    # Conflicts are ungated (not a suppressible category): a merge-blocked PR
    # always shows the danger chip. Exact match, so a malformed/future value
    # never trips a false conflict.
    if pull.get("merge_state") == "conflicts":
        chips.append(_chip("triangle-alert", "danger", f"{head} (conflicts)", href))

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


# The worker method a PR row fires to change which PR the pane details. The host
# forwards it with the row's ``params``, so one method serves every row.
SELECT_METHOD = "github.select_pr"


def _pull_key(repo: dict[str, Any], pull: dict[str, Any]) -> str:
    """Stable id for one PR within a session. Round-trips through the selector
    row's ``params`` and back as the worker's remembered selection, so it has to
    be derived from the snapshot alone (no index into a list that reorders)."""
    return f"{repo.get('repo') or repo.get('name') or '?'}#{pull.get('number', '?')}"


def _session_pulls(repos: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every PR in the session as ``(repo, pull)``, most-actionable first.

    Ranked per PR rather than per repo (the pane's selector is a flat list across
    the workspace), using the same attention ladder the session row uses so the
    two orders agree. Merged PRs sink below every open one: they are history, not
    work. Ties keep snapshot order, which keeps the list stable between refreshes.
    """
    ranked: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    order = 0
    for repo in repos:
        for pull in repo.get("pulls") or []:
            if _is_merged(pull):
                rank = len(_ATTENTION_VISUAL)
            else:
                rank = _ATTENTION_VISUAL[_pull_attention(pull, _ALL_CHIPS)][0]
            ranked.append((rank, order, repo, pull))
            order += 1
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [(repo, pull) for _, _, repo, pull in ranked]


def _unresolved_count(pull: dict[str, Any]) -> int:
    comments = pull.get("comments")
    count = comments.get("unresolved") if isinstance(comments, dict) else 0
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _pr_signals(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """The compact glyph strip on a PR selector row: CI, review, conflicts and an
    unresolved-comment count. Each entry is tone-colored with the words in its
    tooltip, so a run of rows is scannable without reading any of them. Empty for
    the no-token shape, where none of these fields exist."""
    signals: list[dict[str, Any]] = []
    checks = pull.get("checks")
    cstate = checks.get("state") if isinstance(checks, dict) else None
    if isinstance(cstate, str):
        icon, tone, label = _CHECK_VISUAL.get(cstate, _CHECK_VISUAL["unknown"])
        signals.append({"icon": icon, "tone": tone, "tooltip": f"CI {label}"})
    review = pull.get("review_state")
    visual = _REVIEW_VISUAL.get(review) if isinstance(review, str) else None
    if visual is not None:
        icon, tone, label = visual
        signals.append({"icon": icon, "tone": tone, "tooltip": label})
    if pull.get("merge_state") == "conflicts":
        signals.append({"icon": "triangle-alert", "tone": "danger", "tooltip": "conflicts with base branch"})
    unresolved = _unresolved_count(pull)
    if unresolved:
        signals.append(
            {"icon": "message-square", "tone": "warn", "text": str(unresolved), "tooltip": f"{unresolved} unresolved"}
        )
    return signals


def _pr_row(repo: dict[str, Any], pull: dict[str, Any], *, selected: bool) -> dict[str, Any]:
    """One row of the pane's PR selector: number, title, the branch/author line,
    and the signal strip. Clicking the row body re-points the pane's detail at that
    PR; the trailing href stays a separate affordance so selecting never navigates
    away."""
    icon, tone, color = _pull_visual(pull)
    row: dict[str, Any] = {
        "kind": "row",
        "prefix": f"#{pull.get('number', '?')}",
        "label": pull.get("title") or "",
        "icon": icon,
        "mono": True,
        "method": SELECT_METHOD,
        "params": {"pr": _pull_key(repo, pull)},
        "selected": selected,
    }
    sublabel = " · ".join(str(x) for x in (repo.get("branch"), pull.get("author")) if x)
    if sublabel:
        row["sublabel"] = sublabel
    signals = _pr_signals(pull)
    if signals:
        row["badges"] = signals
    if tone:
        row["tone"] = tone
    if color:
        row["color"] = color
    if pull.get("url"):
        row["href"] = pull["url"]
    return row


def _check_buckets(checks: Any) -> dict[str, list[dict[str, Any]]] | None:
    """Split a check summary's runs into the display buckets the pane shows, or
    ``None`` when there is no check data. ``skipped`` is carved out of the passing
    set (a skipped run counts as passing for the rollup but is not a real pass), and
    ``attention`` is the concatenation the pane leaves expanded."""
    if not isinstance(checks, dict):
        return None
    runs = [r for r in (checks.get("runs") or []) if isinstance(r, dict)]
    failing = [r for r in runs if r.get("state") == "failing"]
    running = [r for r in runs if r.get("state") == "running"]
    queued = [r for r in runs if r.get("state") == "queued"]
    unknown = [r for r in runs if r.get("state") == "unknown"]
    passing = [r for r in runs if r.get("state") == "succeeded" and not r.get("skipped")]
    skipped = [r for r in runs if r.get("state") == "succeeded" and r.get("skipped")]
    return {
        "failing": failing,
        "running": running,
        "queued": queued,
        "unknown": unknown,
        "passing": passing,
        "skipped": skipped,
        "attention": failing + running + queued + unknown,
    }


def _check_row(run: dict[str, Any], *, compact: bool) -> dict[str, Any]:
    """One check row. The expanded (attention) form leads with a state glyph and
    carries ``workflow · duration`` beneath the name; the ``compact`` form, used
    inside the folded passing group, drops the glyph (the group header already says
    they passed) and pins the duration right."""
    icon, tone, _label = _CHECK_VISUAL.get(run.get("state") or "", _CHECK_VISUAL["unknown"])
    name = run.get("name") or "check"
    row: dict[str, Any] = {"kind": "row", "label": name, "mono": True}
    if run.get("url"):
        row["href"] = run["url"]
    if compact:
        if run.get("duration"):
            row["value"] = str(run["duration"])
        return row
    row["icon"] = icon
    row["tone"] = tone
    detail = " · ".join(str(x) for x in (run.get("group"), run.get("duration")) if x)
    if detail:
        row["sublabel"] = detail
    if isinstance(run.get("required"), bool):
        row["sublabel"] = f"{detail} · required" if detail and run["required"] else row.get("sublabel", detail)
    return row


def _check_count_badges(buckets: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The count pills in the Checks header, in severity order. Only non-empty
    buckets appear, so a green PR shows one pill rather than a row of zeros."""
    pills: list[dict[str, Any]] = []
    for key, tone, word in (
        ("failing", "danger", "failing"),
        ("running", "warn", "running"),
        ("queued", "neutral", "queued"),
        ("unknown", "neutral", "unknown"),
        ("passing", "success", "passing"),
        ("skipped", "neutral", "skipped"),
    ):
        count = len(buckets[key])
        if count:
            pills.append({"text": f"{count} {word}", "tone": tone})
    return pills


def _checks_section(checks: Any) -> dict[str, Any] | None:
    """The Checks card: count pills in the header, the runs that need attention
    listed outright, and the passing ones folded into a collapsible group so a
    twenty-check repo does not bury everything below it. The body scrolls in place
    rather than pushing the rest of the pane away."""
    buckets = _check_buckets(checks)
    if buckets is None:
        return None
    children: list[dict[str, Any]] = [_check_row(run, compact=False) for run in buckets["attention"][:_MAX_CHECK_ROWS]]
    passing = buckets["passing"]
    if passing:
        children.append(
            {
                "kind": "section",
                "title": f"{len(passing)} checks passing" if len(passing) != 1 else "1 check passing",
                "icon": _CHECK_VISUAL["succeeded"][0],
                "tone": "success",
                "collapsible": True,
                # Folded by default: a pass needs no reading, and the attention
                # rows above it are the point of the card.
                "collapsed": True,
                "children": [_check_row(run, compact=True) for run in passing[:_MAX_CHECK_ROWS]],
            }
        )
    skipped = buckets["skipped"]
    if skipped:
        children.append(
            {
                "kind": "row",
                "icon": "circle-slash",
                "label": f"{len(skipped)} skipped" if len(skipped) != 1 else "1 skipped",
                "tone": "neutral",
            }
        )
    if not children:
        return None
    return {
        "kind": "section",
        "title": "Checks",
        "badges": _check_count_badges(buckets),
        "boxed": True,
        "scroll": True,
        "children": children,
    }


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
        "boxed": True,
    }


def _review_section(pull: dict[str, Any]) -> dict[str, Any] | None:
    """The Review card: one row per reviewer with their initials and current
    position, under the summary GitHub itself would show. Falls back to the single
    aggregate review-state row when per-reviewer data is absent (the no-token shape,
    or a PR nobody has been asked to review)."""
    people = [p for p in (pull.get("reviewers") or []) if isinstance(p, dict) and p.get("name")]
    if not people:
        state = pull.get("review_state")
        visual = _REVIEW_VISUAL.get(state) if isinstance(state, str) else None
        if visual is None:
            return None
        icon, tone, label = visual
        return {
            "kind": "section",
            "title": "Review",
            "value": label,
            "value_tone": tone,
            "boxed": True,
            "children": [{"kind": "row", "label": label, "icon": icon, "tone": tone}],
        }
    children: list[dict[str, Any]] = []
    for person in people:
        icon, tone, label = _REVIEW_VISUAL.get(person.get("state") or "", _REVIEW_VISUAL["waiting"])
        children.append(
            {
                "kind": "row",
                "avatar": _initials(str(person["name"])),
                "label": str(person["name"]),
                "value": label,
                "tone": tone,
                "icon": icon,
            }
        )
    summary = pull.get("review_summary") or ""
    section: dict[str, Any] = {"kind": "section", "title": "Review", "boxed": True, "children": children}
    if summary:
        section["value"] = summary
        section["value_tone"] = _REVIEW_VISUAL.get(pull.get("review_state") or "", _REVIEW_VISUAL["waiting"])[1]
    return section


def _initials(name: str) -> str:
    """Up to two initials from a display name, for a reviewer row's avatar bubble.
    A single-word handle contributes its first character, so every reviewer gets
    a non-empty bubble."""
    words = [w for w in name.replace("-", " ").replace("_", " ").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _context_columns(pull: dict[str, Any]) -> dict[str, Any] | None:
    """The side-by-side Diff and Linked cards. Either can be absent (no diff stats
    without a token, no linked issues on most PRs); a lone survivor spans the full
    width rather than leaving a gap, and with neither the block is omitted."""
    cards: list[dict[str, Any]] = []
    diff = pull.get("diff")
    if isinstance(diff, dict):
        added = diff.get("added", 0)
        removed = diff.get("removed", 0)
        files = diff.get("files", 0)
        cards.append(
            {
                "kind": "section",
                "title": "Diff",
                "badges": [
                    {"text": f"+{added}", "tone": "success"},
                    {"text": f"-{removed}", "tone": "danger"},
                ],
                "boxed": True,
                "children": [
                    {
                        "kind": "bar",
                        "segments": [
                            {"value": added, "tone": "success"},
                            {"value": removed, "tone": "danger"},
                        ],
                        "caption": f"{files} files" if files != 1 else "1 file",
                    }
                ],
            }
        )
    issues = [i for i in (pull.get("issues") or []) if isinstance(i, dict)]
    if issues:
        rows: list[dict[str, Any]] = []
        for issue in issues:
            closed = issue.get("state") == "closed"
            row: dict[str, Any] = {
                "kind": "row",
                "prefix": f"#{issue.get('number', '?')}",
                "label": issue.get("title") or "",
                "icon": "circle-check" if closed else "circle-dot",
                "tone": "neutral" if closed else "success",
                "mono": True,
            }
            if issue.get("url"):
                row["href"] = issue["url"]
            rows.append(row)
        cards.append({"kind": "section", "title": "Linked", "boxed": True, "children": rows})
    if not cards:
        return None
    return {"kind": "columns", "children": cards}


# Timeline item kind -> (lucide icon, host Tone). An unlisted kind is skipped by
# the normalizer, so this only needs the kinds it emits.
_TIMELINE_VISUAL: dict[str, tuple[str, str]] = {
    "commit": ("arrow-up", "neutral"),
    "comment": ("message-square", "info"),
    "review": ("badge-check", "info"),
    "push": ("triangle-alert", "warn"),
    "merge": (_ICON_MERGED, "info"),
}
# Rows in the Activity card. The query already caps the window; this bounds what
# a drifted response can push into the pane.
_MAX_TIMELINE_ROWS = 10


def _activity_section(pull: dict[str, Any]) -> dict[str, Any] | None:
    """The Activity card: recent events, newest first, each with its local
    wall-clock time. Absent without a token, since the timeline is token-gated."""
    items = [t for t in (pull.get("timeline") or []) if isinstance(t, dict) and t.get("text")]
    if not items:
        return None
    rows: list[dict[str, Any]] = []
    for item in items[:_MAX_TIMELINE_ROWS]:
        icon, tone = _TIMELINE_VISUAL.get(item.get("kind") or "", ("dot", "neutral"))
        row: dict[str, Any] = {"kind": "row", "label": str(item["text"]), "icon": icon, "tone": tone}
        at = _format_refreshed_at(item.get("at"))
        if at:
            row["value"] = at
            # The timestamp is a scalar, not a status: keep it dim rather than
            # inheriting the event's tone.
            row["value_tone"] = "neutral"
        rows.append(row)
    return {"kind": "section", "title": "Activity", "boxed": True, "children": rows}


def _failing_names(runs: list[dict[str, Any]]) -> str:
    """ "X and Y" / "X and 3 others" over check names, for the verdict detail."""
    names = [str(r.get("name") or "check") for r in runs]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} and {len(names) - 1} others"


def _verdict(title: str, detail: str, visual: tuple[str, str], short: str, *, ready: bool = False) -> dict[str, Any]:
    """One rung of the merge ladder. ``visual`` is the ``(icon, tone)`` pair, which
    the ``_CHECK_VISUAL`` / ``_REVIEW_VISUAL`` tables already hand out."""
    icon, tone = visual
    return {"title": title, "detail": detail, "icon": icon, "tone": tone, "short": short, "ready": ready}


def _merge_verdict(pull: dict[str, Any]) -> dict[str, Any]:
    """Whether the selected PR can merge, and why not when it cannot.

    Derived from what the plugin already fetches (conflicts, the review decision,
    the check rollup, unresolved threads) rather than GitHub's ``mergeStateStatus``,
    which conflates all of those causes into one enum and so cannot say which one
    is blocking (the reason #80 left it unselected). ``short`` is the footer's
    one-word version.

    The rungs are ordered by who can clear the block: a state only the author can
    change (draft, conflicts) outranks one a reviewer owns, which outranks one CI
    will resolve on its own. First match wins, so the pane always names the thing
    to do next rather than the worst thing that is true.
    """
    buckets = _check_buckets(pull.get("checks")) or {k: [] for k in ("failing", "running", "queued", "passing")}
    base = pull.get("base") or "the base branch"
    failing, in_flight = buckets["failing"], buckets["running"] + buckets["queued"]
    passing, unresolved = len(buckets["passing"]), _unresolved_count(pull)

    def plural(count: int) -> str:
        return "check" if count == 1 else "checks"

    ladder: tuple[tuple[bool, dict[str, Any]], ...] = (
        (
            _is_merged(pull),
            _verdict("Merged", f"This PR is merged into {base}.", (_ICON_MERGED, "neutral"), "merged"),
        ),
        (
            bool(pull.get("draft")),
            _verdict("Draft", "Mark the PR ready for review to merge it.", (_ICON_DRAFT, "warn"), "draft"),
        ),
        (
            pull.get("merge_state") == "conflicts",
            _verdict(
                "Conflicts with base",
                f"This branch conflicts with {base}.",
                ("triangle-alert", "danger"),
                "conflicts",
            ),
        ),
        (
            pull.get("review_state") == "changes-requested",
            _verdict(
                "Changes requested",
                "A reviewer asked for changes before this can merge.",
                _REVIEW_VISUAL["changes-requested"][:2],
                "changes requested",
            ),
        ),
        (
            bool(failing),
            _verdict(
                f"{len(failing)} {plural(len(failing))} failing",
                f"Merging is blocked until {_failing_names(failing)} pass.",
                _CHECK_VISUAL["failing"][:2],
                "blocked",
            ),
        ),
        (
            bool(in_flight),
            _verdict(
                f"{len(in_flight)} {plural(len(in_flight))} running",
                f"Waiting on {_failing_names(in_flight)}.",
                _CHECK_VISUAL["running"][:2],
                "in progress",
            ),
        ),
        (
            bool(unresolved),
            _verdict(
                f"{unresolved} unresolved {'comment' if unresolved == 1 else 'comments'}",
                "Resolve the open review threads before merging.",
                ("message-square", "warn"),
                "unresolved",
            ),
        ),
        (
            pull.get("review_state") == "approved",
            _verdict(
                "Ready to merge",
                f"{passing} checks passing, approved, no conflicts with {base}."
                if passing
                else f"Approved, no conflicts with {base}.",
                _CHECK_VISUAL["succeeded"][:2],
                "ready",
                ready=True,
            ),
        ),
    )
    for matched, verdict in ladder:
        if matched:
            return verdict
    return _verdict(
        "Awaiting review",
        f"No approvals yet. No conflicts with {base}.",
        _REVIEW_VISUAL["waiting"][:2],
        "awaiting review",
    )


def _merge_callout(pull: dict[str, Any]) -> dict[str, Any]:
    """The pane's headline verdict card. Its button is deliberately not a merge:
    this plugin is read-only, so a mergeable PR gets a link out to GitHub and a
    blocked one gets an inert button naming the block."""
    verdict = _merge_verdict(pull)
    if verdict["ready"] and pull.get("url"):
        action = {
            "kind": "action",
            "label": "Merge on GitHub",
            "variant": "primary",
            "icon": _ICON_MERGED,
            "href": pull["url"],
            "tooltip": "Opens the PR on GitHub; this plugin never merges for you.",
        }
    else:
        action = {"kind": "action", "label": verdict["title"], "disabled": True}
    return {
        "kind": "callout",
        "title": verdict["title"],
        "detail": verdict["detail"],
        "icon": verdict["icon"],
        "tone": verdict["tone"],
        "actions": [action],
    }


def _pull_detail_blocks(pull: dict[str, Any]) -> list[dict[str, Any]]:
    """The selected PR's detail stack: verdict, reviewers, checks, diff/linked, and
    recent activity. A merged PR keeps only its verdict and activity: its review
    decision and CI rollup are history, not something to act on."""
    blocks: list[dict[str, Any]] = [_merge_callout(pull)]
    if not _is_merged(pull):
        candidates = (
            _review_section(pull),
            _checks_section(pull.get("checks")),
            _comments_section(pull.get("comments")),
            _context_columns(pull),
        )
        blocks.extend(b for b in candidates if b is not None)
    activity = _activity_section(pull)
    if activity is not None:
        blocks.append(activity)
    return blocks


def _repo_status_rows(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows for the repos that contribute no PR to the selector: a failed lookup, a
    non-GitHub checkout, or a clean branch. Grouped and folded when they are only
    context alongside repos that do have PRs."""
    rows: list[dict[str, Any]] = []
    for repo in repos:
        name = repo.get("name") or repo.get("repo") or "repo"
        if repo.get("error"):
            rows.append(
                {
                    "kind": "row",
                    "label": name,
                    "value": str(repo["error"].get("hint", "error")).splitlines()[0],
                    "icon": _ICON_ERROR,
                    "tone": "danger",
                }
            )
        elif not repo.get("repo"):
            rows.append({"kind": "row", "label": name, "value": "not a GitHub remote"})
        elif not (repo.get("pulls") or []):
            row: dict[str, Any] = {"kind": "row", "label": name, "value": "no open PR"}
            if repo.get("branch"):
                row["sublabel"] = str(repo["branch"])
            rows.append(row)
    return rows


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

# Budget-aware text for the always-on rate-limit note (#62). GitHub meters REST
# and GraphQL separately, so which budget is limited changes what still updates:
# a GraphQL-only limit keeps the REST-driven open-PR list fresh (only the rich
# CI/review fields go stale), while a REST limit freezes the list itself. The
# text is static (no live countdown) so the background pane push stays identical
# every tick until the budget refills.
_RATE_LIMIT_NOTE_TEXT = {
    "graphql": "GitHub GraphQL rate limit hit: CI and review details may be stale. The open-PR list still updates.",
    "rest": "GitHub REST rate limit hit: the pull request list may be stale until the budget resets.",
    "mixed": "GitHub API rate limit hit: showing cached pull request data until the budget resets.",
}


def _rate_limit_note(rate_limit: Any) -> dict[str, Any] | None:
    """A persistent warn note describing an active rate-limit backoff, or ``None``
    when none is active. ``rate_limit`` is the snapshot's ``{"budget", ...}`` from
    ``refresh._rate_limit_status``; an unknown budget degrades to the generic
    (``mixed``) wording rather than dropping the note."""
    if not isinstance(rate_limit, dict):
        return None
    budget = rate_limit.get("budget")
    key = budget if isinstance(budget, str) else "mixed"
    text = _RATE_LIMIT_NOTE_TEXT.get(key, _RATE_LIMIT_NOTE_TEXT["mixed"])
    return {"kind": "note", "tone": "warn", "text": text}


def _format_refreshed_at(value: Any) -> str | None:
    """ISO timestamp to compact pane text in the worker's local time. The worker
    runs on the user's machine, so local time is their computer's clock; rendering
    UTC here would mismatch every other local time the plugin shows (e.g. the
    rate-limit reset in ``main.py``)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A tz-naive stamp is UTC by convention (``_utc_now_iso`` always emits one).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%H:%M")


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


def _resolve_selection(
    pulls: list[tuple[dict[str, Any], dict[str, Any]]], selected_key: Any
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The ``(repo, pull)`` the pane details. The user's remembered choice when it
    is still in the snapshot, else the most-actionable PR: a selection must never
    survive the PR it named being merged, closed or renamed out of the list."""
    if not pulls:
        return None
    if isinstance(selected_key, str):
        for repo, pull in pulls:
            if _pull_key(repo, pull) == selected_key:
                return repo, pull
    return pulls[0]


def _pane_blocks(
    repos: list[dict[str, Any]],
    *,
    auth_present: bool,
    rate_limit: Any = None,
    selected_key: Any = None,
) -> list[dict[str, Any]]:
    """The pane's block list: a PR selector across the workspace, then the selected
    PR's detail, then whatever repos contribute no PR. No leading heading block: the
    dock tab (and the TUI overlay's own heading) already names the pane, so one here
    only repeated it."""
    head: list[dict[str, Any]] = []
    has_github_repo = any(repo.get("repo") for repo in repos)
    # Surface an active rate-limit at the top, on background refreshes too (#62):
    # the pane, not just a forced-refresh toast, explains why the data is stale.
    # Only when there is a GitHub repo whose data the limit affects.
    if has_github_repo:
        note = _rate_limit_note(rate_limit)
        if note is not None:
            head.append(note)
    # Only nag when there is actually a GitHub repo whose detail the token gates.
    if not auth_present and has_github_repo:
        head.append(dict(_TOKEN_NOTE))
    tail = [{"kind": "divider"}, dict(_REFRESH_ACTION)]

    if not repos:
        return _fit_to_budget(head, [{"kind": "note", "text": "no repos in this workspace", "tone": "neutral"}], tail)

    pulls = _session_pulls(repos)
    middle: list[dict[str, Any]] = []
    selection = _resolve_selection(pulls, selected_key)
    if selection is not None:
        chosen_key = _pull_key(*selection)
        # The selector renders even for a single PR: it is the pane's title for the
        # detail below it, and it carries the signal strip and the link out.
        middle.append(
            {
                "kind": "section",
                "children": [_pr_row(repo, pull, selected=_pull_key(repo, pull) == chosen_key) for repo, pull in pulls],
            }
        )
        middle.extend(_pull_detail_blocks(selection[1]))

    status_rows = _repo_status_rows(repos)
    if status_rows:
        # Folded away when there is real work above them; shown outright when they
        # are all there is to say about the workspace.
        if selection is not None and len(repos) > 1:
            middle.append(
                {
                    "kind": "section",
                    "title": f"Other repos ({len(status_rows)})",
                    "children": status_rows,
                    "collapsible": True,
                    "collapsed": True,
                    "tone": "neutral",
                }
            )
        else:
            middle.extend(status_rows)
    return _fit_to_budget(head, middle, tail)


def _pane_footer(freshness: Any, selection: tuple[dict[str, Any], dict[str, Any]] | None) -> dict[str, Any] | None:
    """The pane's pinned status line: when the data was last refreshed, and the
    selected PR's verdict in a word. ``None`` when neither half is known, so the
    host renders no empty bar."""
    footer: dict[str, Any] = {}
    if isinstance(freshness, dict):
        at = _format_refreshed_at(freshness.get("refreshed_at"))
        if at is not None:
            stale = freshness.get("stale") is True
            footer["text"] = f"last good refresh {at}" if stale else f"refreshed {at}"
            footer["icon"] = "clock" if stale else "refresh-cw"
    if selection is not None:
        verdict = _merge_verdict(selection[1])
        footer["value"] = verdict["short"]
        footer["tone"] = verdict["tone"]
    return footer or None


def snapshot_ui_state_params(
    snapshot: dict[str, Any],
    *,
    chips_on: frozenset[str] = _ALL_CHIPS,
    show_column: bool = True,
    selected: dict[str, str] | None = None,
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
    rate_limit = snapshot.get("rate_limit")
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
        selected_key = (selected or {}).get(sid)
        pane_payload: dict[str, Any] = {
            "title": "GitHub",
            "default_location": PANE_DEFAULT_LOCATION,
            # No per-pane icon: the manifest's icon_asset (the real GitHub logo)
            # wins unconditionally in the activity bar and dock tab, falling back
            # to the manifest icon (git-branch) below that. A per-pane icon here
            # would only ever shadow both.
            "blocks": _pane_blocks(
                repos,
                auth_present=auth_present,
                rate_limit=rate_limit,
                selected_key=selected_key,
            ),
        }
        footer = _pane_footer(session.get("freshness"), _resolve_selection(_session_pulls(repos), selected_key))
        if footer is not None:
            pane_payload["footer"] = footer
        params.append(
            {
                "slot": PANE_SLOT[0],
                "id": PANE_SLOT[1],
                "session_id": sid,
                "payload": pane_payload,
            }
        )
    return params

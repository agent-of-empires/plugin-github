"""GitHub GraphQL query + pure normalizers for the rich (token-gated) PR view.

The single query fetches, for a branch's pull requests, everything the pane
renders: state (incl. MERGED), ``reviewDecision``, the head commit's check
rollup + per-check contexts, and review threads with their resolved flag and
first comment. Everything here is a PURE function over the parsed GraphQL
envelope, so it is unit-tested without a network and a drifting/partial API
shape degrades a field rather than raising (the worker stays fail-soft).

Why infer ``review_state`` instead of mapping ``reviewDecision`` directly:
GitHub's ``reviewDecision`` only yields ``APPROVED`` / ``CHANGES_REQUESTED`` /
``REVIEW_REQUIRED`` / null. It cannot express "commented", so that state is
inferred from a COMMENTED review or any unresolved thread.
"""

from __future__ import annotations

from typing import Any

# The per-PR node selection, shared by every aliased ``pullRequests`` field in a
# batched query (#25). Kept as a fragment so only the top-level aliases and their
# ``headRefName`` args are dynamic. ``pullRequests(headRefName:)`` matches by the
# head ref NAME, so a merged PR still resolves after its remote branch is deleted
# (unlike ref(qualifiedName:), which would be null). Connections are capped so a
# pathological PR cannot blow past the host's payload cap.
#
# Counts are sized to what the pane renders, to keep the GraphQL point cost low
# now that this query fires only on a detected change (#21):
# - pullRequests first:3 (a branch rarely has more than one open + one merged PR);
# - reviews last:1, states:[COMMENTED] (review_state only needs to know whether a
#   COMMENTED review exists; APPROVED/CHANGES_REQUESTED come from reviewDecision);
# - reviewThreads first:100 with pageInfo, so a big review (e.g. a CodeRabbit
#   pass) surfaces every unresolved comment; refresh paginates by the node ``id``
#   for the rare PR that still has more (#28).
# contexts stays at first:50 ON PURPOSE: the per-check rows are ranked
# failure-first CLIENT-side (see check_summary), so truncating the connection
# could drop a failing check past the cap and render a PR falsely green.
_PR_CONNECTION_FRAGMENT = """
fragment PRConnection on PullRequestConnection {
  nodes {
    id number title url state isDraft merged reviewDecision
    baseRef { branchProtectionRule {
      requiresStatusChecks
      requiredStatusCheckContexts
      requiredStatusChecks { context app { databaseId slug name } }
    } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 50) { nodes {
      __typename
      ... on CheckRun {
        name status conclusion detailsUrl startedAt completedAt
        checkSuite { app { databaseId slug name } }
      }
      ... on StatusContext { context state targetUrl createdAt }
    } } } } } }
    reviews(last: 1, states: [COMMENTED]) { nodes { state } }
    reviewThreads(first: 100) {
      pageInfo { hasNextPage endCursor }
      nodes {
        isResolved path line
        comments(first: 1) { nodes { author { login } bodyText url } }
      }
    }
  }
}
"""

# One aliased ``pullRequests`` field. ``{i}`` is the index; the doubled braces are
# GraphQL literals that survive ``str.format``. rateLimit.cost (one per batched
# query) lets the per-query budget be verified (#23).
_ALIAS_FIELD = """    b{i}: pullRequests(
      headRefName: $b{i}, states: [OPEN, MERGED], first: 3,
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{ ...PRConnection }}"""


def build_query(alias_count: int) -> str:
    """A single GraphQL document fetching ``alias_count`` branches of one repo,
    aliased ``b0:``/``b1:``/... over the static PR fragment (#25). Branch names
    travel as ``$bN`` variables, never interpolated, so the aliases are always
    valid identifiers and there is no injection or quoting risk. A count of 1 is
    just one alias, so this is the only rich-query path."""
    n = max(alias_count, 1)
    params = ", ".join(["$owner: String!", "$repo: String!", *[f"$b{i}: String!" for i in range(n)]])
    fields = "\n".join(_ALIAS_FIELD.format(i=i) for i in range(n))
    return f"""
query({params}) {{
  rateLimit {{ cost remaining resetAt }}
  repository(owner: $owner, name: $repo) {{
{fields}
  }}
}}
{_PR_CONNECTION_FRAGMENT}"""


# Follow-up query for a PR whose reviewThreads exceeded one page (#28): fetch the
# next page by the PR's node id. The caller bounds the page count so cost stays
# small (the common PR needs zero follow-ups).
THREADS_PAGE_QUERY = """
query($id: ID!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  node(id: $id) {
    ... on PullRequest {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved path line
          comments(first: 1) { nodes { author { login } bodyText url } }
        }
      }
    }
  }
}
"""

# GitHub purple, for the MERGED state (no semantic tone names this hue).
MERGED_COLOR = "#8957e5"
EXCERPT_LEN = 200


def _nodes(obj: Any, *path: str) -> list[dict[str, Any]]:
    """Walk ``obj[path...]`` to a ``{"nodes": [...]}`` connection and return the
    dict nodes, or ``[]`` for any missing/malformed step. Total, never raises."""
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(key)
    if not isinstance(cur, dict):
        return []
    nodes = cur.get("nodes")
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def excerpt(text: Any, limit: int = EXCERPT_LEN) -> str:
    """A one-paragraph, length-capped excerpt of a comment body."""
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def review_state(pr: dict[str, Any]) -> str:
    """``approved`` / ``changes-requested`` / ``commented`` / ``waiting``.

    ``commented`` is inferred (see module docstring): a COMMENTED review or any
    unresolved thread, when the decision is neither approved nor changes-needed.
    """
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        return "changes-requested"
    reviews = _nodes(pr, "reviews")
    threads = _nodes(pr, "reviewThreads")
    if any(r.get("state") == "COMMENTED" for r in reviews) or any(not t.get("isResolved") for t in threads):
        return "commented"
    return "waiting"


# Per-check conclusion -> our state vocabulary. Anything unlisted reads "unknown".
_CONCLUSION = {
    "SUCCESS": "succeeded",
    "NEUTRAL": "succeeded",
    "SKIPPED": "succeeded",
    "FAILURE": "failing",
    "TIMED_OUT": "failing",
    "CANCELLED": "failing",
    "ACTION_REQUIRED": "failing",
    "STARTUP_FAILURE": "failing",
    "STALE": "failing",
}
# StatusContext.state / statusCheckRollup.state -> our state vocabulary.
_STATUS_STATE = {
    "SUCCESS": "succeeded",
    "FAILURE": "failing",
    "ERROR": "failing",
    "PENDING": "running",
    "EXPECTED": "running",
}
# Display order for the per-check rows: failing first (most actionable), then
# in-flight, then done; anything unmapped sinks to the bottom.
_STATE_RANK = {"failing": 0, "running": 1, "queued": 2, "succeeded": 3, "unknown": 4}
Requirement = tuple[str, str | None]


def _context_state(ctx: dict[str, Any]) -> str:
    """One check context (CheckRun or StatusContext) -> state vocabulary."""
    if ctx.get("__typename") == "CheckRun":
        status = ctx.get("status")
        if status == "IN_PROGRESS":
            return "running"
        if status in ("QUEUED", "WAITING", "REQUESTED", "PENDING"):
            return "queued"
        return _CONCLUSION.get(ctx.get("conclusion") or "", "unknown")
    return _STATUS_STATE.get(ctx.get("state") or "", "unknown")


def _app_key(app: Any) -> str | None:
    if not isinstance(app, dict):
        return None
    for key in ("databaseId", "slug", "name"):
        value = app.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, int):
            return str(value)
    return None


def _branch_protection_rule(pr: dict[str, Any]) -> dict[str, Any] | None:
    base_ref = pr.get("baseRef")
    if not isinstance(base_ref, dict):
        return None
    rule = base_ref.get("branchProtectionRule")
    return rule if isinstance(rule, dict) else None


def _optional_list(rule: dict[str, Any], key: str) -> list[Any] | None:
    if key not in rule or rule[key] is None:
        return []
    value = rule[key]
    return value if isinstance(value, list) else None


def _parse_required_checks(contexts_raw: list[Any], checks_raw: list[Any]) -> list[Requirement] | None:
    required: set[Requirement] = set()
    for context in contexts_raw:
        if not isinstance(context, str):
            return None
        required.add((context, None))
    for item in checks_raw:
        if not isinstance(item, dict):
            return None
        context = item.get("context")
        if not isinstance(context, str):
            return None
        required.add((context, _app_key(item.get("app"))))
    return sorted(required)


def _required_checks(pr: dict[str, Any]) -> list[Requirement] | None:
    rule = _branch_protection_rule(pr)
    if rule is None:
        return None
    requires = rule.get("requiresStatusChecks")
    if not isinstance(requires, bool):
        return None
    if not requires:
        return []
    has_contexts = "requiredStatusCheckContexts" in rule
    has_checks = "requiredStatusChecks" in rule
    if not has_contexts and not has_checks:
        return None
    contexts_raw = _optional_list(rule, "requiredStatusCheckContexts")
    checks_raw = _optional_list(rule, "requiredStatusChecks")
    if contexts_raw is None or checks_raw is None:
        return None
    return _parse_required_checks(contexts_raw, checks_raw)


def _matches_requirement(run: dict[str, Any], requirement: Requirement) -> bool:
    context, app = requirement
    if run.get("name") != context:
        return False
    return app is None or run.get("app") == app


def _required_rollup_state(runs: list[dict[str, Any]], required: list[Requirement]) -> str:
    if not required:
        return "unknown"
    states: list[str] = []
    for requirement in required:
        matches = [run for run in runs if _matches_requirement(run, requirement)]
        states.extend(run.get("state", "unknown") for run in matches)
        if not matches:
            states.append("running")
    if "failing" in states:
        return "failing"
    if "running" in states:
        return "running"
    if "queued" in states:
        return "queued"
    if "unknown" in states:
        return "unknown"
    return "succeeded"


def _mark_required_runs(runs: list[dict[str, Any]], required: list[Requirement]) -> None:
    for run in runs:
        run["required"] = any(_matches_requirement(run, requirement) for requirement in required)


def check_summary(pr: dict[str, Any], *, required_checks_only: bool = False) -> dict[str, Any] | None:
    """``{"state", "runs": [{name, state, url}]}`` for the PR head commit, or
    ``None`` when the head commit has no checks configured."""
    commits = _nodes(pr, "commits")
    rollup = commits[0].get("commit", {}).get("statusCheckRollup") if commits else None
    if not isinstance(rollup, dict):
        return None
    # GitHub returns one node per check run, so reusable/multi-caller workflows
    # yield several runs sharing a display name (#37). Collapse same-named runs
    # to a single row, keeping the latest by timestamp. ISO 8601 timestamps sort
    # lexicographically, so plain string ``>`` is correct; missing timestamps are
    # the empty string. On a tie (equal or both missing) keep the worse state, so
    # a flake cannot hide a real failure.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    ts: dict[tuple[str, str], str] = {}
    for ctx in _nodes(rollup, "contexts"):
        run: dict[str, Any]
        if ctx.get("__typename") == "CheckRun":
            name = ctx.get("name") or "check"
            when = ctx.get("completedAt") or ctx.get("startedAt") or ""
            suite = ctx.get("checkSuite")
            app = _app_key(suite.get("app") if isinstance(suite, dict) else None)
            run = {"name": name, "state": _context_state(ctx), "url": ctx.get("detailsUrl")}
            if app is not None:
                run["app"] = app
        else:
            name = ctx.get("context") or "check"
            when = ctx.get("createdAt") or ""
            run = {"name": name, "state": _context_state(ctx), "url": ctx.get("targetUrl")}
        key = (name, run.get("app") or "")
        if key not in latest:
            latest[key] = run
            ts[key] = when
            continue
        prev_when = ts[key]
        if when > prev_when:
            latest[key] = run
            ts[key] = when
        elif when == prev_when and _STATE_RANK.get(run["state"], 5) < _STATE_RANK.get(latest[key]["state"], 5):
            latest[key] = run
    runs = list(latest.values())
    # Stable sort by state rank keeps GitHub's order within each group while
    # surfacing failures at the top.
    runs.sort(key=lambda r: _STATE_RANK.get(r["state"], 5))
    state = _STATUS_STATE.get(rollup.get("state") or "", "unknown")
    if required_checks_only:
        required = _required_checks(pr)
        if required is not None:
            _mark_required_runs(runs, required)
            state = _required_rollup_state(runs, required)
    return {"state": state, "runs": runs}


def comment_summary(pr: dict[str, Any]) -> dict[str, Any]:
    """Unresolved review threads: a count plus each thread's first comment."""
    items: list[dict[str, Any]] = []
    for thread in _nodes(pr, "reviewThreads"):
        if thread.get("isResolved"):
            continue
        first = _nodes(thread, "comments")
        comment = first[0] if first else {}
        items.append(
            {
                "author": (comment.get("author") or {}).get("login") or "",
                "body": excerpt(comment.get("bodyText")),
                "path": thread.get("path"),
                "line": thread.get("line"),
                "url": comment.get("url"),
                "resolved": False,
            }
        )
    return {"unresolved": len(items), "items": items}


def _normalize_pull(pr: dict[str, Any], *, required_checks_only: bool = False) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "title": pr.get("title") or "",
        "state": pr.get("state"),
        "draft": bool(pr.get("isDraft", False)),
        "merged": bool(pr.get("merged", False)),
        "review_state": review_state(pr),
        "checks": check_summary(pr, required_checks_only=required_checks_only),
        "comments": comment_summary(pr),
    }


def normalize_connection(conn: Any, *, required_checks_only: bool = False) -> list[dict[str, Any]]:
    """A ``pullRequests`` connection (the value of one alias in a batched
    response, or any ``{"nodes": [...]}``) -> trimmed, UI-ready pull dicts.

    Shape stays a superset of the REST ``_trim`` output (``number``/``url``/
    ``title``/``state``/``draft``) so ``uistate`` renders either source, plus
    the rich fields ``merged``/``review_state``/``checks``/``comments``. Total:
    a missing/malformed connection degrades to ``[]`` rather than raising.
    """
    if not isinstance(conn, dict):
        return []
    nodes = conn.get("nodes")
    pulls = [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []
    return [_normalize_pull(pr, required_checks_only=required_checks_only) for pr in pulls]

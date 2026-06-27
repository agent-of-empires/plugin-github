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
    commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 50) { nodes {
      __typename
      ... on CheckRun { name status conclusion detailsUrl }
      ... on StatusContext { context state targetUrl }
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


def check_summary(pr: dict[str, Any]) -> dict[str, Any] | None:
    """``{"state", "runs": [{name, state, url}]}`` for the PR head commit, or
    ``None`` when the head commit has no checks configured."""
    commits = _nodes(pr, "commits")
    rollup = commits[0].get("commit", {}).get("statusCheckRollup") if commits else None
    if not isinstance(rollup, dict):
        return None
    runs: list[dict[str, Any]] = []
    for ctx in _nodes(rollup, "contexts"):
        if ctx.get("__typename") == "CheckRun":
            runs.append(
                {"name": ctx.get("name") or "check", "state": _context_state(ctx), "url": ctx.get("detailsUrl")}
            )
        else:
            runs.append(
                {"name": ctx.get("context") or "check", "state": _context_state(ctx), "url": ctx.get("targetUrl")}
            )
    # Stable sort by state rank keeps GitHub's order within each group while
    # surfacing failures at the top.
    runs.sort(key=lambda r: _STATE_RANK.get(r["state"], 5))
    return {"state": _STATUS_STATE.get(rollup.get("state") or "", "unknown"), "runs": runs}


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


def _normalize_pull(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "title": pr.get("title") or "",
        "state": pr.get("state"),
        "draft": bool(pr.get("isDraft", False)),
        "merged": bool(pr.get("merged", False)),
        "review_state": review_state(pr),
        "checks": check_summary(pr),
        "comments": comment_summary(pr),
    }


def normalize_connection(conn: Any) -> list[dict[str, Any]]:
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
    return [_normalize_pull(pr) for pr in pulls]

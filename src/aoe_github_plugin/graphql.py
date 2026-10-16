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

import json
from typing import Any
from datetime import datetime
from datetime import timezone

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
# - reviews last:20, states:[COMMENTED] (review_state only needs to know who left
#   a COMMENTED review, so bot-only comments can be ignored; APPROVED /
#   CHANGES_REQUESTED come from reviewDecision);
# - reviewThreads first:100 with pageInfo, so a big review (e.g. a CodeRabbit
#   pass) surfaces every unresolved comment; refresh paginates by the node ``id``
#   for the rare PR that still has more (#28).
# contexts is first:100 (GitHub's connection max, same point cost as first:50) ON
# PURPOSE: the per-check rows are ranked failure-first CLIENT-side (see
# check_summary), so truncating the connection could drop a failing check past the
# cap and render a PR falsely green. A PR with 50+ checks is routine, and
# check_summary keeps the raw rollup whenever the page is still truncated.
_PR_CONNECTION_FRAGMENT = """
fragment PRConnection on PullRequestConnection {
  nodes {
    id number title url state isDraft merged mergeable reviewDecision updatedAt headRefOid
    baseRefName additions deletions changedFiles
    author { login }
    baseRef { branchProtectionRule {
      requiresStatusChecks
      requiredStatusCheckContexts
      requiredStatusChecks { context app { databaseId slug name } }
    } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state contexts(first: 100) { totalCount nodes {
      __typename
      ... on CheckRun {
        name status conclusion detailsUrl startedAt completedAt
        checkSuite {
          app { databaseId slug name }
          # The workflow name is the group a check belongs to ("Lint", "CodeQL"),
          # which is what a reader scans by; `app.name` is "GitHub Actions" for
          # every Actions check and so groups nothing. Plain object fields, not
          # connections, so they add no GraphQL points.
          workflowRun { workflow { name } }
        }
      }
      ... on StatusContext { context state targetUrl createdAt }
    } } } } } }
    reviews(last: 20, states: [COMMENTED]) { nodes { state author { login } } }
    latestReviews(first: 20) { nodes { state author { login } commit { oid } } }
    decidedReviews: reviews(last: 30, states: [CHANGES_REQUESTED, APPROVED, DISMISSED]) {
      nodes { state author { login } commit { oid } }
    }
    reviewRequests(first: 20) { nodes { requestedReviewer {
      __typename
      ... on User { login }
      ... on Team { name }
    } } }
    closingIssuesReferences(first: 5) { nodes { number title url state } }
    timelineItems(last: 10, itemTypes: [
      PULL_REQUEST_COMMIT, PULL_REQUEST_REVIEW, ISSUE_COMMENT,
      HEAD_REF_FORCE_PUSHED_EVENT, REVIEW_REQUESTED_EVENT, MERGED_EVENT
    ]) { nodes {
      __typename
      ... on PullRequestCommit { commit {
        abbreviatedOid committedDate messageHeadline author { user { login } name }
      } }
      ... on PullRequestReview { state createdAt author { login } }
      ... on IssueComment { createdAt author { login } }
      ... on HeadRefForcePushedEvent { createdAt actor { login } }
      ... on ReviewRequestedEvent { createdAt actor { login } }
      ... on MergedEvent { createdAt actor { login } }
    } }
    reviewThreads(first: 100) {
      totalCount
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


# The cheap change-detection tier (#69). Scalars plus the top-level check rollup
# and the review-thread count only: no contexts, no thread nodes, no comments, so
# a batched digest costs ~1 point where the rich query costs ~25-30. The fields
# mirror ``digest_signature`` exactly; anything the signature reads must be
# selected both here and in ``_PR_CONNECTION_FRAGMENT`` so a signature computed
# from a full response compares equal to one computed from a digest response.
_DIGEST_ALIAS_FIELD = """    b{i}: pullRequests(
      headRefName: $b{i}, states: [OPEN, MERGED], first: 3,
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{ nodes {{
      number state isDraft merged mergeable reviewDecision updatedAt headRefOid
      commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
      reviewThreads(first: 1) {{ totalCount }}
    }} }}"""


def build_digest_query(alias_count: int) -> str:
    """The digest counterpart of ``build_query``: same aliasing and variable
    scheme, minimal per-PR selection (#69)."""
    n = max(alias_count, 1)
    params = ", ".join(["$owner: String!", "$repo: String!", *[f"$b{i}: String!" for i in range(n)]])
    fields = "\n".join(_DIGEST_ALIAS_FIELD.format(i=i) for i in range(n))
    return f"""
query({params}) {{
  rateLimit {{ cost remaining resetAt }}
  repository(owner: $owner, name: $repo) {{
{fields}
  }}
}}"""


def digest_signature(conn: Any) -> str | None:
    """Canonical signature of the cheap change-detection fields for one
    ``pullRequests`` connection, valid for both the digest and the full (rich)
    response shapes. Equal signatures mean the cached rich result is still
    current for everything the digest can see; the caller then skips the full
    query. ``None`` for a malformed connection, which callers treat as
    "cannot validate" (next background pass falls back to the full query).

    Deliberately a heuristic, not a hash of everything the pane renders: a
    comment body edit or a per-check change under an unchanged rollup state can
    slip past it, which is why ``GRAPHQL_FULL_MAX_STALE`` still bounds how long
    a full refresh can be deferred."""
    if not isinstance(conn, dict):
        return None
    nodes = conn.get("nodes")
    if not isinstance(nodes, list):
        return None
    sig: list[dict[str, Any]] = []
    for pr in nodes:
        if not isinstance(pr, dict):
            return None
        commits = _nodes(pr, "commits")
        rollup = commits[0].get("commit", {}).get("statusCheckRollup") if commits else None
        threads = pr.get("reviewThreads")
        sig.append(
            {
                "number": pr.get("number"),
                "state": pr.get("state"),
                "draft": bool(pr.get("isDraft", False)),
                "merged": bool(pr.get("merged", False)),
                "decision": pr.get("reviewDecision"),
                "updated": pr.get("updatedAt"),
                "head": pr.get("headRefOid"),
                # Canonicalized to the actionable conflict bool, NOT the raw
                # ``mergeable`` enum: GitHub recomputes mergeability async, so a
                # push cycles MERGEABLE -> UNKNOWN -> MERGEABLE. Storing the raw
                # enum would diff twice per push and re-fire the ~25-point rich
                # query each time. The bool only flips when a conflict actually
                # appears or clears, and CONFLICTING still diffs the instant
                # GitHub reports it (the digest reads the same scalar), so this
                # loses no detection latency (#80).
                "conflicts": merge_state(pr) == "conflicts",
                "rollup": rollup.get("state") if isinstance(rollup, dict) else None,
                "threads": threads.get("totalCount") if isinstance(threads, dict) else None,
            }
        )
    return json.dumps(sig, sort_keys=True, separators=(",", ":"))


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


def _blocking_reviews(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Each reviewer's standing ``CHANGES_REQUESTED`` review.

    ``latestReviews`` drops a reviewer's review once they are re-requested, yet
    ``reviewDecision`` stays ``CHANGES_REQUESTED``; ``decidedReviews`` (oldest
    first) still carries it, so the latest of those per author is authoritative.
    ``latestReviews`` fills in for a response without ``decidedReviews``."""
    latest: dict[str, dict[str, Any]] = {}
    for node in [*_nodes(pr, "latestReviews"), *_nodes(pr, "decidedReviews")]:
        name = _reviewer_name(node.get("author"))
        if name is not None:
            latest[name] = node
    return [n for n in latest.values() if n.get("state") == "CHANGES_REQUESTED"]


def _review_is_stale(pr: dict[str, Any], review: dict[str, Any]) -> bool:
    """True when a review was made on a commit other than the PR's current head.
    Unknown commits (a drifted or partial response) are never stale."""
    commit = review.get("commit")
    oid = commit.get("oid") if isinstance(commit, dict) else None
    head = pr.get("headRefOid")
    return isinstance(oid, str) and isinstance(head, str) and oid != head


def review_state(pr: dict[str, Any]) -> str:
    """``approved`` / ``changes-requested`` / ``changes-pushed`` / ``commented`` / ``waiting``.

    ``changes-pushed`` is a ``CHANGES_REQUESTED`` decision whose blocking reviews
    were all made on an older commit: the author has pushed since, so the review
    still blocks the merge on GitHub but is likely addressed and awaits a re-review.

    ``commented`` is inferred (see module docstring): a COMMENTED review or any
    unresolved thread, when the decision is neither approved nor changes-needed.
    """
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        return "approved"
    if decision == "CHANGES_REQUESTED":
        blocking = _blocking_reviews(pr)
        if blocking and all(_review_is_stale(pr, r) for r in blocking):
            return "changes-pushed"
        return "changes-requested"
    reviews = _nodes(pr, "reviews")
    threads = _nodes(pr, "reviewThreads")
    if any(r.get("state") == "COMMENTED" for r in reviews) or any(not t.get("isResolved") for t in threads):
        return "commented"
    return "waiting"


def commenters(pr: dict[str, Any]) -> list[str]:
    """Logins behind an inferred ``commented`` state: COMMENTED-review authors plus
    each unresolved thread's opener. An unknown author is ``""`` (counts as a
    person, so a deleted account never hides a comment)."""
    logins = {_actor_login(r, "author") for r in _nodes(pr, "reviews") if r.get("state") == "COMMENTED"}
    for thread in _nodes(pr, "reviewThreads"):
        if not thread.get("isResolved"):
            first = _nodes(thread, "comments")
            logins.add(_actor_login(first[0], "author") if first else "")
    return sorted(logins)


def _login_key(login: str) -> str:
    """Case-insensitive login, ``[bot]`` suffix dropped: GraphQL says
    ``coderabbitai``, REST and the web UI say ``coderabbitai[bot]``."""
    return login.strip().lower().removesuffix("[bot]")


def parse_ignored_commenters(raw: Any) -> frozenset[str]:
    """The ``ignored_commenters`` setting (comma/whitespace separated logins) as
    a set of ``_login_key`` values. A non-string degrades to the empty set."""
    if not isinstance(raw, str):
        return frozenset()
    return frozenset(k for k in (_login_key(x) for x in raw.replace(",", " ").split()) if k)


def drop_ignored_commenters(pull: dict[str, Any], ignored: frozenset[str]) -> dict[str, Any]:
    """``pull`` with ``commented`` downgraded to ``waiting`` when every commenter
    is ignored (bots like CodeRabbit) or is the PR author (replying to a review
    thread posts a COMMENTED review), so the row reads "commented" only when
    someone else did. Returns a copy, never mutating the cached pull. A pull
    without ``commenters`` (basic REST shape, an older persisted snapshot) is
    untouched. Unresolved-comment counts are unaffected: bot threads still
    surface there."""
    names = pull.get("commenters")
    if pull.get("review_state") != "commented" or not isinstance(names, list):
        return pull
    author = pull.get("author")
    skip = ignored | {_login_key(author)} if isinstance(author, str) and author else ignored
    if any(not isinstance(n, str) or _login_key(n) not in skip for n in names):
        return pull
    return {**pull, "review_state": "waiting"}


def merge_state(pr: dict[str, Any]) -> str | None:
    """``conflicts`` when GitHub reports the PR head conflicts with its base, else
    ``None`` (no signal).

    Only ``mergeable == CONFLICTING`` is surfaced. ``MERGEABLE`` is healthy and
    ``UNKNOWN`` is the transient value GitHub returns while it recomputes
    mergeability after a push; both degrade to ``None`` so the UI never flashes a
    conflict state that GitHub has not actually confirmed. ``mergeStateStatus`` is
    deliberately not consulted: it conflates conflicts (DIRTY) with policy/CI/
    behind-base blockers, which are not this signal (#80)."""
    return "conflicts" if pr.get("mergeable") == "CONFLICTING" else None


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


def _parse_iso(value: Any) -> datetime | None:
    """An ISO 8601 GraphQL timestamp to an aware ``datetime``, or ``None`` for
    anything unparseable. Total, so a drifted field degrades a display detail."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _run_duration(started: Any, completed: Any) -> str | None:
    """A check run's wall time as ``"6m 40s"`` / ``"48s"``, or ``None`` when either
    endpoint is missing (a still-running check) or the pair is inverted."""
    start = _parse_iso(started)
    end = _parse_iso(completed)
    if start is None or end is None:
        return None
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return None
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


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


def _workflow_name(suite: Any) -> str | None:
    """The workflow a check run belongs to, used as its display group. ``None`` for
    a non-Actions check (a StatusContext, an external app) which has no workflow."""
    if not isinstance(suite, dict):
        return None
    run = suite.get("workflowRun")
    workflow = run.get("workflow") if isinstance(run, dict) else None
    name = workflow.get("name") if isinstance(workflow, dict) else None
    return name if isinstance(name, str) and name else None


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


def _worst_state(states: list[str]) -> str:
    for worst in ("failing", "running", "queued", "unknown"):
        if worst in states:
            return worst
    return "succeeded"


def _runs_state(runs: list[dict[str, Any]]) -> str:
    return _worst_state([run.get("state", "unknown") for run in runs])


def _required_rollup_state(runs: list[dict[str, Any]], required: list[Requirement]) -> str:
    if not required:
        return "unknown"
    states: list[str] = []
    for requirement in required:
        matches = [run for run in runs if _matches_requirement(run, requirement)]
        states.extend(run.get("state", "unknown") for run in matches)
        if not matches:
            states.append("running")
    return _worst_state(states)


def _mark_required_runs(runs: list[dict[str, Any]], required: list[Requirement]) -> None:
    for run in runs:
        run["required"] = any(_matches_requirement(run, requirement) for requirement in required)


def _context_run(ctx: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """One check context (a `CheckRun` or a `StatusContext`) as a display run plus
    the timestamp same-name runs are deduped by. A StatusContext is the sparse
    case: it has no suite, so no app, workflow group, duration or skipped flag."""
    if ctx.get("__typename") != "CheckRun":
        return (
            {
                "name": ctx.get("context") or "check",
                "state": _context_state(ctx),
                "url": ctx.get("targetUrl"),
            },
            ctx.get("createdAt") or "",
        )
    suite = ctx.get("checkSuite")
    run: dict[str, Any] = {
        "name": ctx.get("name") or "check",
        "state": _context_state(ctx),
        "url": ctx.get("detailsUrl"),
    }
    app = _app_key(suite.get("app") if isinstance(suite, dict) else None)
    if app is not None:
        run["app"] = app
    # A skipped run still reads as ``succeeded`` in the rollup (it does not block a
    # merge), but the pane lists it apart from real passes. Kept as a separate
    # display flag rather than a state so the rollup and attention math stay put.
    if ctx.get("conclusion") == "SKIPPED":
        run["skipped"] = True
    duration = _run_duration(ctx.get("startedAt"), ctx.get("completedAt"))
    if duration is not None:
        run["duration"] = duration
    group = _workflow_name(suite)
    if group is not None:
        run["group"] = group
    return run, ctx.get("completedAt") or ctx.get("startedAt") or ""


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
        run, when = _context_run(ctx)
        key = (run["name"], run.get("app") or "")
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
    # GitHub's rollup stays FAILURE after a failed run is re-run green (#98), so
    # when every context was fetched, derive the headline from the deduped runs
    # (what GitHub's own PR page shows). A truncated page may hide a failing run,
    # so it keeps the raw rollup: a false "failing" beats a false green.
    contexts = rollup.get("contexts")
    total = contexts.get("totalCount") if isinstance(contexts, dict) else None
    if runs and not (isinstance(total, int) and total > len(_nodes(rollup, "contexts"))):
        state = _runs_state(runs)
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


# Per-reviewer review state -> our vocabulary. PENDING is a review the author has
# started but not submitted, which is not a signal about this PR, so it reads the
# same as no review at all.
_REVIEW_NODE_STATE = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes-requested",
    "COMMENTED": "commented",
    "DISMISSED": "pending",
    "PENDING": "pending",
}


def _reviewer_name(reviewer: Any) -> str | None:
    """A requested reviewer's display handle: a user's login or a team's name."""
    if not isinstance(reviewer, dict):
        return None
    for key in ("login", "name"):
        value = reviewer.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def reviewers(pr: dict[str, Any]) -> list[dict[str, str]]:
    """One entry per person whose opinion this PR is waiting on or has:
    ``[{"name", "state"}]`` where state is ``approved`` / ``changes-requested`` /
    ``changes-pushed`` / ``commented`` / ``pending``.

    ``latestReviews`` already collapses a reviewer's history to their current
    position, so it needs no de-duplication of its own; a standing block that
    ``latestReviews`` dropped (re-requested reviewer) is restored; outstanding
    ``reviewRequests`` are appended as ``pending``. A reviewer who has both (a
    re-request after a review) keeps their submitted state, since that is the
    position still on record. Total: malformed nodes are skipped, never raised.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in _nodes(pr, "latestReviews"):
        name = _reviewer_name(node.get("author"))
        if name is None or name in seen:
            continue
        seen.add(name)
        state = _REVIEW_NODE_STATE.get(node.get("state") or "", "pending")
        if state == "changes-requested" and _review_is_stale(pr, node):
            state = "changes-pushed"
        out.append({"name": name, "state": state})
    # A re-requested reviewer is gone from `latestReviews` but their block stands.
    for node in _blocking_reviews(pr):
        name = _reviewer_name(node.get("author"))
        if name is None or name in seen:
            continue
        seen.add(name)
        stale = _review_is_stale(pr, node)
        out.append({"name": name, "state": "changes-pushed" if stale else "changes-requested"})
    for node in _nodes(pr, "reviewRequests"):
        name = _reviewer_name(node.get("requestedReviewer"))
        if name is None or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "state": "pending"})
    return out


def review_summary(people: list[dict[str, str]]) -> str:
    """The one-line headline over a reviewer list, matching how GitHub reads it:
    a blocking "changes requested" wins, then an approval count, then how many are
    still outstanding. Empty for no reviewers, so the caller can omit the line."""
    if not people:
        return ""
    if any(p["state"] == "changes-requested" for p in people):
        return "changes requested"
    if any(p["state"] == "changes-pushed" for p in people):
        return "pushed since review"
    approved = sum(1 for p in people if p["state"] == "approved")
    if approved == len(people):
        return f"{approved} approved" if approved != 1 else "1 approved"
    if approved:
        return f"{approved} of {len(people)} approved"
    return f"{len(people)} pending"


def diff_summary(pr: dict[str, Any]) -> dict[str, int] | None:
    """``{"added", "removed", "files"}`` for the PR, or ``None`` when GitHub did
    not report them (the no-token REST shape, or a drifted response)."""
    values: dict[str, int] = {}
    for key, field in (("added", "additions"), ("removed", "deletions"), ("files", "changedFiles")):
        value = pr.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values[key] = value
    return values


def linked_issues(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Issues this PR closes on merge, from GitHub's own resolution of the
    ``Closes #n`` links in the body. Entries without a number are skipped."""
    out: list[dict[str, Any]] = []
    for node in _nodes(pr, "closingIssuesReferences"):
        number = node.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        out.append(
            {
                "number": number,
                "title": node.get("title") or "",
                "url": node.get("url"),
                # OPEN / CLOSED, so a linked issue already closed reads differently.
                "state": (node.get("state") or "").lower() or None,
            }
        )
    return out


def _actor_login(node: Any, key: str) -> str:
    holder = node.get(key) if isinstance(node, dict) else None
    login = holder.get("login") if isinstance(holder, dict) else None
    return login if isinstance(login, str) else ""


def _commit_timeline_item(node: dict[str, Any]) -> dict[str, Any] | None:
    commit = node.get("commit")
    if not isinstance(commit, dict):
        return None
    raw_author = commit.get("author")
    author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
    raw_user = author.get("user")
    user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
    # A commit author is a git identity: the GitHub login when the email matches an
    # account, else the raw name from the commit itself.
    who = user.get("login") or author.get("name") or ""
    oid = commit.get("abbreviatedOid") or ""
    headline = commit.get("messageHeadline") or ""
    text = f"{who} pushed {oid}".strip() if oid else f"{who} pushed a commit".strip()
    return {
        "kind": "commit",
        "text": f"{text}: {headline}" if headline else text,
        "at": commit.get("committedDate"),
    }


# One timeline node type -> (our kind, a template over the actor login). The
# review branch is special-cased below because its wording depends on the state.
_TIMELINE_TEXT = {
    "IssueComment": ("comment", "{who} commented"),
    "HeadRefForcePushedEvent": ("push", "{who} force-pushed"),
    "ReviewRequestedEvent": ("review", "{who} requested a review"),
    "MergedEvent": ("merge", "{who} merged this"),
}

_REVIEW_EVENT_TEXT = {
    "APPROVED": "{who} approved",
    "CHANGES_REQUESTED": "{who} requested changes",
    "DISMISSED": "{who}'s review was dismissed",
}


def timeline(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Recent activity on the PR, newest first: ``[{"kind", "text", "at"}]``.

    ``timelineItems`` returns oldest-first within the requested window, so the
    list is reversed for display. A node whose type carries no wording here (a
    newly added item type, a drifted shape) is skipped rather than rendered blank.
    """
    out: list[dict[str, Any]] = []
    for node in _nodes(pr, "timelineItems"):
        kind_name = node.get("__typename")
        if kind_name == "PullRequestCommit":
            item = _commit_timeline_item(node)
            if item is not None:
                out.append(item)
            continue
        if kind_name == "PullRequestReview":
            who = _actor_login(node, "author")
            template = _REVIEW_EVENT_TEXT.get(node.get("state") or "", "{who} reviewed")
            out.append({"kind": "review", "text": template.format(who=who).strip(), "at": node.get("createdAt")})
            continue
        entry = _TIMELINE_TEXT.get(kind_name or "")
        if entry is None:
            continue
        kind, template = entry
        who = _actor_login(node, "author") or _actor_login(node, "actor")
        out.append({"kind": kind, "text": template.format(who=who).strip(), "at": node.get("createdAt")})
    out.reverse()
    return out


def _normalize_pull(pr: dict[str, Any], *, required_checks_only: bool = False) -> dict[str, Any]:
    people = reviewers(pr)
    return {
        "number": pr.get("number"),
        "url": pr.get("url"),
        "title": pr.get("title") or "",
        "state": pr.get("state"),
        "draft": bool(pr.get("isDraft", False)),
        "merged": bool(pr.get("merged", False)),
        "merge_state": merge_state(pr),
        "review_state": review_state(pr),
        "commenters": commenters(pr),
        "checks": check_summary(pr, required_checks_only=required_checks_only),
        "comments": comment_summary(pr),
        "author": (pr.get("author") or {}).get("login") or "",
        "base": pr.get("baseRefName"),
        "reviewers": people,
        "review_summary": review_summary(people),
        "diff": diff_summary(pr),
        "issues": linked_issues(pr),
        "timeline": timeline(pr),
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

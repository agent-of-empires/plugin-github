"""Pure GraphQL normalizers: review-state inference, check mapping, comments."""

from aoe_github_plugin import graphql


def _pr(decision=None, reviews=(), threads=(), rollup=None, branch_rule=None):
    return {
        "number": 1,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "reviewDecision": decision,
        "baseRef": {"branchProtectionRule": branch_rule} if branch_rule is not None else None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]} if rollup is not None else {"nodes": []},
        "reviews": {"nodes": list(reviews)},
        "reviewThreads": {"nodes": list(threads)},
    }


def _conn(*prs):
    return {"nodes": list(prs)}


def _rollup(state, contexts):
    return {"state": state, "contexts": {"nodes": list(contexts)}}


def _branch_rule(*, requires=True, contexts=(), checks=()):
    return {
        "requiresStatusChecks": requires,
        "requiredStatusCheckContexts": list(contexts),
        "requiredStatusChecks": list(checks),
    }


def _required_check(context, app=None):
    return {"context": context, "app": app}


def _checkrun(name, status, conclusion=None, **kwargs):
    out = {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": kwargs.get("url", "cu"),
        "completedAt": kwargs.get("completed_at"),
    }
    app = kwargs.get("app")
    if app is not None:
        out["checkSuite"] = {"app": app}
    return out


def _statusctx(context, state, url="su", created_at=None):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": url,
        "createdAt": created_at,
    }


def _thread(resolved, author="al", body="hi", path="a.py", line=3):
    return {
        "isResolved": resolved,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"author": {"login": author}, "bodyText": body, "url": "x"}]},
    }


def test_review_state_decision_wins():
    assert graphql.review_state(_pr(decision="APPROVED")) == "approved"
    assert graphql.review_state(_pr(decision="CHANGES_REQUESTED")) == "changes-requested"


def test_review_state_infers_commented_from_review_or_thread():
    assert graphql.review_state(_pr(reviews=[{"state": "COMMENTED"}])) == "commented"
    assert graphql.review_state(_pr(threads=[_thread(resolved=False)])) == "commented"
    # Resolved-only thread, no comment review, no decision -> waiting.
    assert graphql.review_state(_pr(threads=[_thread(resolved=True)])) == "waiting"


def test_check_summary_maps_runs_and_rollup():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("build", "COMPLETED", "SUCCESS"),
            _checkrun("test", "COMPLETED", "FAILURE"),
            _checkrun("lint", "IN_PROGRESS"),
            _checkrun("deploy", "QUEUED"),
            _statusctx("legacy", "PENDING"),
        ],
    )
    summary = graphql.check_summary(_pr(rollup=rollup))
    assert summary["state"] == "failing"
    states = [(r["name"], r["state"]) for r in summary["runs"]]
    # Sorted failing -> running -> queued -> succeeded; GitHub's order is kept
    # within a group (lint before legacy, both running).
    assert states == [
        ("test", "failing"),
        ("lint", "running"),
        ("legacy", "running"),
        ("deploy", "queued"),
        ("build", "succeeded"),
    ]


def test_check_summary_none_without_rollup():
    assert graphql.check_summary(_pr(rollup=None)) is None


def test_check_summary_collapses_same_named_runs():
    # Reusable workflow invoked thrice: three runs share one display name (#37).
    rollup = _rollup(
        "SUCCESS",
        [
            _checkrun("Lint PR title", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("Lint PR title", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:01:00Z"),
            _checkrun("Lint PR title", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:02:00Z"),
        ],
    )
    summary = graphql.check_summary(_pr(rollup=rollup))
    names = [r["name"] for r in summary["runs"]]
    assert names == ["Lint PR title"]


def test_check_summary_keeps_latest_same_named_run():
    # Older failure, newer success: the newest timestamp wins regardless of order.
    rollup = _rollup(
        "SUCCESS",
        [
            _checkrun("flaky", "COMPLETED", "FAILURE", url="old", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("flaky", "COMPLETED", "SUCCESS", url="new", completed_at="2024-01-01T00:05:00Z"),
        ],
    )
    summary = graphql.check_summary(_pr(rollup=rollup))
    assert [(r["name"], r["state"], r["url"]) for r in summary["runs"]] == [("flaky", "succeeded", "new")]


def test_check_summary_tie_keeps_worst_state():
    # Equal (here, missing) timestamps: keep the worse state so a flake cannot
    # hide a real failure.
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("tie", "COMPLETED", "SUCCESS", url="ok"),
            _checkrun("tie", "COMPLETED", "FAILURE", url="bad"),
        ],
    )
    summary = graphql.check_summary(_pr(rollup=rollup))
    assert [(r["name"], r["state"], r["url"]) for r in summary["runs"]] == [("tie", "failing", "bad")]


def test_required_check_mode_ignores_optional_failures_for_rollup():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("optional-lint", "COMPLETED", "FAILURE"),
            _checkrun("required-build", "COMPLETED", "SUCCESS"),
        ],
    )
    summary = graphql.check_summary(
        _pr(rollup=rollup, branch_rule=_branch_rule(contexts=["required-build"])),
        required_checks_only=True,
    )
    assert summary["state"] == "succeeded"
    by_name = {run["name"]: run for run in summary["runs"]}
    assert by_name["required-build"]["required"] is True
    assert by_name["optional-lint"]["state"] == "failing"
    assert by_name["optional-lint"]["required"] is False


def test_required_check_mode_keeps_required_failures_failing():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("optional-lint", "COMPLETED", "SUCCESS"),
            _checkrun("required-build", "COMPLETED", "FAILURE"),
        ],
    )
    summary = graphql.check_summary(
        _pr(rollup=rollup, branch_rule=_branch_rule(contexts=["required-build"])),
        required_checks_only=True,
    )
    assert summary["state"] == "failing"


def test_required_check_mode_falls_back_without_required_metadata():
    rollup = _rollup("FAILURE", [_checkrun("optional-lint", "COMPLETED", "FAILURE")])
    summary = graphql.check_summary(_pr(rollup=rollup), required_checks_only=True)
    assert summary["state"] == "failing"
    assert "required" not in summary["runs"][0]


def test_required_check_mode_matches_app_specific_required_checks():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("build", "COMPLETED", "FAILURE", app={"slug": "optional-ci"}),
            _checkrun("build", "COMPLETED", "SUCCESS", app={"slug": "github-actions"}),
        ],
    )
    summary = graphql.check_summary(
        _pr(
            rollup=rollup,
            branch_rule=_branch_rule(checks=[_required_check("build", {"slug": "github-actions"})]),
        ),
        required_checks_only=True,
    )
    assert summary["state"] == "succeeded"
    assert [(run["app"], run["required"]) for run in summary["runs"]] == [
        ("optional-ci", False),
        ("github-actions", True),
    ]


def test_comment_summary_keeps_only_unresolved():
    pr = _pr(threads=[_thread(resolved=True), _thread(resolved=False, author="bo", body="please fix")])
    summary = graphql.comment_summary(pr)
    assert summary["unresolved"] == 1
    assert summary["items"][0]["author"] == "bo"
    assert summary["items"][0]["resolved"] is False


def test_excerpt_collapses_and_truncates():
    assert graphql.excerpt("  a\n\n b  ") == "a b"
    long = "x" * 500
    out = graphql.excerpt(long, limit=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_normalize_connection_is_total_on_garbage():
    assert graphql.normalize_connection({}) == []
    assert graphql.normalize_connection(None) == []
    assert graphql.normalize_connection({"nodes": None}) == []
    assert graphql.normalize_connection({"nodes": ["junk", 3]}) == []
    pulls = graphql.normalize_connection(_conn(_pr(decision="APPROVED", rollup=_rollup("SUCCESS", []))))
    assert pulls[0]["review_state"] == "approved"
    assert pulls[0]["checks"]["state"] == "succeeded"


def test_normalize_pull_with_no_commit_or_threads_does_not_raise():
    # A freshly opened PR before commit metadata lands: empty commits/threads.
    pr = {
        "number": 1,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "isDraft": True,
        "merged": False,
        "reviewDecision": None,
        "commits": {"nodes": []},
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
    }
    out = graphql.normalize_connection(_conn(pr))[0]
    assert out["checks"] is None
    assert out["review_state"] == "waiting"
    assert out["comments"] == {"unresolved": 0, "items": []}


def test_build_query_aliases_each_branch_with_variables():
    q = graphql.build_query(3)
    # One typed variable + one aliased field per branch, sharing the fragment.
    for i in range(3):
        assert f"$b{i}: String!" in q
        assert f"b{i}: pullRequests(" in q
        assert f"headRefName: $b{i}" in q
    assert "fragment PRConnection on PullRequestConnection" in q
    assert "reviewThreads(first: 100)" in q
    assert "pageInfo { hasNextPage endCursor }" in q


def test_build_query_single_alias_is_just_one_branch():
    q = graphql.build_query(1)
    assert "$b0: String!" in q
    assert "$b1" not in q


def test_build_query_floors_at_one_alias():
    # A zero/negative count never yields an empty, malformed document.
    assert "b0: pullRequests(" in graphql.build_query(0)


# --- digest tier (#69) ---


def test_build_digest_query_aliases_and_omits_rich_connections():
    q = graphql.build_digest_query(3)
    for i in range(3):
        assert f"b{i}: pullRequests" in q
        assert f"$b{i}: String!" in q
    assert "rateLimit { cost remaining resetAt }" in q
    # The whole point: none of the expensive nested connections.
    assert "contexts" not in q
    assert "comments" not in q
    assert "PRConnection" not in q


def test_digest_signature_equal_for_digest_and_full_shapes():
    # The same PR seen through the full fragment (extra fields) and the digest
    # fragment (minimal fields) must produce the same signature.
    full_node = _pr(decision="APPROVED", rollup=_rollup("SUCCESS", []))
    full_node.update({"updatedAt": "2026-01-01T00:00:00Z", "headRefOid": "abc", "id": "PR_1", "title": "t"})
    full_node["reviewThreads"]["totalCount"] = 2
    digest_node = {
        "number": 1,
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "reviewDecision": "APPROVED",
        "updatedAt": "2026-01-01T00:00:00Z",
        "headRefOid": "abc",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviewThreads": {"totalCount": 2},
    }
    sig_full = graphql.digest_signature(_conn(full_node))
    sig_digest = graphql.digest_signature(_conn(digest_node))
    assert sig_full is not None
    assert sig_full == sig_digest


def test_digest_signature_changes_on_each_watched_field():
    base = {
        "number": 1,
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "reviewDecision": None,
        "updatedAt": "2026-01-01T00:00:00Z",
        "headRefOid": "abc",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "PENDING"}}}]},
        "reviewThreads": {"totalCount": 0},
    }
    sig = graphql.digest_signature(_conn(dict(base)))
    for field, value in [
        ("state", "MERGED"),
        ("isDraft", True),
        ("merged", True),
        ("reviewDecision", "APPROVED"),
        ("updatedAt", "2026-01-02T00:00:00Z"),
        ("headRefOid", "def"),
        ("reviewThreads", {"totalCount": 3}),
        ("mergeable", "CONFLICTING"),
    ]:
        node = dict(base)
        node[field] = value
        assert graphql.digest_signature(_conn(node)) != sig, field
    node = dict(base)
    node["commits"] = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}
    assert graphql.digest_signature(_conn(node)) != sig


def test_digest_signature_none_on_malformed():
    assert graphql.digest_signature(None) is None
    assert graphql.digest_signature({"nodes": "garbage"}) is None
    assert graphql.digest_signature({"nodes": ["not-a-dict"]}) is None
    assert graphql.digest_signature(_conn()) is not None  # empty connection is a valid "no PRs"


def test_merge_state_maps_only_conflicting():
    assert graphql.merge_state({"mergeable": "CONFLICTING"}) == "conflicts"
    # MERGEABLE is healthy; UNKNOWN is GitHub's transient async-compute value;
    # missing is the no-token / partial shape. None of them are a conflict.
    assert graphql.merge_state({"mergeable": "MERGEABLE"}) is None
    assert graphql.merge_state({"mergeable": "UNKNOWN"}) is None
    assert graphql.merge_state({"mergeable": None}) is None
    assert graphql.merge_state({}) is None


def test_normalize_pull_exposes_merge_state():
    conflicting = _pr()
    conflicting["mergeable"] = "CONFLICTING"
    clean = _pr()
    clean["mergeable"] = "MERGEABLE"
    assert graphql.normalize_connection(_conn(conflicting))[0]["merge_state"] == "conflicts"
    assert graphql.normalize_connection(_conn(clean))[0]["merge_state"] is None
    # No mergeable field (no-token / partial) degrades to no signal, not a raise.
    assert graphql.normalize_connection(_conn(_pr()))[0]["merge_state"] is None


def test_digest_signature_tracks_conflict_not_transient_unknown():
    # Canonicalized to the conflict bool, so MERGEABLE and UNKNOWN are
    # signature-equal: the MERGEABLE -> UNKNOWN -> MERGEABLE churn GitHub emits
    # on every push must not re-fire the expensive rich query. But a real
    # conflict (CONFLICTING) still diffs, so onset is detected immediately.
    def node(mergeable):
        base = {
            "number": 1,
            "state": "OPEN",
            "isDraft": False,
            "merged": False,
            "reviewDecision": None,
            "updatedAt": "2026-01-01T00:00:00Z",
            "headRefOid": "abc",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
            "reviewThreads": {"totalCount": 0},
        }
        if mergeable is not None:
            base["mergeable"] = mergeable
        return base

    sig_clean = graphql.digest_signature(_conn(node("MERGEABLE")))
    sig_unknown = graphql.digest_signature(_conn(node("UNKNOWN")))
    sig_missing = graphql.digest_signature(_conn(node(None)))
    sig_conflict = graphql.digest_signature(_conn(node("CONFLICTING")))
    assert sig_clean == sig_unknown == sig_missing
    assert sig_conflict != sig_clean

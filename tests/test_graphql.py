"""Pure GraphQL normalizers: review-state inference, check mapping, comments."""

from aoe_github_plugin import graphql


def _pr(decision=None, reviews=(), threads=(), rollup=None):
    return {
        "number": 1,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "reviewDecision": decision,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]} if rollup is not None else {"nodes": []},
        "reviews": {"nodes": list(reviews)},
        "reviewThreads": {"nodes": list(threads)},
    }


def _conn(*prs):
    return {"nodes": list(prs)}


def _rollup(state, contexts):
    return {"state": state, "contexts": {"nodes": list(contexts)}}


def _checkrun(name, status, conclusion=None, url="cu"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion, "detailsUrl": url}


def _statusctx(context, state, url="su"):
    return {"__typename": "StatusContext", "context": context, "state": state, "targetUrl": url}


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

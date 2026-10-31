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


def _rollup(state, contexts, total=None):
    out = {"state": state, "contexts": {"nodes": list(contexts)}}
    if total is not None:
        out["contexts"]["totalCount"] = total
    return out


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


def _latest(state, oid, login="jules"):
    return {"state": state, "author": {"login": login}, "commit": {"oid": oid}}


def _approval_recommendation():
    return {
        "state": "COMMENTED",
        "body": "Recommendation: Approve",
        "author": {"login": "reviewbot", "__typename": "Bot"},
    }


def test_review_state_changes_pushed_only_when_every_blocking_review_is_stale():
    def pr(*latest):
        out = _pr(decision="CHANGES_REQUESTED")
        out["headRefOid"] = "new"
        out["latestReviews"] = {"nodes": list(latest)}
        return out

    cases = [
        # Pushed since the only blocking review.
        ([_latest("CHANGES_REQUESTED", "old")], "changes-pushed"),
        # Reviewed the current head: still a live block.
        ([_latest("CHANGES_REQUESTED", "new")], "changes-requested"),
        # One blocking reviewer is current, so the PR is not yet "pushed since".
        ([_latest("CHANGES_REQUESTED", "old"), _latest("CHANGES_REQUESTED", "new", "nate")], "changes-requested"),
        # An approval does not count as blocking, however old.
        ([_latest("CHANGES_REQUESTED", "old"), _latest("APPROVED", "old", "nate")], "changes-pushed"),
        # Unknown commit or no review data degrades to the conservative state.
        ([{"state": "CHANGES_REQUESTED", "author": {"login": "jules"}}], "changes-requested"),
        ([], "changes-requested"),
    ]
    for latest, expected in cases:
        assert graphql.review_state(pr(*latest)) == expected, latest


def test_review_state_sees_block_dropped_from_latest_reviews_by_a_re_request():
    # Real shape (agent-of-empires#4518): njbrake was re-requested, so GitHub drops
    # his review from `latestReviews`, but `reviewDecision` stays CHANGES_REQUESTED
    # and `decidedReviews` still carries it.
    def pr(blocked_on):
        out = _pr(decision="CHANGES_REQUESTED")
        out["headRefOid"] = "new"
        out["latestReviews"] = {"nodes": [_latest("COMMENTED", "x", "coderabbitai")]}
        out["decidedReviews"] = {"nodes": [_latest("CHANGES_REQUESTED", blocked_on, "njbrake")]}
        out["reviewRequests"] = {"nodes": [{"requestedReviewer": {"login": "njbrake"}}]}
        return out

    assert graphql.review_state(pr("old")) == "changes-pushed"
    assert graphql.review_state(pr("new")) == "changes-requested"
    assert graphql.reviewers(pr("old")) == [
        {"name": "coderabbitai", "state": "commented"},
        {"name": "njbrake", "state": "changes-pushed"},
    ]
    # A later approval from the same reviewer clears their block.
    cleared = pr("old")
    cleared["decidedReviews"]["nodes"].append(_latest("APPROVED", "new", "njbrake"))
    assert graphql.review_state(cleared) == "changes-requested"  # decision still says so; no blocking review known


def test_review_state_infers_commented_from_review_or_thread():
    assert graphql.review_state(_pr(reviews=[{"state": "COMMENTED"}])) == "commented"
    assert graphql.review_state(_pr(threads=[_thread(resolved=False)])) == "commented"
    # Resolved-only thread, no comment review, no decision -> waiting.
    assert graphql.review_state(_pr(threads=[_thread(resolved=True)])) == "waiting"


def test_bot_approval_recommendation_is_opt_in():
    bot_recommendation = _approval_recommendation()
    pr = _pr(reviews=[bot_recommendation])
    pr["latestReviews"] = {"nodes": [bot_recommendation]}
    # Default (off): the recommendation counts like any other comment.
    assert graphql.review_state(pr) == "commented"
    assert graphql.commenters(pr) == ["reviewbot"]
    assert graphql.reviewers(pr) == [{"name": "reviewbot", "state": "commented"}]
    pulled = graphql.normalize_connection(_conn(pr))[0]
    assert pulled["review_state"] == "commented"
    assert pulled["commenters"] == ["reviewbot"]
    assert pulled["reviewers"] == [{"name": "reviewbot", "state": "commented"}]

    # Opting in excludes the bot approval recommendation from all three outputs.
    assert graphql.review_state(pr, ignore_bot_approval_recommendations=True) == "waiting"
    assert graphql.commenters(pr, ignore_bot_approval_recommendations=True) == []
    assert graphql.reviewers(pr, ignore_bot_approval_recommendations=True) == []
    opted = graphql.normalize_connection(_conn(pr), ignore_bot_approval_recommendations=True)[0]
    assert opted["review_state"] == "waiting"
    assert opted["commenters"] == []
    assert opted["reviewers"] == []

    bot_comment = {**bot_recommendation, "body": "Please update this section."}
    bot = _pr(reviews=[bot_comment])
    bot["latestReviews"] = {"nodes": [bot_comment]}
    assert graphql.review_state(bot, ignore_bot_approval_recommendations=True) == "commented"
    assert graphql.commenters(bot, ignore_bot_approval_recommendations=True) == ["reviewbot"]
    assert graphql.reviewers(bot, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewbot", "state": "commented"}
    ]

    human_recommendation = {
        **bot_recommendation,
        "author": {"login": "reviewer", "__typename": "User"},
    }
    human = _pr(reviews=[human_recommendation])
    human["latestReviews"] = {"nodes": [human_recommendation]}
    assert graphql.review_state(human, ignore_bot_approval_recommendations=True) == "commented"
    assert graphql.commenters(human, ignore_bot_approval_recommendations=True) == ["reviewer"]
    assert graphql.reviewers(human, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewer", "state": "commented"}
    ]

    threaded = _pr(reviews=[bot_recommendation], threads=[_thread(resolved=False, author="reviewbot")])
    threaded["latestReviews"] = {"nodes": [bot_recommendation]}
    assert graphql.review_state(threaded, ignore_bot_approval_recommendations=True) == "commented"
    assert graphql.commenters(threaded, ignore_bot_approval_recommendations=True) == ["reviewbot"]
    # An unresolved thread is genuine feedback, so the bot stays a reviewer.
    assert graphql.reviewers(threaded, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewbot", "state": "commented"}
    ]

    # An earlier genuine COMMENTED review also keeps the bot a reviewer even
    # though its latest review is the excluded recommendation.
    prior_comment = {**bot_recommendation, "body": "Please update this section."}
    both = _pr(reviews=[prior_comment, bot_recommendation])
    both["latestReviews"] = {"nodes": [bot_recommendation]}
    assert graphql.review_state(both, ignore_bot_approval_recommendations=True) == "commented"
    assert graphql.commenters(both, ignore_bot_approval_recommendations=True) == ["reviewbot"]
    assert graphql.reviewers(both, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewbot", "state": "commented"}
    ]

    formal_approval = {**bot_recommendation, "state": "APPROVED"}
    approved = _pr(decision="APPROVED")
    approved["latestReviews"] = {"nodes": [formal_approval]}
    assert graphql.review_state(approved, ignore_bot_approval_recommendations=True) == "approved"
    assert graphql.reviewers(approved, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewbot", "state": "approved"}
    ]


def test_standing_block_outranks_retained_bot_comment():
    # A standing CHANGES_REQUESTED outranks a retained comment: a bot that
    # blocked, then commented, then recommended approval must still show the
    # block in reviewers (a comment does not dismiss the earlier block).
    rec = _approval_recommendation()
    prior_comment = {**rec, "body": "Please update this section."}
    blocked = _pr(decision="CHANGES_REQUESTED")
    blocked["reviews"] = {"nodes": [prior_comment, rec]}
    blocked["latestReviews"] = {"nodes": [rec]}
    blocked["decidedReviews"] = {"nodes": [_latest("CHANGES_REQUESTED", "head", "reviewbot")]}
    blocked["headRefOid"] = "head"
    assert graphql.review_state(blocked, ignore_bot_approval_recommendations=True) == "changes-requested"
    assert graphql.reviewers(blocked, ignore_bot_approval_recommendations=True) == [
        {"name": "reviewbot", "state": "changes-requested"}
    ]


def test_ignored_commenters_only_downgrade_bot_only_commented():
    ignored = graphql.parse_ignored_commenters("CodeRabbitAI, codecov[bot]")
    assert ignored == {"coderabbitai", "codecov"}
    bot = _pr(
        reviews=[{"state": "COMMENTED", "author": {"login": "coderabbitai"}}],
        threads=[_thread(resolved=False, author="coderabbitai[bot]"), _thread(resolved=True, author="al")],
    )
    pull = graphql.normalize_connection(_conn(bot))[0]
    assert pull["commenters"] == ["coderabbitai", "coderabbitai[bot]"]
    assert graphql.drop_ignored_commenters(pull, ignored)["review_state"] == "waiting"
    assert pull["review_state"] == "commented"  # copy, cached pull untouched
    assert pull["comments"]["unresolved"] == 1  # bot threads still count
    # One human commenter keeps the state; so does an empty ignore list.
    human = graphql.normalize_connection(_conn(_pr(reviews=[{"state": "COMMENTED", "author": {"login": "seluj78"}}])))[
        0
    ]
    assert graphql.drop_ignored_commenters(human, ignored)["review_state"] == "commented"
    assert graphql.drop_ignored_commenters(pull, frozenset())["review_state"] == "commented"
    # The PR author replying to threads on their own PR is not "commented".
    own = {**human, "author": "Seluj78", "commenters": ["Seluj78", "coderabbitai"]}
    assert graphql.drop_ignored_commenters(own, ignored)["review_state"] == "waiting"
    assert graphql.drop_ignored_commenters({**own, "author": "lucas"}, ignored)["review_state"] == "commented"
    # An older persisted pull with no commenters is left alone.
    assert graphql.drop_ignored_commenters({"review_state": "commented"}, ignored)["review_state"] == "commented"


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


def test_check_summary_state_ignores_superseded_failure_in_sticky_rollup():
    # GitHub keeps the rollup at FAILURE after a failed run is re-run green (#98),
    # so the headline state must follow the latest run per check, not the rollup.
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("build", "COMPLETED", "FAILURE", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("test", "COMPLETED", "CANCELLED", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("build", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:05:00Z"),
            _checkrun("test", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:05:00Z"),
        ],
        total=4,
    )
    summary = graphql.check_summary(_pr(rollup=rollup))
    assert summary["state"] == "succeeded"


def test_check_summary_state_failing_when_latest_run_fails():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("build", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("build", "COMPLETED", "FAILURE", completed_at="2024-01-01T00:05:00Z"),
        ],
        total=2,
    )
    assert graphql.check_summary(_pr(rollup=rollup))["state"] == "failing"


def test_check_summary_state_running_when_rerun_in_flight():
    rollup = _rollup(
        "FAILURE",
        [
            _checkrun("build", "COMPLETED", "FAILURE", completed_at="2024-01-01T00:00:00Z"),
            _checkrun("build", "IN_PROGRESS", completed_at="2024-01-01T00:05:00Z"),
        ],
        total=2,
    )
    assert graphql.check_summary(_pr(rollup=rollup))["state"] == "running"


def test_check_summary_state_keeps_rollup_when_contexts_truncated():
    # More contexts exist than were fetched: a failing run may sit past the cap,
    # so the raw rollup stays authoritative rather than risk a false green.
    rollup = _rollup("FAILURE", [_checkrun("build", "COMPLETED", "SUCCESS")], total=60)
    assert graphql.check_summary(_pr(rollup=rollup))["state"] == "failing"


def test_check_summary_state_green_for_pr_with_more_than_fifty_checks():
    # 56 contexts, a superseded failure, and every latest run green (#98 follow-up):
    # the page must hold them all so the headline is not pinned to the sticky rollup.
    runs = [_checkrun(f"job-{i}", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:05:00Z") for i in range(54)]
    runs += [
        _checkrun("flaky", "COMPLETED", "FAILURE", completed_at="2024-01-01T00:00:00Z"),
        _checkrun("flaky", "COMPLETED", "SUCCESS", completed_at="2024-01-01T00:05:00Z"),
    ]
    summary = graphql.check_summary(_pr(rollup=_rollup("FAILURE", runs, total=56)))
    assert summary["state"] == "succeeded"


def test_queries_fetch_a_full_page_of_contexts():
    # first:100 is GitHub's connection max and costs the same points as first:50.
    assert "contexts(first: 100)" in graphql.build_query(1)


def test_check_summary_state_falls_back_to_rollup_without_runs():
    assert graphql.check_summary(_pr(rollup=_rollup("SUCCESS", [])))["state"] == "succeeded"
    assert graphql.check_summary(_pr(rollup=_rollup("PENDING", [])))["state"] == "running"


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


def test_normalize_pull_exposes_merge_queue_and_degrades_without_entry():
    queued = _pr(decision="APPROVED")
    queued["mergeQueueEntry"] = {"position": 2, "state": "AWAITING_CHECKS"}
    queued["mergeQueue"] = {"url": "https://github.com/o/r/queue/main"}
    without_queue = _pr()
    without_queue["mergeQueueEntry"] = None
    assert graphql.normalize_connection(_conn(queued))[0]["merge_queue"] == {
        "position": 2,
        "state": "awaiting-checks",
        "url": "https://github.com/o/r/queue/main",
    }
    assert graphql.normalize_connection(_conn(without_queue))[0]["merge_queue"] is None


def test_merge_queue_position_rejects_invalid_values():
    queued = _pr()
    queued["mergeQueueEntry"] = {"position": True, "state": None}
    assert graphql.normalize_connection(_conn(queued))[0]["merge_queue"] == {
        "position": None,
        "state": "unknown",
    }


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
    assert "mergeQueueEntry { position state }" in q
    assert "mergeQueue { url }" in q


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
    assert "mergeQueueEntry" in q


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
    full_node["mergeQueueEntry"] = {"position": 2, "state": "QUEUED"}
    full_node["mergeQueue"] = {"url": "https://github.com/org/repo/queues/1"}
    digest_node["mergeQueueEntry"] = {"position": 2, "state": "QUEUED"}
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
        ("mergeQueueEntry", {"position": 2, "state": "QUEUED"}),
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


# --- api_version 12 pane data: reviewers, diff, linked issues, timeline ---


def test_reviewers_merge_latest_reviews_with_outstanding_requests():
    pr = {
        "latestReviews": {
            "nodes": [
                {"state": "APPROVED", "author": {"login": "nate"}},
                {"state": "CHANGES_REQUESTED", "author": {"login": "jules"}},
                {"state": "PENDING", "author": {"login": "half-written"}},
                {"state": "APPROVED", "author": None},
            ]
        },
        # `jules` was re-requested after reviewing: the submitted position is what
        # is still on record, so it must not be downgraded to pending.
        "reviewRequests": {
            "nodes": [
                {"requestedReviewer": {"login": "jules"}},
                {"requestedReviewer": {"name": "platform-team"}},
                {"requestedReviewer": None},
            ]
        },
    }
    assert graphql.reviewers(pr) == [
        {"name": "nate", "state": "approved"},
        {"name": "jules", "state": "changes-requested"},
        {"name": "half-written", "state": "pending"},
        {"name": "platform-team", "state": "pending"},
    ]
    stale = {"headRefOid": "new", "latestReviews": {"nodes": [_latest("CHANGES_REQUESTED", "old")]}}
    assert graphql.reviewers(stale) == [{"name": "jules", "state": "changes-pushed"}]
    # Total: a malformed PR yields no reviewers rather than raising.
    assert graphql.reviewers({}) == []


def test_review_summary_reads_the_way_github_does():
    cases = [
        ([], ""),
        ([{"name": "a", "state": "approved"}], "1 approved"),
        ([{"name": "a", "state": "approved"}, {"name": "b", "state": "approved"}], "2 approved"),
        ([{"name": "a", "state": "approved"}, {"name": "b", "state": "pending"}], "1 of 2 approved"),
        ([{"name": "a", "state": "pending"}, {"name": "b", "state": "pending"}], "2 pending"),
        # A blocking review wins regardless of how many approvals sit beside it.
        ([{"name": "a", "state": "approved"}, {"name": "b", "state": "changes-requested"}], "changes requested"),
        (
            [{"name": "a", "state": "approved"}, {"name": "b", "state": "changes-pushed"}],
            "pushed since review, 1 approved",
        ),
        ([{"name": "a", "state": "changes-pushed"}, {"name": "b", "state": "changes-requested"}], "changes requested"),
    ]
    for people, expected in cases:
        assert graphql.review_summary(people) == expected, people


def test_diff_summary_needs_every_field_to_be_a_count():
    assert graphql.diff_summary({"additions": 842, "deletions": 317, "changedFiles": 18}) == {
        "added": 842,
        "removed": 317,
        "files": 18,
    }
    # The no-token REST shape carries none of these, and a partial/odd shape must
    # not render half a diff card.
    for bad in (
        {},
        {"additions": 1, "deletions": 2},
        {"additions": 1, "deletions": -2, "changedFiles": 3},
        {"additions": True, "deletions": 0, "changedFiles": 0},
    ):
        assert graphql.diff_summary(bad) is None, bad


def test_linked_issues_keep_number_title_url_and_state():
    pr = {
        "closingIssuesReferences": {
            "nodes": [
                {"number": 3180, "title": "Stale daemon", "url": "https://gh/i/3180", "state": "OPEN"},
                {"number": 3204, "title": "Drift", "url": "https://gh/i/3204", "state": "CLOSED"},
                {"title": "no number"},
            ]
        }
    }
    assert graphql.linked_issues(pr) == [
        {"number": 3180, "title": "Stale daemon", "url": "https://gh/i/3180", "state": "open"},
        {"number": 3204, "title": "Drift", "url": "https://gh/i/3204", "state": "closed"},
    ]
    assert graphql.linked_issues({}) == []


def test_timeline_reverses_to_newest_first_and_skips_unknown_types():
    pr = {
        "timelineItems": {
            "nodes": [
                {
                    "__typename": "PullRequestCommit",
                    "commit": {
                        "abbreviatedOid": "4f9a1c2",
                        "committedDate": "2026-06-29T11:40:00Z",
                        "messageHeadline": "fix the thing",
                        "author": {"user": {"login": "njbrake"}, "name": "N B"},
                    },
                },
                {"__typename": "IssueComment", "createdAt": "2026-06-29T11:52:00Z", "author": {"login": "seluj78"}},
                {
                    "__typename": "PullRequestReview",
                    "state": "CHANGES_REQUESTED",
                    "createdAt": "2026-06-29T11:58:00Z",
                    "author": {"login": "nate"},
                },
                {"__typename": "SomeFutureEvent", "createdAt": "2026-06-29T12:00:00Z"},
            ]
        }
    }
    assert graphql.timeline(pr) == [
        {"kind": "review", "text": "nate requested changes", "at": "2026-06-29T11:58:00Z"},
        {"kind": "comment", "text": "seluj78 commented", "at": "2026-06-29T11:52:00Z"},
        {"kind": "commit", "text": "njbrake pushed 4f9a1c2: fix the thing", "at": "2026-06-29T11:40:00Z"},
    ]
    assert graphql.timeline({}) == []


def test_check_runs_carry_duration_workflow_group_and_a_skipped_flag():
    def ctx(name, conclusion, started, completed, workflow):
        return {
            "__typename": "CheckRun",
            "name": name,
            "status": "COMPLETED",
            "conclusion": conclusion,
            "detailsUrl": "https://ci/1",
            "startedAt": started,
            "completedAt": completed,
            "checkSuite": {"app": {"slug": "github-actions"}, "workflowRun": {"workflow": {"name": workflow}}},
        }

    pr = {
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": {
                                "nodes": [
                                    ctx("Clippy", "SUCCESS", "2026-01-01T00:00:00Z", "2026-01-01T00:01:28Z", "Lint"),
                                    ctx("Vitest", "SUCCESS", "2026-01-01T00:00:00Z", "2026-01-01T00:00:48Z", "Web"),
                                    ctx("close-stale", "SKIPPED", None, None, "Meta"),
                                ]
                            },
                        }
                    }
                }
            ]
        }
    }
    runs = {r["name"]: r for r in graphql.check_summary(pr)["runs"]}
    assert runs["Clippy"]["duration"] == "1m 28s"
    assert runs["Vitest"]["duration"] == "48s"
    assert runs["Clippy"]["group"] == "Lint"
    # A skipped run still reads as succeeded for the rollup (it blocks nothing),
    # but carries the flag so the pane can list it apart from the real passes.
    assert runs["close-stale"]["state"] == "succeeded"
    assert runs["close-stale"]["skipped"] is True
    assert "duration" not in runs["close-stale"]
    assert "skipped" not in runs["Clippy"]


def test_approve_over_stale_block_upgrades_only_when_someone_approved_the_head():
    people = [{"name": "a", "state": "approved"}, {"name": "b", "state": "changes-pushed"}]
    pull = {"review_state": "changes-pushed", "reviewers": people, "head_approved": True, "review_requested": False}
    assert graphql.approve_over_stale_block(pull)["review_state"] == "approved"
    assert pull["review_state"] == "changes-pushed"
    # Someone is still requested (e.g. a code-owner group): not fully approved.
    waiting = {**pull, "reviewers": [*people, {"name": "acp", "state": "pending"}], "review_requested": True}
    assert graphql.approve_over_stale_block(waiting)["review_state"] == "partially-approved"
    # The stale reviewer themselves was re-requested: no separate pending entry, still requested.
    rerequested = {**pull, "review_requested": True}
    assert graphql.approve_over_stale_block(rerequested)["review_state"] == "partially-approved"
    # No approval on the current head, or no approval at all.
    assert graphql.approve_over_stale_block({**pull, "head_approved": False})["review_state"] == "changes-pushed"
    lone = {"review_state": "changes-pushed", "reviewers": people[1:]}
    assert graphql.approve_over_stale_block(lone) is lone
    live = {"review_state": "changes-requested", "reviewers": people, "head_approved": True}
    assert graphql.approve_over_stale_block(live) is live
    assert graphql.approve_over_stale_block({"review_state": "changes-pushed"})["review_state"] == "changes-pushed"


def test_review_state_approved_needs_nobody_else_outstanding():
    def pr(**extra):
        out = _pr(decision="APPROVED")
        out.update(extra)
        return out

    green = {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": []}}}}]}
    assert graphql.review_state(pr()) == "approved"
    # A requested reviewer or team (code-owner group) is still outstanding.
    requested = {"nodes": [{"requestedReviewer": {"__typename": "Team", "name": "acp"}}]}
    assert graphql.review_state(pr(reviewRequests=requested)) == "partially-approved"
    # Blocked with green CI can only be a missing review; blocked with no green CI says nothing.
    assert graphql.review_state(pr(mergeStateStatus="BLOCKED", commits=green)) == "partially-approved"
    assert graphql.review_state(pr(mergeStateStatus="BLOCKED")) == "approved"
    assert graphql.review_state(pr(mergeStateStatus="BLOCKED", commits=green, isDraft=True)) == "approved"
    assert graphql.review_state(pr(mergeStateStatus="CLEAN", commits=green)) == "approved"
    # A failed run re-run green: the raw rollup stays FAILURE but the checks read green.
    rerun = {
        "nodes": [
            {
                "commit": {
                    "statusCheckRollup": {
                        "state": "FAILURE",
                        "contexts": {
                            "totalCount": 2,
                            "nodes": [
                                _checkrun("Clippy", "COMPLETED", "FAILURE", completed_at="2026-01-01T00:00:00Z"),
                                _checkrun("Clippy", "COMPLETED", "SUCCESS", completed_at="2026-01-01T00:05:00Z"),
                            ],
                        },
                    }
                }
            }
        ]
    }
    assert graphql.review_state(pr(mergeStateStatus="BLOCKED", commits=rerun)) == "partially-approved"


def test_head_approved_ignores_approvals_on_older_commits():
    pr = {"headRefOid": "new", "latestReviews": {"nodes": [_latest("APPROVED", "old")]}}
    assert graphql.head_approved(pr) is False
    pr["latestReviews"]["nodes"].append(_latest("APPROVED", "new", "nate"))
    assert graphql.head_approved(pr) is True

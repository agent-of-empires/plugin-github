import json
import queue

import httpx
import pytest

from aoe_github_plugin import main
from aoe_github_plugin import suggestions
from aoe_github_plugin.client import GitHubClient


class FakeClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.variables = []

    def post_graphql(self, query, variables):
        self.variables.append(variables)
        return next(self.pages)


def _pr(url="https://github.com/acme/widgets/pull/42", **overrides):
    pr = {
        "url": url,
        "number": 42,
        "title": "Fix widget",
        "body": "Please fix the widget",
        "repository": {"nameWithOwner": "acme/widgets"},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "me"}}]},
        "reviews": {"nodes": []},
        "commits": {"nodes": []},
    }
    pr.update(overrides)
    return pr


def test_discovery_paginates_each_criterion_and_deduplicates_canonical_prs():
    client = FakeClient(
        [
            {"data": {"search": {"nodes": [_pr()], "pageInfo": {"hasNextPage": True, "endCursor": "page-2"}}}},
            {
                "data": {
                    "search": {
                        "nodes": [_pr("https://github.com/acme/widgets/pull/43", number=43)],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            },
            {"data": {"search": {"nodes": [_pr()], "pageInfo": {"hasNextPage": False}}}},
        ]
    )
    pulls = suggestions.discover_review_requests(
        client, criteria={"review_requested", "changes_requested"}, repos={"acme/widgets"}, orgs=set()
    )
    assert len(pulls) == 2
    assert client.variables[0]["query"] == "is:open author:@me review:changes_requested repo:acme/widgets"
    assert client.variables[1]["after"] == "page-2"
    assert client.variables[2]["query"] == "is:open review-requested:@me repo:acme/widgets"


def test_provider_uses_exact_mapping_and_builds_stable_task_not_source_instructions():
    pulls = [_pr()]
    kwargs = {"criteria": {"review_requested"}, "repos": set(), "orgs": set(), "mapping": {"acme/widgets": "Widgets"}}
    item = suggestions.build_suggestions(pulls, **kwargs)[0]
    assert item["id"] == suggestions.build_suggestions(pulls, **kwargs)[0]["id"]
    assert item["source_ref"] == "https://github.com/acme/widgets/pull/42"
    assert item["project"] == "Widgets"
    assert item["criterion"] == "review_requested"
    assert item["preview"] == "Please fix the widget"
    assert "untrusted source material" in item["draft"]["prompt"]
    unresolved = suggestions.build_suggestions(pulls, **{**kwargs, "mapping": {}})[0]
    assert "project" not in unresolved


@pytest.mark.parametrize(
    ("review_oid", "priority", "expected"),
    [
        ("old-head", ("changes_requested", "review_requested"), "review_requested"),
        ("old-head", ("review_requested", "changes_requested"), "review_requested"),
        ("head", ("changes_requested", "review_requested"), "changes_requested"),
        ("head", ("review_requested", "changes_requested"), "review_requested"),
    ],
)
def test_overlapping_search_results_choose_highest_priority_eligible_criterion(review_oid, priority, expected):
    pr = _pr(
        reviews={
            "nodes": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit": {"oid": review_oid}}]
        },
        commits={"nodes": [{"commit": {"oid": "head"}}]},
    )
    client = FakeClient(
        [
            {"data": {"search": {"nodes": [pr], "pageInfo": {"hasNextPage": False}}}},
            {"data": {"search": {"nodes": [pr], "pageInfo": {"hasNextPage": False}}}},
        ]
    )
    criteria = {"review_requested", "changes_requested"}
    pulls = suggestions.discover_review_requests(client, criteria=criteria, repos=set(), orgs=set(), priority=priority)
    items = suggestions.build_suggestions(
        pulls, criteria=criteria, repos=set(), orgs=set(), mapping={}, priority=priority
    )
    assert len(items) == 1
    assert items[0]["criterion"] == expected


@pytest.mark.parametrize(
    ("criterion", "review_oid", "expected"),
    [
        ("changes_requested", "old-head", []),
        ("changes_requested", "head", ["changes_requested"]),
        ("review_requested", "old-head", ["review_requested"]),
    ],
)
def test_single_search_result_does_not_invent_an_unmatched_criterion(criterion, review_oid, expected):
    pr = _pr(
        reviews={
            "nodes": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit": {"oid": review_oid}}]
        },
        commits={"nodes": [{"commit": {"oid": "head"}}]},
    )
    client = FakeClient([{"data": {"search": {"nodes": [pr], "pageInfo": {"hasNextPage": False}}}}])
    pulls = suggestions.discover_review_requests(client, criteria={criterion}, repos=set(), orgs=set())
    items = suggestions.build_suggestions(
        pulls, criteria={"review_requested", "changes_requested"}, repos=set(), orgs=set(), mapping={}
    )
    assert [item["criterion"] for item in items] == expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (["CHANGES_REQUESTED", "COMMENTED"], "changes_requested"),
        (["CHANGES_REQUESTED", "PENDING"], "changes_requested"),
        (["CHANGES_REQUESTED", "DISMISSED"], "review_requested"),
        (["CHANGES_REQUESTED", "DISMISSED", "CHANGES_REQUESTED"], "changes_requested"),
    ],
)
def test_latest_review_verdict_ignores_non_verdicts_and_honors_dismissal(states, expected):
    pr = _pr(
        reviews={
            "nodes": [{"author": {"login": "reviewer"}, "state": state, "commit": {"oid": "head"}} for state in states]
        },
        commits={"nodes": [{"commit": {"oid": "head"}}]},
    )
    assert suggestions._review_state(pr) == expected


def test_search_pagination_cap_rejects_incomplete_snapshot():
    pages = [
        {
            "data": {
                "search": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": f"page-{page + 1}"},
                }
            }
        }
        for page in range(suggestions.MAX_PAGES)
    ]
    client = FakeClient(pages)

    with pytest.raises(RuntimeError, match="complete pagination limit"):
        suggestions.discover_review_requests(client, criteria={"changes_requested"}, repos=set(), orgs=set())

    assert len(client.variables) == suggestions.MAX_PAGES


def test_failed_pagination_never_returns_partial_snapshot():
    client = FakeClient(
        [
            {"data": {"search": {"nodes": [_pr()], "pageInfo": {"hasNextPage": True, "endCursor": "p2"}}}},
            {"errors": [{"message": "rate limited"}], "data": {"search": {"nodes": []}}},
        ]
    )
    with pytest.raises(RuntimeError, match="incomplete"):
        suggestions.discover_review_requests(client, criteria={"review_requested"}, repos=set(), orgs=set())


def _host_config(method, params, timeout=10.0):
    assert method == "config.get"
    assert params["key"].startswith("suggestion_")
    assert timeout > 0
    return {"value": None}


def test_runtime_refresh_publishes_complete_snapshot_and_retains_it_on_error(monkeypatch):
    sent = []
    runtime = main.Runtime(send=sent.append)
    runtime.call_host = _host_config

    def fetch(**kwargs):
        return [{"id": kwargs["criteria_raw"]}]

    monkeypatch.setattr(main.suggestions, "fetch_suggestions", fetch)
    runtime._refresh_suggestions()
    assert sent[-1]["method"] == "suggestions.set"
    assert sent[-1]["params"] == {"items": [{"id": "review_requested,changes_requested"}]}
    previous = runtime._suggestions_last_good

    def fail(**kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(main.suggestions, "fetch_suggestions", fail)
    runtime._refresh_suggestions()
    assert runtime._suggestions_last_good is previous
    assert sent[-1]["method"] == "suggestions.error"
    assert sent[-1]["params"]["message"] == "offline"


@pytest.mark.parametrize("failed_key", main.SUGGESTION_SETTING_KEYS)
def test_runtime_config_failure_never_fetches_or_replaces_last_good_snapshot(monkeypatch, failed_key):
    sent = []
    calls = []
    runtime = main.Runtime(send=sent.append)
    config_failed = False

    def config(method, params, timeout=10.0):
        result = _host_config(method, params, timeout)
        if config_failed and params["key"] == failed_key:
            return None
        if params["key"] == "suggestion_repositories":
            return {"value": "acme/widgets"}
        return result

    def fetch(**kwargs):
        calls.append(kwargs)
        return [{"id": "scoped-review", "source_ref": "https://github.com/acme/widgets/pull/42"}]

    runtime.call_host = config
    monkeypatch.setattr(main.suggestions, "fetch_suggestions", fetch)
    runtime._refresh_suggestions()
    assert calls[0]["repos_raw"] == "acme/widgets"
    assert sent[-1]["method"] == "suggestions.set"
    previous = runtime._suggestions_last_good
    sent.clear()
    calls.clear()

    config_failed = True
    runtime._refresh_suggestions()
    assert not calls
    assert runtime._suggestions_last_good is previous
    assert [message["method"] for message in sent] == ["suggestions.error"]
    assert failed_key in sent[0]["params"]["message"]


def test_host_refresh_notification_runs_provider_on_main_thread(monkeypatch):
    sent = []
    runtime = main.Runtime(send=sent.append)
    runtime.call_host = _host_config

    def fetch(**kwargs):
        assert kwargs["criteria_raw"] == "review_requested,changes_requested"
        return []

    monkeypatch.setattr(main.suggestions, "fetch_suggestions", fetch)
    runtime.handle_inbound({"jsonrpc": "2.0", "id": 7, "method": "suggestions.refresh"})
    assert runtime._suggestions_refresh_due
    runtime._service_ticks()
    assert not runtime._suggestions_refresh_due
    assert sent[0]["result"] == {"accepted": True}
    assert sent[-1]["method"] == "suggestions.set"
    assert sent[-1]["params"] == {"items": []}


def test_provider_applies_configured_ranking_and_snapshot_limit():
    change = _pr(
        "https://github.com/acme/widgets/pull/43",
        number=43,
        reviewRequests={"nodes": []},
        reviews={"nodes": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit": {"oid": "head"}}]},
        commits={"nodes": [{"commit": {"oid": "head"}}]},
    )
    pulls = [_pr(), change]
    kwargs = {"criteria": {"review_requested", "changes_requested"}, "repos": set(), "orgs": set(), "mapping": {}}
    ranked = suggestions.build_suggestions(pulls, **kwargs, priority=("review_requested", "changes_requested"))
    assert [item["criterion"] for item in ranked] == ["review_requested", "changes_requested"]
    assert len(suggestions.build_suggestions(pulls, **kwargs, limit=1)) == 1


def test_worker_snapshot_and_config_error_round_trip_retains_last_good(monkeypatch, tmp_path):
    """Smoke the worker loop, real host RPCs, and discovery with mocked HTTP only."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    stdin = queue.Queue()
    sent = []
    queries = []
    pr = _pr(
        reviews={
            "nodes": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit": {"oid": "old-head"}}]
        },
        commits={"nodes": [{"commit": {"oid": "head"}}]},
    )

    def graphql(request):
        assert request.url.path == "/graphql"
        queries.append(json.loads(request.content)["variables"]["query"])
        return httpx.Response(200, json={"data": {"search": {"nodes": [pr], "pageInfo": {"hasNextPage": False}}}})

    monkeypatch.setattr(
        suggestions, "GitHubClient", lambda token: GitHubClient(token, transport=httpx.MockTransport(graphql))
    )

    def host(message):
        sent.append(message)
        method = message.get("method")
        if method == "config.get":
            key = message["params"]["key"]
            published = any(item.get("method") == "suggestions.set" for item in sent)
            reply = {"jsonrpc": "2.0", "id": message["id"]}
            if published and key == "suggestion_repositories":
                reply["error"] = {"code": -32000, "message": "configuration unavailable"}
            else:
                values = {"suggestion_repositories": "acme/widgets", "ui_refresh_secs": 0}
                reply["result"] = {"value": values.get(key)}
            stdin.put(json.dumps(reply))
            if key == "ui_refresh_secs":
                stdin.put(json.dumps({"jsonrpc": "2.0", "id": 77, "method": "suggestions.refresh"}))
        elif method == "sessions.list":
            stdin.put(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"sessions": []}}))
        elif method == "suggestions.error":
            stdin.put(None)

    runtime = main.Runtime(send=host, stdin=iter(stdin.get, None))
    runtime.run()

    snapshots = [message["params"]["items"] for message in sent if message.get("method") == "suggestions.set"]
    assert len(snapshots) == 1
    assert len(snapshots[0]) == 1
    assert snapshots[0][0]["criterion"] == "review_requested"
    assert snapshots[0][0]["source_ref"] == "https://github.com/acme/widgets/pull/42"
    assert runtime._suggestions_last_good is snapshots[0]
    assert len(queries) == 2
    assert all("repo:acme/widgets" in query for query in queries)
    assert any(message.get("id") == 77 and message.get("result") == {"accepted": True} for message in sent)
    errors = [message for message in sent if message.get("method") == "suggestions.error"]
    assert len(errors) == 1
    assert "suggestion_repositories" in errors[0]["params"]["message"]

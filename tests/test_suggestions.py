import pytest

from aoe_github_plugin import main
from aoe_github_plugin import suggestions


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
    assert client.variables[0]["query"] == "is:open review:changes_requested repo:acme/widgets"
    assert client.variables[1]["after"] == "page-2"
    assert "review-requested:@me" in client.variables[2]["query"]


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


def test_overlapping_search_results_keep_configured_higher_priority_criterion():
    def discover(priority):
        client = FakeClient(
            [
                {"data": {"search": {"nodes": [_pr()], "pageInfo": {"hasNextPage": False}}}},
                {"data": {"search": {"nodes": [_pr()], "pageInfo": {"hasNextPage": False}}}},
            ]
        )
        results = suggestions.discover_review_requests(
            client,
            criteria={"review_requested", "changes_requested"},
            repos=set(),
            orgs=set(),
            priority=priority,
        )
        return results[0]["_matched_criterion"]

    assert discover(("changes_requested", "review_requested")) == "changes_requested"
    assert discover(("review_requested", "changes_requested")) == "review_requested"


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

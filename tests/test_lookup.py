"""PR identity across real Git checkouts, direct handlers, and web snapshots."""

import json
import subprocess
from collections import Counter

import httpx
import pytest

from aoe_github_plugin import lookup
from aoe_github_plugin import refresh
from aoe_github_plugin import uistate
from aoe_github_plugin import handlers
from aoe_github_plugin.auth import TokenEnvironment


class _Env(TokenEnvironment):
    def __init__(self, token=False):
        self.token = token

    def env_var(self, key):
        return "test-token" if self.token and key == "GITHUB_TOKEN" else None

    def gh_available(self):
        return False


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _checkout(path, origin, upstream=None):
    path.mkdir()
    _git(path, "init", "-q", "-b", "feature")
    _git(
        path,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fixture",
    )
    _git(path, "remote", "add", "origin", f"https://github.com/{origin}.git")
    if upstream:
        _git(path, "remote", "add", "upstream", f"git@github.com:{upstream}.git")
    return path


def _node(destination, head_repo, number, branch="feature"):
    owner, name = head_repo.split("/")
    return {
        "id": f"PR_{destination}_{number}",
        "number": number,
        "title": f"Change {number}",
        "url": f"https://github.com/{destination}/pull/{number}",
        "state": "OPEN",
        "isDraft": False,
        "merged": False,
        "headRefName": branch,
        "headRepository": {"name": name, "owner": {"login": owner}},
        "reviewThreads": {"nodes": [], "totalCount": 0},
    }


def _rest(node):
    return {
        "number": node["number"],
        "html_url": node["url"],
        "title": node["title"],
        "state": "open",
        "draft": node["isDraft"],
        "head": lookup.graphql_head(node),
    }


class _API:
    def __init__(self, nodes, parents=None):
        self.nodes = nodes
        self.parents = parents or {}
        self.requests = []
        self.metadata_status = 200

    def __call__(self, request):
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if request.url.path == "/graphql":
            variables = json.loads(request.content)["variables"]
            destination = f"{variables['owner']}/{variables['repo']}"
            aliases = {}
            for alias, branch in variables.items():
                if not alias.startswith("b"):
                    continue
                nodes = [n for n in self.nodes.get(destination, []) if n["headRefName"] == branch]
                # Rich/digest queries fetch a single page; flag truncation only
                # beyond the query's first:100 cap so tests with fewer nodes see
                # a clean page.
                aliases[alias] = {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": len(nodes) > 100, "endCursor": "101"},
                }
            return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 5000}, "repository": aliases}})
        destination = "/".join(parts[1:3])
        if len(parts) == 3:
            parent = self.parents.get(destination)
            owner, name = parent.split("/") if parent else (None, None)
            return httpx.Response(
                self.metadata_status,
                json={"parent": {"owner": {"login": owner}, "name": name}} if parent else {},
            )
        if parts[-1] == "stacks":
            return httpx.Response(404)
        assert parts[-1] == "pulls"
        if request.headers.get("If-None-Match") == '"stable"':
            return httpx.Response(304)
        # Deliberately include foreign heads: consumers must verify actual identity,
        # even when the server ignores the head filter or returns ambiguous data.
        return httpx.Response(
            200, headers={"ETag": '"stable"'}, json=[_rest(n) for n in self.nodes.get(destination, [])]
        )


@pytest.fixture(autouse=True)
def _caches():
    def reset():
        lookup._parent_cache.clear()
        refresh._etag_cache.clear()
        refresh._graphql_cache.clear()
        refresh._session_refresh_cache.clear()
        refresh._retry_cooldown.clear()
        refresh._stack_cache.clear()
        refresh._stack_unsupported.clear()
        refresh._stack_backoff["until"] = 0.0
        refresh._graphql_budget.update({"remaining": None, "valid_until": None})
        for kind in ("rest", "graphql"):
            refresh._backoff[kind].update({"until": 0.0, "reset_known": False})
        refresh._backoff["notified"] = False

    reset()
    yield
    reset()


def _snapshot(paths, api, token=False):
    sessions = [{"id": str(path), "project_path": str(path)} for path in paths]
    return refresh.build_snapshot(sessions, env=_Env(token), transport=httpx.MockTransport(api))


@pytest.mark.parametrize("token", [False, True])
@pytest.mark.parametrize("explicit_upstream", [False, True])
def test_fork_upstream_status_open_and_web(tmp_path, token, explicit_upstream):
    path = _checkout(tmp_path / "fork", "alice/r", "upstream/r" if explicit_upstream else None)
    nodes = [_node("upstream/r", f"stranger{i}/r", i) for i in range(1, 4)]
    nodes += [
        _node("upstream/r", "alice/another-repo", 6),
        _node("upstream/r", "alice/r", 8),
        _node("upstream/r", "upstream/r", 9),
        _node("upstream/r", "alice/r", 10, branch="different"),
    ]
    api = _API({"upstream/r": nodes}, {"alice/r": "upstream/r"})
    transport = httpx.MockTransport(api)
    status = handlers.github_status(str(path), env=_Env(token), transport=transport)
    assert status["repo"] == "alice/r"
    assert status["branch"] == "feature"
    assert status["error"] is None
    assert {p["number"] for p in status["pulls"]} == {8}
    assert handlers.github_open(str(path), env=_Env(token), transport=transport) == {
        "url": "https://github.com/upstream/r/pull/8",
        "kind": "pull",
    }
    snapshot = _snapshot([path], api, token)
    repo = snapshot["sessions"][0]["repos"][0]
    assert repo["repo"] == "alice/r"
    assert {p["number"]: p["head"]["repo"]["owner"]["login"] for p in repo["pulls"]} == {8: "alice"}
    # Upstream-owned #9 and any third-party same-name branch are rejected: the
    # permitted head is always the checkout's own origin repository.
    assert 9 not in {p["number"] for p in repo["pulls"]}
    assert all(p["url"].startswith("https://github.com/upstream/r/pull/") for p in repo["pulls"])
    metadata = [r for r in api.requests if r.url.path == "/repos/alice/r"]
    assert len(metadata) == (0 if explicit_upstream else 1)
    if token:
        assert any(r.url.path == "/graphql" for r in api.requests)
        assert {r.url.path for r in api.requests if r.url.path.endswith("/stacks")} == {"/repos/upstream/r/stacks"}


@pytest.mark.parametrize("token", [False, True])
def test_same_repo_still_matches_exact_head(tmp_path, token):
    path = _checkout(tmp_path / "repo", "team/r")
    api = _API({"team/r": [_node("team/r", "unrelated/r", 1), _node("team/r", "team/r", 2)]})
    repo = _snapshot([path], api, token)["sessions"][0]["repos"][0]
    assert [p["number"] for p in repo["pulls"]] == [2]
    assert repo["error"] is None
    transport = httpx.MockTransport(api)
    assert [p["number"] for p in handlers.github_status(str(path), env=_Env(), transport=transport)["pulls"]] == [2]
    assert handlers.github_open(str(path), env=_Env(), transport=transport)["url"] == "https://github.com/team/r/pull/2"


@pytest.mark.parametrize("token", [False, True])
def test_two_forks_and_multiple_repos_do_not_share_foreign_heads(tmp_path, token):
    alice = _checkout(tmp_path / "alice", "alice/r")
    bob = _checkout(tmp_path / "bob", "bob/r")
    other = _checkout(tmp_path / "other", "team/other")
    api = _API(
        {
            "upstream/r": [_node("upstream/r", "alice/r", 41), _node("upstream/r", "bob/r", 42)],
            "alice/r": [_node("alice/r", "alice/r", 43)],
            "team/other": [_node("team/other", "team/other", 41)],
        },
        {"alice/r": "upstream/r", "bob/r": "upstream/r"},
    )
    snapshot = _snapshot([alice, bob, other, alice], api, token)
    pulls = [[p["url"] for p in session["repos"][0]["pulls"]] for session in snapshot["sessions"]]
    assert [set(urls) for urls in pulls] == [
        {"https://github.com/alice/r/pull/43", "https://github.com/upstream/r/pull/41"},
        {"https://github.com/upstream/r/pull/42"},
        {"https://github.com/team/other/pull/41"},
        {"https://github.com/alice/r/pull/43", "https://github.com/upstream/r/pull/41"},
    ]
    polls = Counter((r.url.path, r.url.params.get("head")) for r in api.requests if r.url.path.endswith("/pulls"))
    assert polls[("/repos/upstream/r/pulls", "alice:feature")] == 1
    assert polls[("/repos/upstream/r/pulls", "bob:feature")] == 1
    # A subsequent conditional poll must retain the owner-separated cached results.
    again = _snapshot([alice, bob, other], api, token)
    assert [{p["url"] for p in s["repos"][0]["pulls"]} for s in again["sessions"]] == [set(p) for p in pulls[:3]]


def test_parent_failure_cools_down_then_recovers(tmp_path, monkeypatch):
    path = _checkout(tmp_path / "fork", "alice/r")
    api = _API(
        {"alice/r": [_node("alice/r", "alice/r", 1)], "upstream/r": [_node("upstream/r", "alice/r", 2)]},
        {"alice/r": "upstream/r"},
    )
    api.metadata_status = 503
    now = [100.0]
    monkeypatch.setattr(lookup.time, "monotonic", lambda: now[0])
    transport = httpx.MockTransport(api)
    for _ in range(2):
        assert [p["number"] for p in handlers.github_status(str(path), env=_Env(), transport=transport)["pulls"]] == [1]
    assert sum(r.url.path == "/repos/alice/r" for r in api.requests) == 1
    now[0] += lookup.PARENT_RETRY + 1
    api.metadata_status = 200
    assert {p["number"] for p in handlers.github_status(str(path), env=_Env(), transport=transport)["pulls"]} == {1, 2}
    assert sum(r.url.path == "/repos/alice/r" for r in api.requests) == 2


def test_digest_ignores_foreign_head_changes_but_observes_own(tmp_path):
    path = _checkout(tmp_path / "fork", "alice/r", "upstream/r")
    nodes = [_node("upstream/r", "bob/r", 1), _node("upstream/r", "alice/r", 2)]
    api = _API({"upstream/r": nodes})
    _snapshot([path], api, token=True)
    nodes[0]["updatedAt"] = "2026-10-26T10:00:00Z"
    for entry in refresh._graphql_cache.values():
        entry["validated_at"] -= refresh.GRAPHQL_DIGEST_STALE + 1
    before = len(api.requests)
    second = _snapshot([path], api, token=True)
    assert [p["number"] for p in second["sessions"][0]["repos"][0]["pulls"]] == [2]
    assert all(
        "fragment PRConnection" not in json.loads(r.content)["query"]
        for r in api.requests[before:]
        if r.url.path == "/graphql"
    )
    nodes[1]["isDraft"] = True
    for entry in refresh._graphql_cache.values():
        entry["validated_at"] -= refresh.GRAPHQL_DIGEST_STALE + 1
    third = _snapshot([path], api, token=True)
    assert third["sessions"][0]["repos"][0]["pulls"][0]["draft"] is True


def test_web_selection_preserves_equal_numbers_in_fork_and_upstream(tmp_path):
    path = _checkout(tmp_path / "fork", "alice/r", "upstream/r")
    api = _API(
        {
            "alice/r": [_node("alice/r", "alice/r", 7)],
            "upstream/r": [_node("upstream/r", "alice/r", 7)],
        }
    )
    snapshot = _snapshot([path], api)
    session_id = str(path)
    params = uistate.snapshot_ui_state_params(snapshot, selected={session_id: "upstream/r#7"})
    pane = next(p for p in params if p["slot"] == "pane")
    rows = [
        row
        for section in pane["payload"]["blocks"]
        for row in section.get("children", [])
        if row.get("method") == "github.select_pr"
    ]
    assert {row["params"]["pr"]: row["selected"] for row in rows} == {"alice/r#7": False, "upstream/r#7": True}
    assert {row["href"] for row in rows} == {
        "https://github.com/alice/r/pull/7",
        "https://github.com/upstream/r/pull/7",
    }


def test_unavailable_upstream_does_not_hide_existing_origin_pull(tmp_path):
    path = _checkout(tmp_path / "fork", "alice/r", "upstream/r")
    api = _API({"alice/r": [_node("alice/r", "alice/r", 1)]})

    def handler(request):
        if request.url.path.startswith("/repos/upstream/"):
            return httpx.Response(503)
        return api(request)

    transport = httpx.MockTransport(handler)
    status = handlers.github_status(str(path), env=_Env(), transport=transport)
    assert [p["number"] for p in status["pulls"]] == [1]
    assert status["error"] is None
    assert handlers.github_open(str(path), env=_Env(), transport=transport) == {
        "url": "https://github.com/alice/r/pull/1",
        "kind": "pull",
    }
    snapshot = refresh.build_snapshot([{"id": "s", "project_path": str(path)}], env=_Env(), transport=transport)
    repo = snapshot["sessions"][0]["repos"][0]
    assert [p["number"] for p in repo["pulls"]] == [1]


def test_truncated_page_serves_rest_without_cooldown_on_busy_branch(tmp_path):
    # A popular branch (e.g. same-repo main) with more foreign same-name PRs
    # than one page can prove an absence; the owner-scoped REST probe is the
    # trusted source, and a collision must not arm the per-key cooldown or
    # re-query in a loop.
    path = _checkout(tmp_path / "repo", "team/r")
    team_pull = _node("team/r", "team/r", 2)
    foreign = [_node("team/r", f"stranger{i}/r", i + 10) for i in range(105)]
    api = _API({"team/r": [*foreign, team_pull]})
    graphql_queries = []
    served_pages = []

    def handler(request):
        if request.url.path == "/graphql":
            variables = json.loads(request.content)["variables"]
            graphql_queries.append(variables)
            destination = f"{variables['owner']}/{variables['repo']}"
            matching = [n for n in api.nodes.get(destination, []) if n["headRefName"] == variables["b0"]]
            # Real single-page semantics: cap the page and let the server flag
            # truncation only because the branch has more than first:100 nodes.
            page = matching[:100]
            served_pages.append(page)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "rateLimit": {"remaining": 5000, "resetAt": "x"},
                        "repository": {
                            "b0": {
                                "nodes": page,
                                "pageInfo": {"hasNextPage": len(matching) > len(page)},
                            }
                        },
                    }
                },
            )
        return api(request)

    snapshot = refresh.build_snapshot(
        [{"id": "s", "project_path": str(path)}], env=_Env(token=True), transport=httpx.MockTransport(handler)
    )
    repo = snapshot["sessions"][0]["repos"][0]
    # The fixture must genuinely be a full, truncated page, not a small page
    # with a hand-set flag.
    assert len(served_pages) == 1
    assert len(served_pages[0]) == 100
    # The owned PR survives through the owner-scoped REST fallback, not an empty
    # rich page, and only one GraphQL query is spent.
    assert [p["number"] for p in repo["pulls"]] == [2]
    assert len(graphql_queries) == 1
    assert refresh._graphql_cache == {}  # nothing cached as a confirmed result
    assert refresh._retry_cooldown == {}  # a collision is not a failure
    # The truncation is remembered so the next tick cannot re-spend a full query
    # that is certain to truncate identically.
    assert refresh._truncated_until != {}

    # Second tick: no new GraphQL query, and the same owned PR is still shown.
    second = refresh.build_snapshot(
        [{"id": "s", "project_path": str(path)}], env=_Env(token=True), transport=httpx.MockTransport(handler)
    )
    assert len(graphql_queries) == 1  # unchanged: the full query was skipped
    assert [p["number"] for p in second["sessions"][0]["repos"][0]["pulls"]] == [2]

    # A forced refresh is the user asking for a live attempt, so it spends one.
    refresh.build_snapshot(
        [{"id": "s", "project_path": str(path)}],
        env=_Env(token=True),
        transport=httpx.MockTransport(handler),
        force=True,
    )
    assert len(graphql_queries) == 2


def test_origin_empty_upstream_error_keeps_status_failsoft(tmp_path):
    path = _checkout(tmp_path / "fork", "alice/r", "upstream/r")
    api = _API({})  # no open origin PRs for feature

    def handler(request):
        if request.url.path.startswith("/repos/upstream/"):
            return httpx.Response(403, json={"message": "Forbidden"})
        return api(request)

    transport = httpx.MockTransport(handler)
    status = handlers.github_status(str(path), env=_Env(), transport=transport)
    assert status["pulls"] == []
    assert status["error"] is None  # fail-soft contract preserved
    assert "no open PR" in status["summary"]
    repo = refresh.build_snapshot([{"id": "s", "project_path": str(path)}], env=_Env(), transport=transport)[
        "sessions"
    ][0]["repos"][0]
    assert repo["pulls"] == []
    assert repo["error"] is None
    assert repo.get("freshness") is None or repo["freshness"]["stale"] is False

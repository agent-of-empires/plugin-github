"""Multi-session refresh: discovery, aggregation, dedup, ETag, backoff.

Discovery uses real temp git repos (cheap); GitHub lookups use a MockTransport.
"""

import json
import time
import logging
import threading
import subprocess
from datetime import datetime
from datetime import timezone
from datetime import timedelta

import httpx
import pytest

from aoe_github_plugin import refresh
from aoe_github_plugin.auth import TokenEnvironment


class _Env(TokenEnvironment):
    """Resolves a fixed token via env, never touching gh (rich GraphQL path)."""

    def env_var(self, key):
        return "tok" if key == "GITHUB_TOKEN" else None

    def gh_available(self):
        return False

    def gh_auth_token(self):
        raise AssertionError("gh must not be consulted")


class _NoToken(TokenEnvironment):
    """No token anywhere: exercises the REST open-PR fallback path."""

    def env_var(self, key):
        return None

    def gh_available(self):
        return False

    def gh_auth_token(self):
        raise AssertionError("gh must not be consulted")


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _make_repo(path, remote="https://github.com/o/r.git", branch="feature"):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    _git(path, "checkout", "-q", "-b", branch)
    _git(path, "remote", "add", "origin", remote)


def _make_submodule(parent, name):
    source = parent.parent / f"{name}_source"
    _make_repo(source, remote=f"https://github.com/o/{name}.git")
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(source), name)
    return parent / name


@pytest.fixture(autouse=True)
def _clear_cache():
    def _reset():
        refresh._etag_cache.clear()
        refresh._graphql_cache.clear()
        refresh._session_refresh_cache.clear()
        refresh._backoff["rest"].update({"until": 0.0, "reset_known": False})
        refresh._backoff["graphql"].update({"until": 0.0, "reset_known": False})
        refresh._backoff["notified"] = False
        refresh._graphql_budget.update({"remaining": None, "valid_until": None})

    _reset()
    yield
    _reset()


def test_discovery_finds_child_checkouts_not_plain_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "repo_a")
    (ws / "not_a_repo").mkdir()
    found = {p.rsplit("/", 1)[-1] for p in refresh.discover_checkouts(str(ws))}
    assert found == {"repo_a"}


def test_discovery_workspace_root_repo_does_not_report_children(tmp_path):
    # If the workspace root is itself one repo, its subdirs are part of it, not
    # separate checkouts.
    ws = tmp_path / "single"
    _make_repo(ws)
    (ws / "src").mkdir()
    found = list(refresh.discover_checkouts(str(ws)))
    assert found == [str(ws)]


def test_discovery_ignores_child_submodules_when_enabled(tmp_path):
    ws = tmp_path / "ws"
    _make_repo(ws)
    submodule = _make_submodule(ws, "dep")
    found = set(refresh.discover_checkouts(str(ws), ignore_submodules=True))
    assert found == {str(ws)}
    found_with_submodules = set(refresh.discover_checkouts(str(ws), ignore_submodules=False))
    assert found_with_submodules == {str(ws), str(submodule)}


def test_discovery_keeps_normal_child_checkouts_when_ignoring_submodules(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "repo_a")
    found = {p.rsplit("/", 1)[-1] for p in refresh.discover_checkouts(str(ws), ignore_submodules=True)}
    assert found == {"repo_a"}


def test_discovery_keeps_workspace_root_submodule(tmp_path):
    superproject = tmp_path / "super"
    _make_repo(superproject)
    submodule = _make_submodule(superproject, "ws")
    assert refresh.discover_checkouts(str(submodule), ignore_submodules=True) == [str(submodule)]


def _transport(pulls, etag='W/"v1"', capture=None):
    def handler(request):
        if capture is not None:
            capture.append(request)
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": etag}, json=pulls)

    return httpx.MockTransport(handler)


def _pull(number=12, draft=False):
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "title": "t",
        "state": "open",
        "draft": draft,
    }


def test_build_snapshot_aggregates_a_session(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "repo_a")
    sessions = [{"id": "s1", "title": "sess", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()]))
    assert len(snap["sessions"]) == 1
    repos = snap["sessions"][0]["repos"]
    assert len(repos) == 1
    assert repos[0]["repo"] == "o/r"
    assert repos[0]["pulls"][0]["number"] == 12
    assert repos[0]["error"] is None


def test_dedup_fetches_a_shared_key_once(tmp_path):
    # Two sessions, each a workspace with the same origin+branch -> one fetch.
    ws1 = tmp_path / "w1"
    ws2 = tmp_path / "w2"
    ws1.mkdir()
    ws2.mkdir()
    _make_repo(ws1 / "r")
    _make_repo(ws2 / "r")
    captured = []
    sessions = [
        {"id": "s1", "project_path": str(ws1)},
        {"id": "s2", "project_path": str(ws2)},
    ]
    refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()], capture=captured))
    assert len(captured) == 1  # (o, r, feature) fetched once, not twice


def test_etag_304_reuses_cached_pulls(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    transport = _transport([_pull(number=99)])
    first = refresh.build_snapshot(sessions, env=_NoToken(), transport=transport)
    assert first["sessions"][0]["repos"][0]["pulls"][0]["number"] == 99
    # Second refresh: server answers 304 (cache hit); pulls come from cache.
    second = refresh.build_snapshot(sessions, env=_NoToken(), transport=transport)
    assert second["sessions"][0]["repos"][0]["pulls"][0]["number"] == 99


def test_backoff_serves_cached_and_skips_http(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Seed the cache, then trip backoff: the next refresh must not hit HTTP.
    refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull(number=5)]))
    refresh._backoff["rest"]["until"] = time.monotonic() + 60
    captured = []

    def boom(request):
        captured.append(request)
        raise AssertionError("HTTP must not be called during backoff")

    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=httpx.MockTransport(boom))
    assert captured == []
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 5


def test_non_github_checkout_is_benign(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "local", remote="git@gitlab.com:o/r.git")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([]))
    repo = snap["sessions"][0]["repos"][0]
    assert repo["repo"] is None
    assert repo["error"] is None
    assert repo["pulls"] == []


def test_no_token_marks_auth_absent(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()]))
    assert snap["auth"]["present"] is False


def test_successful_basic_refresh_records_freshness(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: "2026-06-29T14:32:10Z")

    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()]))

    assert snap["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:32:10Z",
        "stale": False,
    }


def test_unchanged_basic_refresh_advances_freshness(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    transport = _transport([_pull(number=99)])

    refresh.build_snapshot(sessions, env=_NoToken(), transport=transport)
    second = refresh.build_snapshot(sessions, env=_NoToken(), transport=transport)

    assert second["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:05:00Z",
        "stale": False,
    }


def test_backoff_cached_basic_refresh_keeps_prior_freshness(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))

    refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull(number=5)]))
    refresh._backoff["rest"]["until"] = time.monotonic() + 60
    snap = refresh.build_snapshot(
        sessions, env=_NoToken(), transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )

    assert snap["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:00:00Z",
        "stale": True,
    }


# --- rich GraphQL path (token present) ---


def _gql_node(number=7, state="OPEN", merged=False, draft=False, decision="APPROVED"):
    return {
        "id": f"PR_{number}",
        "number": number,
        "title": "t",
        "url": f"https://github.com/o/r/pull/{number}",
        "state": state,
        "isDraft": draft,
        "merged": merged,
        "reviewDecision": decision,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "state": "SUCCESS",
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "test",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS",
                                        "detailsUrl": "https://ci/test",
                                    }
                                ]
                            },
                        }
                    }
                }
            ]
        },
        "reviews": {"nodes": []},
        "reviewThreads": {
            "nodes": [
                {
                    "isResolved": False,
                    "path": "a.py",
                    "line": 3,
                    "comments": {"nodes": [{"author": {"login": "al"}, "bodyText": "fix this", "url": "https://c/1"}]},
                }
            ]
        },
    }


def _alias_repo(request, by_branch):
    """Build the batched-query ``repository`` from the posted variables: one
    aliased connection (``b0``/``b1``/...) per requested branch. ``by_branch`` maps
    a branch name to its PR nodes; a missing branch yields an empty connection."""
    variables = json.loads(request.content.decode()).get("variables", {})
    repo = {}
    for alias, branch in variables.items():
        if alias.startswith("b"):
            repo[alias] = {"nodes": by_branch.get(branch, [])}
    return repo


def _rich_transport(nodes, *, etag='W/"v1"', remaining=5000, errors=None, gql=None, branch="feature"):  # noqa: PLR0913
    """Combined transport for the token path: a REST conditional probe (GET) then
    a batched GraphQL query (POST). The REST probe answers 304 when the caller
    already holds ``etag`` (no change), else 200 with a one-PR list (changed). The
    GraphQL response aliases ``nodes`` under whichever branch(es) the query asked
    for. ``gql`` is an optional capture list for the GraphQL requests."""

    def handler(request):
        if str(request.url).endswith("/graphql"):
            if gql is not None:
                gql.append(request)
            if errors is not None:
                return httpx.Response(200, json={"errors": errors})
            body = {
                "data": {
                    "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": "x"},
                    "repository": _alias_repo(request, {branch: nodes}),
                }
            }
            return httpx.Response(200, json=body)
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": etag}, json=[_pull()])

    return httpx.MockTransport(handler)


def test_graphql_path_parses_rich_fields(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node()]))
    assert snap["auth"]["present"] is True
    pull = snap["sessions"][0]["repos"][0]["pulls"][0]
    assert pull["review_state"] == "approved"
    assert pull["checks"]["state"] == "succeeded"
    assert pull["checks"]["runs"][0] == {"name": "test", "state": "succeeded", "url": "https://ci/test"}
    assert pull["comments"]["unresolved"] == 1
    assert pull["comments"]["items"][0]["author"] == "al"


def test_forced_rich_refresh_advances_freshness_when_data_unchanged(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    transport = _rich_transport([_gql_node(number=7)])

    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    second = refresh.build_snapshot(sessions, env=_Env(), transport=transport, force=True)

    assert second["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:05:00Z",
        "stale": False,
    }


def test_unchanged_rich_refresh_advances_freshness(tmp_path, monkeypatch):
    # A background (non-forced) tick whose REST probe returns 304 confirms the
    # cached rich data is current, so the freshness stamp must advance. Before
    # the fix it only advanced on a forced refresh and looked frozen here.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    transport = _rich_transport([_gql_node(number=7)])

    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    second = refresh.build_snapshot(sessions, env=_Env(), transport=transport)

    assert second["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:05:00Z",
        "stale": False,
    }


def test_backoff_cached_rich_refresh_keeps_prior_freshness(tmp_path, monkeypatch):
    # A backoff-served rich cache was never validated against GitHub, so it must
    # keep the prior stamp and read as stale, not advance like a real 304.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    transport = _rich_transport([_gql_node(number=7)])

    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    refresh._backoff["rest"]["until"] = time.monotonic() + 60
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)

    assert snap["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:00:00Z",
        "stale": True,
    }


def test_session_freshness_advances_on_confirmed_current_304():
    fingerprint = (("o", "r", "feature"),)
    results = [{"pulls": [{"number": 1}], "_fresh": False, "_stale": False}]
    out = refresh._session_freshness("s1", fingerprint, results, "2026-06-29T14:05:00Z")
    assert out == {"refreshed_at": "2026-06-29T14:05:00Z", "stale": False}


def test_session_freshness_advances_on_confirmed_current_zero_prs():
    # A repo with zero open PRs, confirmed current via 304 (_fresh=False on the
    # rich-cache branch, _stale=False). The dropped not-pulls heuristic used to
    # wrongly mark this stale and freeze the stamp.
    fingerprint = (("o", "r", "feature"),)
    results = [{"pulls": [], "_fresh": False, "_stale": False}]
    out = refresh._session_freshness("s1", fingerprint, results, "2026-06-29T14:05:00Z")
    assert out == {"refreshed_at": "2026-06-29T14:05:00Z", "stale": False}


def test_session_freshness_keeps_prior_when_stale():
    fingerprint = (("o", "r", "feature"),)
    refresh._session_refresh_cache["s1"] = {"fingerprint": fingerprint, "refreshed_at": "2026-06-29T14:00:00Z"}
    results = [{"pulls": [{"number": 1}], "_fresh": False, "_stale": True}]
    out = refresh._session_freshness("s1", fingerprint, results, "2026-06-29T14:05:00Z")
    assert out == {"refreshed_at": "2026-06-29T14:00:00Z", "stale": True}


def test_graphql_merged_pull_flagged(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(
        sessions, env=_Env(), transport=_rich_transport([_gql_node(state="MERGED", merged=True)])
    )
    pull = snap["sessions"][0]["repos"][0]["pulls"][0]
    assert pull["merged"] is True
    assert pull["state"] == "MERGED"


def _expire_graphql_cache():
    """Age every cached GraphQL entry past the DIGEST ceiling (but not the hard
    full ceiling) so the next refresh revalidates even on a REST 304."""
    for entry in refresh._graphql_cache.values():
        entry["fetched_at"] -= refresh.GRAPHQL_DIGEST_STALE + 1
        entry["validated_at"] -= refresh.GRAPHQL_DIGEST_STALE + 1


def test_rest_304_with_fresh_cache_skips_graphql(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)
    # First load: REST reports a change (no cached etag), so GraphQL fires once.
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    # Steady state: REST answers 304 and the rich cache is fresh, so NO GraphQL.
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 7


def test_rest_change_triggers_graphql(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=7)]))
    # A changed PR list (different etag -> REST 200) re-fires GraphQL on the next tick.
    gql = []
    snap = refresh.build_snapshot(
        sessions, env=_Env(), transport=_rich_transport([_gql_node(number=8)], etag='W/"v2"', gql=gql)
    )
    assert len(gql) == 1
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 8


def test_stale_cache_triggers_graphql_on_304(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=7)]))
    _expire_graphql_cache()
    # REST still answers 304 (unchanged list), but the rich cache is past the
    # digest ceiling and the data really changed (number 7 -> 9): the cheap
    # digest detects the mismatch and the full query fires in the same refresh.
    gql = []
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=9)], gql=gql))
    assert [_is_full_query(r) for r in gql] == [False, True]  # digest first, then rich
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 9


def test_force_refetches_graphql_on_304(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=7)]))
    # A user-clicked refresh (force=True) re-queries even when REST reports 304
    # and the cache is fresh, surfacing live data over the cached pull.
    gql = []
    snap = refresh.build_snapshot(
        sessions, env=_Env(), transport=_rich_transport([_gql_node(number=8)], gql=gql), force=True
    )
    assert len(gql) == 1
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 8


def test_required_check_mode_uses_separate_rich_cache(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    node = _gql_node(number=7)
    node["baseRef"] = {
        "branchProtectionRule": {
            "requiresStatusChecks": True,
            "requiredStatusCheckContexts": ["test"],
            "requiredStatusChecks": [],
        }
    }
    node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
        "state": "FAILURE",
        "contexts": {
            "nodes": [
                {
                    "__typename": "CheckRun",
                    "name": "optional-lint",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://ci/lint",
                },
                {
                    "__typename": "CheckRun",
                    "name": "test",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://ci/test",
                },
            ]
        },
    }
    gql = []
    transport = _rich_transport([node], gql=gql)
    default_snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    assert default_snap["sessions"][0]["repos"][0]["pulls"][0]["checks"]["state"] == "failing"
    required_snap = refresh.build_snapshot(
        sessions,
        env=_Env(),
        transport=transport,
        settings=refresh.SnapshotSettings(required_checks_only=True),
    )
    assert len(gql) == 2
    assert required_snap["sessions"][0]["repos"][0]["pulls"][0]["checks"]["state"] == "succeeded"


def test_graphql_rate_limit_serves_stale(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Seed the cache with a good result, then age it past the ceiling.
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=42)]))
    _expire_graphql_cache()
    # Next refresh hits a GraphQL secondary rate limit: serve the cached pull and arm backoff.
    snap = refresh.build_snapshot(
        sessions,
        env=_Env(),
        transport=_rich_transport([], errors=[{"type": "RATE_LIMITED", "message": "slow down"}]),
    )
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 42
    assert refresh._backoff["graphql"]["until"] > time.monotonic()


def test_graphql_failure_falls_back_to_basic_pulls(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # First load (no rich cache): REST returns a basic pull, GraphQL errors out.
    # The basic REST pull is served rather than blanking the repo.
    snap = refresh.build_snapshot(
        sessions,
        env=_Env(),
        transport=_rich_transport([], errors=[{"message": "boom"}]),
    )
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 12


def test_graphql_keeps_partial_data_with_rate_limit_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]

    # A GraphQL 200 carrying BOTH repository data and a RATE_LIMITED error: keep
    # the data, but still arm the backoff for the next refresh.
    def handler(request):
        if not str(request.url).endswith("/graphql"):
            return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])
        body = {
            "data": {
                "rateLimit": {"cost": 1, "remaining": 10, "resetAt": "x"},
                "repository": _alias_repo(request, {"feature": [_gql_node(number=99)]}),
            },
            "errors": [{"type": "RATE_LIMITED", "message": "slow down"}],
        }
        return httpx.Response(200, json=body)

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler))
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 99
    assert refresh._backoff["graphql"]["until"] > time.monotonic()


# --- rate-limit notice (issue #20) ---


def test_forced_refresh_during_backoff_emits_one_notice(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z", "2026-06-29T14:06:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=42)]))
    _expire_graphql_cache()
    # A GraphQL secondary rate limit on a forced refresh: serve stale + announce.
    rl = _rich_transport([], errors=[{"type": "RATE_LIMITED", "message": "slow down"}])
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=rl, force=True)
    notice = snap.get("rate_limit_notice")
    assert notice is not None
    assert notice["reset_known"] is False  # errors-only response carries no resetAt
    assert snap["sessions"][0]["freshness"] == {
        "refreshed_at": "2026-06-29T14:00:00Z",
        "stale": True,
    }
    # A second forced refresh in the same window must not re-announce.
    snap2 = refresh.build_snapshot(sessions, env=_Env(), transport=rl, force=True)
    assert "rate_limit_notice" not in snap2


def test_background_refresh_never_emits_notice(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Block BOTH budgets so no HTTP fires (a graphql-only backoff would leave the
    # REST probe free to run, #62).
    refresh._backoff["rest"].update({"until": time.monotonic() + 300, "reset_known": True})
    refresh._backoff["graphql"].update({"until": time.monotonic() + 300, "reset_known": True})
    refresh._backoff["notified"] = False

    def boom(request):
        raise AssertionError("HTTP must not be called during backoff")

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(boom), force=False)
    assert "rate_limit_notice" not in snap


def test_known_reset_marks_notice_reset_known(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    reset_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

    # 200 with data but a near-spent budget and a real resetAt: arms a known reset.
    def handler(request):
        if not str(request.url).endswith("/graphql"):
            return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])
        body = {
            "data": {
                "rateLimit": {"remaining": 10, "resetAt": reset_at},
                "repository": _alias_repo(request, {"feature": [_gql_node(number=7)]}),
            }
        }
        return httpx.Response(200, json=body)

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler), force=True)
    notice = snap.get("rate_limit_notice")
    assert notice is not None
    assert notice["reset_known"] is True
    assert notice["seconds"] > 0


# --- cross-key GraphQL batching (#25) ---


def _batched_handler(by_branch, *, gql=None, etag='W/"v1"'):
    """Token-path transport whose GraphQL response aliases per-branch nodes from
    ``by_branch`` (keyed by branch name), so several same-repo branches resolve
    from one batched query."""

    def handler(request):
        if str(request.url).endswith("/graphql"):
            if gql is not None:
                gql.append(request)
            body = {
                "data": {
                    "rateLimit": {"cost": 1, "remaining": 5000, "resetAt": "x"},
                    "repository": _alias_repo(request, by_branch),
                }
            }
            return httpx.Response(200, json=body)
        if request.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": etag}, json=[_pull()])

    return httpx.MockTransport(handler)


def test_same_repo_branches_share_one_graphql_query(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "a", branch="b1")
    _make_repo(ws / "b", branch="b2")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _batched_handler({"b1": [_gql_node(number=1)], "b2": [_gql_node(number=2)]}, gql=gql)
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    # Two branches of one repo collapse into ONE batched GraphQL query, not two,
    # and each alias normalizes back to its own branch.
    assert len(gql) == 1
    nums = sorted(r["pulls"][0]["number"] for r in snap["sessions"][0]["repos"] if r["pulls"])
    assert nums == [1, 2]


def test_partial_alias_failure_isolates_to_its_key(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "a", branch="b1")
    _make_repo(ws / "b", branch="b2")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Seed both branches' rich cache (numbers 1 and 2).
    refresh.build_snapshot(
        sessions, env=_Env(), transport=_batched_handler({"b1": [_gql_node(number=1)], "b2": [_gql_node(number=2)]})
    )

    # Forced refresh where the b2-branch alias (b1, by sorted order) errors via
    # errors[].path while the b1-branch alias (b0) returns fresh data. The REST
    # probe answers 304 (open-PR list unchanged), so the failed alias keeps its
    # stale RICH cache rather than dropping to the basic pulls (#33 only prefers
    # basic when REST reports a change).
    def handler(request):
        if str(request.url).endswith("/graphql"):
            body = {
                "data": {
                    "rateLimit": {"remaining": 5000, "resetAt": "x"},
                    "repository": {"b0": {"nodes": [_gql_node(number=11)]}, "b1": None},
                },
                "errors": [{"type": "SERVICE", "message": "boom", "path": ["repository", "b1"]}],
            }
            return httpx.Response(200, json=body)
        if request.headers.get("If-None-Match") == 'W/"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler), force=True)
    by_branch = {r["branch"]: r for r in snap["sessions"][0]["repos"]}
    assert by_branch["b1"]["pulls"][0]["number"] == 11  # good alias refreshed
    assert by_branch["b2"]["pulls"][0]["number"] == 2  # failed alias kept its stale cache, isolated


def test_many_branches_chunk_into_multiple_queries(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    count = refresh.MAX_GRAPHQL_ALIASES + 2
    for i in range(count):
        _make_repo(ws / f"r{i}", branch=f"feat{i}")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    refresh.build_snapshot(sessions, env=_Env(), transport=_batched_handler({}, gql=gql))
    # More branches than the alias cap split across multiple serial queries.
    assert len(gql) == 2


# --- state-aware staleness (#26) ---


def _running_node(number=7):
    node = _gql_node(number=number, decision=None)
    node["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
        "state": "PENDING",
        "contexts": {
            "nodes": [
                {"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS", "conclusion": None, "detailsUrl": "u"}
            ]
        },
    }
    return node


def _age_cache(seconds):
    for entry in refresh._graphql_cache.values():
        entry["fetched_at"] -= seconds
        entry["validated_at"] -= seconds


def test_active_ci_refreshes_before_the_300s_ceiling(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_running_node(7)], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    # Past the short active ceiling but far under 300s; REST still 304. A running
    # check means the cache is active, so GraphQL re-fires to catch the CI result.
    _age_cache(refresh.GRAPHQL_ACTIVE_DIGEST_STALE + 1)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 2
    assert not _is_full_query(gql[-1])  # active-ceiling revalidation is the cheap digest, not the rich query


def test_terminal_state_holds_cache_until_the_300s_ceiling(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)  # SUCCESS check, approved -> terminal
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    # Same age, but a terminal cache is not active, so the short ceiling does not
    # apply and a 304 holds the cache (no GraphQL) until the 300s ceiling.
    _age_cache(refresh.GRAPHQL_ACTIVE_DIGEST_STALE + 1)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1


def test_waiting_review_is_not_treated_as_active(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    node = _gql_node(number=7, decision=None)
    node["commits"]["nodes"] = []  # no checks -> checks is None
    node["reviewThreads"]["nodes"] = []  # no unresolved threads -> review_state waiting
    gql = []
    transport = _rich_transport([node], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    # Awaiting review (no running CI) is NOT active: it must not burn a GraphQL
    # query every tick, so the short ceiling does not apply.
    _age_cache(refresh.GRAPHQL_ACTIVE_DIGEST_STALE + 1)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1


# --- reviewThreads pagination (#28) ---


def _thread(path, body, resolved=False):
    return {
        "isResolved": resolved,
        "path": path,
        "line": 1,
        "comments": {"nodes": [{"author": {"login": "al"}, "bodyText": body, "url": f"https://c/{path}"}]},
    }


def test_review_threads_paginate_beyond_the_first_page(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    base = _gql_node(number=7)
    base["reviewThreads"] = {
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_thread("a.py", "first")],
    }

    def handler(request):
        if str(request.url).endswith("/graphql"):
            query = json.loads(request.content.decode())["query"]
            if "$id: ID!" in query:  # follow-up page for one PR
                page = {
                    "data": {
                        "rateLimit": {"remaining": 5000, "resetAt": "x"},
                        "node": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [_thread("b.py", "second")],
                            }
                        },
                    }
                }
                return httpx.Response(200, json=page)
            body = {
                "data": {
                    "rateLimit": {"remaining": 5000, "resetAt": "x"},
                    "repository": _alias_repo(request, {"feature": [base]}),
                }
            }
            return httpx.Response(200, json=body)
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler))
    comments = snap["sessions"][0]["repos"][0]["pulls"][0]["comments"]
    # Both pages' unresolved comments surface, not just the first page.
    assert comments["unresolved"] == 2
    assert [c["path"] for c in comments["items"]] == ["a.py", "b.py"]


def test_thread_pagination_stops_and_backs_off_on_rate_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    base = _gql_node(number=7)
    base["reviewThreads"] = {
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [_thread("a.py", "first")],
    }

    def handler(request):
        if str(request.url).endswith("/graphql"):
            query = json.loads(request.content.decode())["query"]
            if "$id: ID!" in query:  # follow-up page hits a secondary rate limit
                return httpx.Response(200, json={"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]})
            body = {
                "data": {
                    "rateLimit": {"remaining": 5000, "resetAt": "x"},
                    "repository": _alias_repo(request, {"feature": [base]}),
                }
            }
            return httpx.Response(200, json=body)
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler))
    comments = snap["sessions"][0]["repos"][0]["pulls"][0]["comments"]
    # The first page survives (never blanked) and the rate limit arms the backoff.
    assert comments["unresolved"] == 1
    assert [c["path"] for c in comments["items"]] == ["a.py"]
    assert refresh._backoff["graphql"]["until"] > time.monotonic()


def test_graphql_backoff_does_not_block_rest_probe(tmp_path):
    # The core of #62: with only the GraphQL budget exhausted, the cheap REST
    # probe must still run and discover the branch's open PR. GraphQL must not be
    # contacted (its gate is armed), and the REST gate must stay clear.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh._backoff["graphql"]["until"] = time.monotonic() + 300

    def handler(request):
        if str(request.url).endswith("/graphql"):
            raise AssertionError("GraphQL must not be called while its budget is backed off")
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull(number=77)])

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(handler))
    # The PR is discovered via REST (basic shape) rather than hidden as "no open PR".
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 77
    assert refresh._backoff["rest"]["until"] <= time.monotonic()  # REST budget never gated


def test_changed_rest_overrides_stale_empty_rich_cache(tmp_path):
    # #33 / #62 residual: a branch whose rich cache is EMPTY (GraphQL previously
    # saw no PR) gets a PR opened while GraphQL is rate-limited. The REST probe
    # reports the list changed, so the fresh basic pulls win over the stale empty
    # rich cache instead of the pane showing "no open PR".
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Seed: GraphQL returns no nodes -> rich cache is [] for this branch.
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([]))
    # Now a PR exists: REST returns it under a NEW etag (changed), GraphQL is rate
    # limited. Forced so the staleness gate does not short-circuit the fetch.
    rl = _rich_transport([], errors=[{"type": "RATE_LIMITED", "message": "slow down"}], etag='W/"v2"')
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=rl, force=True)
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 12


def test_snapshot_reports_active_rate_limit_regardless_of_force(tmp_path):
    # #62 surfacing: build_snapshot carries the current backoff state so the pane
    # can show a note on a background (force=False) refresh, not only when forced.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh._backoff["graphql"]["until"] = time.monotonic() + 300

    snap = refresh.build_snapshot(
        sessions, env=_Env(), transport=httpx.MockTransport(lambda _r: httpx.Response(304)), force=False
    )
    assert snap["rate_limit"]["budget"] == "graphql"


def test_snapshot_omits_rate_limit_when_clear(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()]))
    assert "rate_limit" not in snap


# --- snapshot persistence for instant repaint on restart (#63) ---


def test_save_load_snapshot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    snap = {"sessions": [{"session_id": "s1", "repos": []}], "auth": {"present": True}}
    refresh.save_snapshot(snap)
    assert refresh._snapshot_cache_path().parent == tmp_path / "agent-of-empires" / "github-plugin"
    assert refresh.load_snapshot() == snap


def test_save_snapshot_skips_empty_sessions(tmp_path, monkeypatch):
    # A transient "no sessions" blip must not erase a useful cache.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    refresh.save_snapshot({"sessions": [{"session_id": "s1", "repos": []}]})
    refresh.save_snapshot({"sessions": []})
    assert refresh.load_snapshot()["sessions"] == [{"session_id": "s1", "repos": []}]


def test_load_snapshot_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert refresh.load_snapshot() is None


def test_load_snapshot_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = refresh._snapshot_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert refresh.load_snapshot() is None


def test_load_snapshot_wrong_shape_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = refresh._snapshot_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessions": "nope"}), encoding="utf-8")
    assert refresh.load_snapshot() is None


# --- two-tier digest refresh (#69) ---


def _is_full_query(request):
    """The rich query carries the PRConnection fragment; the digest does not."""
    return b"PRConnection" in request.content


def test_expired_ceiling_with_unchanged_data_sends_no_full_query(tmp_path):
    # The #69 reproduce: a background tick past the freshness ceiling where
    # GitHub reports nothing changed must NOT spend a full-cost rich query.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1  # cold fill is the full query
    _expire_graphql_cache()
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    full = [r for r in gql[1:] if _is_full_query(r)]
    assert full == []  # unchanged data revalidates via the cheap digest only
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 7


def test_digest_match_advances_freshness_and_rearms_window(tmp_path, monkeypatch):
    # A digest match is a revalidation: the freshness stamp advances and the
    # digest window re-arms, so the immediately following tick sends nothing.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    times = iter(["2026-06-29T14:00:00Z", "2026-06-29T14:05:00Z", "2026-06-29T14:06:00Z"])
    monkeypatch.setattr(refresh, "_utc_now_iso", lambda: next(times))
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    _expire_graphql_cache()
    second = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 2  # cold full + one digest
    assert second["sessions"][0]["freshness"] == {"refreshed_at": "2026-06-29T14:05:00Z", "stale": False}
    third = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 2  # window re-armed: no GraphQL at all
    assert third["sessions"][0]["freshness"] == {"refreshed_at": "2026-06-29T14:06:00Z", "stale": False}


def test_full_ceiling_bypasses_digest(tmp_path):
    # Past the HARD full ceiling the rich query fires directly, no digest probe:
    # the digest cannot see everything the pane renders (comment edits, per-check
    # churn under an unchanged rollup), so its false negatives must be bounded.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    for entry in refresh._graphql_cache.values():
        entry["fetched_at"] -= refresh.GRAPHQL_FULL_MAX_STALE + 1
        entry["validated_at"] -= refresh.GRAPHQL_FULL_MAX_STALE + 1
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert [_is_full_query(r) for r in gql] == [True, True]


def test_digest_validation_does_not_reset_full_ceiling(tmp_path):
    # Digest matches keep bumping validated_at, but fetched_at only moves on a
    # real full query; once the hard ceiling passes, the full query fires even
    # though every digest matched.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    _expire_graphql_cache()
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)  # digest match
    assert [_is_full_query(r) for r in gql] == [True, False]
    for entry in refresh._graphql_cache.values():
        entry["fetched_at"] -= refresh.GRAPHQL_FULL_MAX_STALE + 1  # only the FULL clock ages
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert [_is_full_query(r) for r in gql] == [True, False, True]


def test_failed_full_after_digest_mismatch_cools_down(tmp_path):
    # The #69 anti-burn-loop: digest mismatch promotes to a full query; the full
    # query fails with a non-rate error; the next tick must serve stale instead
    # of re-spending digest + full every tick until the cooldown passes.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    refresh.build_snapshot(sessions, env=_Env(), transport=_rich_transport([_gql_node(number=7)]))
    _expire_graphql_cache()

    gql = []

    def handler(request):
        if str(request.url).endswith("/graphql"):
            gql.append(request)
            if _is_full_query(request):
                return httpx.Response(500, text="boom")
            body = {
                "data": {
                    "rateLimit": {"cost": 1, "remaining": 5000, "resetAt": "x"},
                    "repository": _alias_repo(request, {"feature": [_gql_node(number=9)]}),
                }
            }
            return httpx.Response(200, json=body)
        if request.headers.get("If-None-Match") == 'W/"v1"':
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])

    transport = httpx.MockTransport(handler)
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert [_is_full_query(r) for r in gql] == [False, True]  # digest mismatch, full failed
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 7  # stale cache served
    _expire_graphql_cache()
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 2  # cooldown: no digest, no full retry this tick
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 7


def test_budget_pressure_stretches_ceilings(tmp_path):
    # A low last-seen remaining (below BUDGET_PRESSURE_REMAINING but above the
    # backoff floor) stretches the background ceilings, so an age that would
    # normally trigger the digest no longer does.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    gql = []
    transport = _rich_transport([_gql_node(number=7)], gql=gql, remaining=600)
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1
    _expire_graphql_cache()  # past the 1x ceiling, well under the 4x one
    refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert len(gql) == 1  # pressure multiplier held the window shut


def test_budget_multiplier_resets_after_window(tmp_path):
    now = time.monotonic()
    refresh._graphql_budget.update({"remaining": 600, "valid_until": now + 60})
    assert refresh._budget_multiplier(now) == refresh.BUDGET_PRESSURE_MULTIPLIER
    assert refresh._budget_multiplier(now + 61) == 1.0  # window reset, budget refilled
    assert refresh._graphql_budget["remaining"] is None


def test_graphql_cost_is_logged_per_query(tmp_path, caplog):
    # The #69 telemetry gap: rateLimit.cost used to be selected and discarded.
    # Every query now logs kind/cost/remaining so a spend regression is visible.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    transport = _rich_transport([_gql_node(number=7)])
    with caplog.at_level(logging.INFO, logger="aoe_github_plugin.refresh"):
        refresh.build_snapshot(sessions, env=_Env(), transport=transport)
        _expire_graphql_cache()
        refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    kinds = [r.getMessage() for r in caplog.records if "graphql kind=" in r.getMessage()]
    assert any("kind=full" in m and "cost=1" in m and "remaining=5000" in m for m in kinds)
    assert any("kind=digest" in m for m in kinds)


# --- bounded parallel fan-out (#70) ---


def test_rest_probes_run_parallel_within_bound(tmp_path):
    # Eight distinct repos probe concurrently on the pool: strictly more than
    # one in flight at once (the fan-out is real), never more than the bound,
    # and every repo still resolves its own pulls correctly.
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(8):
        _make_repo(ws / f"r{i}", remote=f"https://github.com/o/r{i}.git")
    sessions = [{"id": "s1", "project_path": str(ws)}]

    lock = threading.Lock()
    state = {"inflight": 0, "max": 0}

    def handler(request):
        with lock:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
        time.sleep(0.05)
        with lock:
            state["inflight"] -= 1
        return httpx.Response(200, headers={"ETag": 'W/"v1"'}, json=[_pull()])

    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=httpx.MockTransport(handler))
    assert state["max"] > 1
    assert state["max"] <= refresh.REST_PROBE_WORKERS
    repos = snap["sessions"][0]["repos"]
    assert len(repos) == 8
    assert all(r["pulls"][0]["number"] == 12 for r in repos)


def test_parallel_discovery_output_matches_serial_shape(tmp_path):
    # The pooled discovery/identity path must produce the same per-session repo
    # entries (paths, names, repos, branches) as the serial implementation did.
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "a", remote="https://github.com/o/a.git", branch="b1")
    _make_repo(ws / "b", remote="https://github.com/o/b.git", branch="b2")
    (ws / "plain").mkdir()  # not a checkout; must not appear
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_NoToken(), transport=_transport([_pull()]))
    repos = snap["sessions"][0]["repos"]
    assert [(r["name"], r["repo"], r["branch"]) for r in repos] == [
        ("a", "o/a", "b1"),
        ("b", "o/b", "b2"),
    ]

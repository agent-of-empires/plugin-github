"""Multi-session refresh: discovery, aggregation, dedup, ETag, backoff.

Discovery uses real temp git repos (cheap); GitHub lookups use a MockTransport.
"""

import time
import subprocess

import httpx
import pytest

from aoe_github_plugin import refresh
from aoe_github_plugin.auth import TokenEnvironment


class _Env(TokenEnvironment):
    """Resolves a fixed token via env, never touching gh."""

    def env_var(self, key):
        return "tok" if key == "GITHUB_TOKEN" else None

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


@pytest.fixture(autouse=True)
def _clear_cache():
    refresh._etag_cache.clear()
    refresh._backoff["until"] = 0.0
    yield
    refresh._etag_cache.clear()
    refresh._backoff["until"] = 0.0


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
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=_transport([_pull()]))
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
    refresh.build_snapshot(sessions, env=_Env(), transport=_transport([_pull()], capture=captured))
    assert len(captured) == 1  # (o, r, feature) fetched once, not twice


def test_etag_304_reuses_cached_pulls(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    transport = _transport([_pull(number=99)])
    first = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert first["sessions"][0]["repos"][0]["pulls"][0]["number"] == 99
    # Second refresh: server answers 304 (cache hit); pulls come from cache.
    second = refresh.build_snapshot(sessions, env=_Env(), transport=transport)
    assert second["sessions"][0]["repos"][0]["pulls"][0]["number"] == 99


def test_backoff_serves_cached_and_skips_http(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "r")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    # Seed the cache, then trip backoff: the next refresh must not hit HTTP.
    refresh.build_snapshot(sessions, env=_Env(), transport=_transport([_pull(number=5)]))
    refresh._backoff["until"] = time.monotonic() + 60
    captured = []

    def boom(request):
        captured.append(request)
        raise AssertionError("HTTP must not be called during backoff")

    snap = refresh.build_snapshot(sessions, env=_Env(), transport=httpx.MockTransport(boom))
    assert captured == []
    assert snap["sessions"][0]["repos"][0]["pulls"][0]["number"] == 5


def test_non_github_checkout_is_benign(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_repo(ws / "local", remote="git@gitlab.com:o/r.git")
    sessions = [{"id": "s1", "project_path": str(ws)}]
    snap = refresh.build_snapshot(sessions, env=_Env(), transport=_transport([]))
    repo = snap["sessions"][0]["repos"][0]
    assert repo["repo"] is None
    assert repo["error"] is None
    assert repo["pulls"] == []

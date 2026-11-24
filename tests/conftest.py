"""Shared test isolation for optional GitHub CLI discovery."""

import pytest

from aoe_github_plugin.utils import gh_cache


@pytest.fixture(autouse=True)
def _isolate_gh_discovery(monkeypatch):
    monkeypatch.setattr(gh_cache, "gh_pr_repository", lambda _path: None)
    gh_cache._cache.clear()
    gh_cache._failures.clear()

"""Shared test isolation for optional GitHub CLI discovery."""

import pytest

from aoe_github_plugin import refresh
from aoe_github_plugin import handlers


@pytest.fixture(autouse=True)
def _isolate_gh_discovery(monkeypatch):
    monkeypatch.setattr(handlers, "gh_pr_repository", lambda _path: None)
    monkeypatch.setattr(refresh, "gh_pr_repository", lambda _path: None)
    refresh._gh_destination_cache.clear()
    refresh._gh_failure_until.clear()

"""Tests for scripts/featured_index.py (the post-release featured-index tool)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.featured_index import update_featured_versions

# A minimal featured.toml mirroring the AoE trust-root shape, with a comment and
# a sibling plugin to prove they survive an edit untouched. The versions inline
# table has the trailing space before `}` that trips tomlkit's in-place append.
FEATURED = """\
# featured index
[plugins."agent-of-empires.other"]
source = "gh:agent-of-empires/other"
versions = { "0.1.0" = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }

[plugins."agent-of-empires.github"]
source = "gh:agent-of-empires/plugin-github"
# keep this comment
versions = { "1.2.0" = "sha256:e5fe509a3e7d39030350d7ee7efcd94623e9ec8ce7707a121804ec95212aba8a" }
"""

GITHUB = "agent-of-empires.github"
NEW_HASH = "sha256:1111111111111111111111111111111111111111111111111111111111111111"


def test_update_appends_new_version() -> None:
    new_text, changed = update_featured_versions(FEATURED, GITHUB, "1.3.0", NEW_HASH)
    assert changed is True
    assert f'"1.3.0" = "{NEW_HASH}"' in new_text
    # Existing pin, sibling plugin, and comment all survive.
    assert "1.2.0" in new_text
    assert "agent-of-empires.other" in new_text
    assert "# keep this comment" in new_text
    # Regression guard: tomlkit drops the comma when appending in place to an
    # inline table with trailing whitespace, yielding invalid TOML. A
    # quote-whitespace-quote join (no comma) must never appear.
    assert not re.search(r'"[ \t]+"', new_text)


def test_update_output_round_trips() -> None:
    new_text, _ = update_featured_versions(FEATURED, GITHUB, "1.3.0", NEW_HASH)
    versions = tomlkit.parse(new_text)["plugins"][GITHUB]["versions"]
    assert str(versions["1.3.0"]) == NEW_HASH
    assert str(versions["1.2.0"]).startswith("sha256:")


def test_update_noop_when_present_with_same_hash() -> None:
    same = "sha256:e5fe509a3e7d39030350d7ee7efcd94623e9ec8ce7707a121804ec95212aba8a"
    new_text, changed = update_featured_versions(FEATURED, GITHUB, "1.2.0", same)
    assert changed is False
    assert new_text == FEATURED


def test_update_raises_when_present_with_different_hash() -> None:
    with pytest.raises(ValueError, match="already pinned"):
        update_featured_versions(FEATURED, GITHUB, "1.2.0", NEW_HASH)


def test_update_raises_when_section_missing() -> None:
    with pytest.raises(ValueError, match=r"no .* section"):
        update_featured_versions(FEATURED, "agent-of-empires.absent", "1.0.0", NEW_HASH)


def test_update_raises_on_malformed_hash() -> None:
    with pytest.raises(ValueError, match="malformed tree hash"):
        update_featured_versions(FEATURED, GITHUB, "1.3.0", "not-a-hash")

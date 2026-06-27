"""Tests for scripts/featured_index.py (the post-release featured-index tool)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.featured_index import tree_hash
from scripts.featured_index import update_featured_versions

# A 2-file tree with a .git dir and a root-level .aoe-build, both excluded.
# The expected digest is a regression anchor: if the algorithm changes, this
# value changes and the test flags it. The same algorithm is separately proven
# against real pinned hashes (1.0.0 / 1.2.0) by hashing `git archive <tag>`.
FIXTURE_HASH = "sha256:8050614cdddae02460f37738ee0adcbf539e0cfd17da16e0319da04a4c881bfa"

# A minimal featured.toml mirroring the AoE trust-root shape, with a comment and
# a sibling plugin to prove they survive an edit untouched.
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


def _make_fixture(root: Path) -> None:
    (root / "sub").mkdir()
    (root / ".git").mkdir()
    (root / ".aoe-build").mkdir()
    (root / "a.txt").write_bytes(b"alpha\n")
    (root / "sub" / "b.txt").write_bytes(b"beta")
    (root / ".git" / "ignored").write_bytes(b"x")
    (root / ".aoe-build" / "c").write_bytes(b"y")


def test_tree_hash_matches_anchor(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    assert tree_hash(tmp_path) == FIXTURE_HASH


def test_tree_hash_ignores_git_and_root_aoe_build(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    baseline = tree_hash(tmp_path)
    # Mutating an excluded path must not change the hash.
    (tmp_path / ".git" / "ignored").write_bytes(b"changed")
    (tmp_path / ".aoe-build" / "c").write_bytes(b"changed")
    assert tree_hash(tmp_path) == baseline


def test_tree_hash_includes_nested_aoe_build(tmp_path: Path) -> None:
    _make_fixture(tmp_path)
    baseline = tree_hash(tmp_path)
    # A nested .aoe-build is real content (only the root one is excluded).
    nested = tmp_path / "sub" / ".aoe-build"
    nested.mkdir()
    (nested / "d").write_bytes(b"z")
    assert tree_hash(tmp_path) != baseline


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ")
def test_tree_hash_rejects_symlink(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_bytes(b"real")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    with pytest.raises(ValueError, match="symlink"):
        tree_hash(tmp_path)


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

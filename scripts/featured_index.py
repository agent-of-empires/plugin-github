"""Compute the AoE plugin tree hash and pin a release in AoE's featured index.

CI-only tooling used by the post-release `featured-pr` job. It reproduces the
host's `aoe plugin hash` source-tree hash (algorithm `aoe-plugin-tree-hash-v1`)
so plugin-github CI can open a PR adding a `"<version>" = "sha256:..."` entry to
`plugins/featured.toml` in agent-of-empires/agent-of-empires.

`plugins/featured.toml` is a security trust root: the host only treats a
reserved-namespace plugin as featured when the fetched source tree hash matches
a pinned value. So this edits TOML through tomlkit (real parse, comment- and
format-preserving), never string surgery, and fails closed on any surprise.

Two subcommands:
    hash <dir>                              print the tree hash of a source tree
    update <toml> <plugin_id> <ver> <hash>  pin the version in featured.toml

`update` exits 0 whether it wrote a change or found the pin already present; it
exits nonzero (writing nothing) on any inconsistency, e.g. the version already
pinned to a different hash, which signals a retag, tamper, or algorithm drift.
"""

from __future__ import annotations

import re
import sys
import hashlib
from pathlib import Path

import tomlkit

# Domain-separation header. Mirrors HASH_PREFIX in agent-of-empires
# src/plugin/integrity.rs. The `v1` is load-bearing: if the host moves to a v2
# tree hash this constant must move with it, or the pins this produces silently
# stop matching. The PR body names the algorithm so a reviewer can catch drift.
HASH_PREFIX = b"aoe-plugin-tree-hash-v1\0"

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _collect(root: Path, current: Path, out: list[tuple[str, bytes]]) -> None:
    for entry in sorted(current.iterdir(), key=lambda p: p.name):
        rel = entry.relative_to(root).as_posix()
        # Symlinks are rejected at every level: the host refuses them (a venv
        # symlink in source root is what bricked the 1.1.0 pin).
        if entry.is_symlink():
            raise ValueError(f"symlink not allowed in plugin source tree: {rel}")
        if entry.is_dir():
            # .git is never part of the source tree; .aoe-build is the reserved
            # build-output dir, excluded only at the root (a nested one is real
            # content). Matches the host's collect() rules.
            if entry.name == ".git":
                continue
            if current == root and entry.name == ".aoe-build":
                continue
            _collect(root, entry, out)
        else:
            # Reject non-UTF-8 paths the way the host does: encoding a name with
            # surrogate-escaped bytes raises here.
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"non-UTF-8 path in plugin source tree: {rel!r}") from exc
            out.append((rel, entry.read_bytes()))


def tree_hash(root: Path) -> str:
    """Return the `sha256:<hex>` source-tree hash for `root`.

    Hash a directory whose contents mirror the host's fetch: the tracked files
    at a tag with `.git` stripped, e.g. a `git archive <tag>` extraction.
    """
    files: list[tuple[str, bytes]] = []
    _collect(root, root, files)
    files.sort(key=lambda item: item[0])

    hasher = hashlib.sha256()
    hasher.update(HASH_PREFIX)
    for rel, contents in files:
        hasher.update(b"file\0")
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(contents).to_bytes(8, "little"))
        hasher.update(contents)
    return "sha256:" + hasher.hexdigest()


def update_featured_versions(
    toml_text: str,
    plugin_id: str,
    version: str,
    tree_hash_value: str,
) -> tuple[str, bool]:
    """Pin `version -> tree_hash_value` for `plugin_id`.

    Returns `(new_text, changed)`. `changed` is False when the pin is already
    present with the identical hash (a clean no-op). Raises ValueError on a
    missing plugin section, a missing/ill-typed `versions` table, a malformed
    hash, or the version already pinned to a different hash.
    """
    if not HASH_RE.match(tree_hash_value):
        raise ValueError(f"malformed tree hash: {tree_hash_value!r}")

    doc = tomlkit.parse(toml_text)

    plugins = doc.get("plugins")
    if plugins is None or plugin_id not in plugins:
        raise ValueError(f'no [plugins."{plugin_id}"] section in featured index')

    versions = plugins[plugin_id].get("versions")
    if versions is None:
        raise ValueError(f'[plugins."{plugin_id}"] has no versions table')

    existing = versions.get(version)
    if existing is not None:
        if existing != tree_hash_value:
            raise ValueError(
                f"{plugin_id} {version} already pinned to {existing}, refusing to overwrite with {tree_hash_value}"
            )
        return toml_text, False

    versions[version] = tree_hash_value
    return tomlkit.dumps(doc), True


def _cmd_hash(args: list[str]) -> int:
    if len(args) != 1:
        print("usage: featured_index.py hash <dir>", file=sys.stderr)
        return 2
    print(tree_hash(Path(args[0])))
    return 0


def _cmd_update(args: list[str]) -> int:
    if len(args) != 4:
        print(
            "usage: featured_index.py update <toml> <plugin_id> <version> <hash>",
            file=sys.stderr,
        )
        return 2
    toml_path = Path(args[0])
    new_text, changed = update_featured_versions(toml_path.read_text(encoding="utf-8"), args[1], args[2], args[3])
    if changed:
        toml_path.write_text(new_text, encoding="utf-8")
        print("changed")
    else:
        print("unchanged")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 1:
        print("usage: featured_index.py {hash,update} ...", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "hash":
        return _cmd_hash(rest)
    if cmd == "update":
        return _cmd_update(rest)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

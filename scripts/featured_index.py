"""Pin a released version in AoE's featured index.

CI-only tooling used by the post-release `featured-pr` job. The tree hash itself
is computed by the canonical `aoe plugin hash` binary (downloaded from the
latest agent-of-empires release) so this never has to track the host's hashing
algorithm. This module only edits the trust-root TOML.

`plugins/featured.toml` is a security trust root: the host only treats a
reserved-namespace plugin as featured when the fetched source tree hash matches
a pinned value. So this edits TOML through tomlkit (real parse, comment- and
format-preserving), never string surgery, and fails closed on any surprise.

One subcommand:
    update <toml> <plugin_id> <version> <hash>  pin the version in featured.toml

It exits 0 whether it wrote a change or found the pin already present; it exits
nonzero (writing nothing) on any inconsistency, e.g. the version already pinned
to a different hash, which signals a retag, tamper, or algorithm drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomlkit

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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
        if str(existing) != tree_hash_value:
            raise ValueError(
                f"{plugin_id} {version} already pinned to {existing}, refusing to overwrite with {tree_hash_value}"
            )
        return toml_text, False

    # Rebuild the inline table rather than mutating it in place: tomlkit drops
    # the comma separator when appending to an inline table that has trailing
    # whitespace before its closing brace (the `{ ... }` style this file uses),
    # producing invalid TOML. A fresh table serializes correctly every time.
    rebuilt = tomlkit.inline_table()
    for key, value in versions.items():
        rebuilt[key] = str(value)
    rebuilt[version] = tree_hash_value
    plugins[plugin_id]["versions"] = rebuilt
    return tomlkit.dumps(doc), True


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
    if len(argv) < 1 or argv[0] != "update":
        print("usage: featured_index.py update <toml> <plugin_id> <version> <hash>", file=sys.stderr)
        return 2
    return _cmd_update(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

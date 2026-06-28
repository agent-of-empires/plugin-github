"""Lint the shipped ``aoe-plugin.toml`` so a command/keybind contribution stays
consistent with what the host accepts: every keybind targets a declared command,
every chord parses to the host's accepted form, and a client `action` points at a
declared per-session UI slot and carries the capability the host gates it on.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit

MANIFEST = Path(__file__).resolve().parent.parent / "aoe-plugin.toml"

# UiSlot variants the host treats as per-session (UiSlot::is_per_session).
PER_SESSION_SLOTS = {"row-badge", "row-column", "pane", "detail-badge"}

# Modifiers the host's parse_chord accepts; Alt/Cmd/Meta/Super are rejected.
ALLOWED_MODIFIERS = {"ctrl", "control", "shift"}


def _manifest() -> dict:
    return tomlkit.parse(MANIFEST.read_text()).unwrap()


def _command_ids(m: dict) -> set[str]:
    return {c["id"] for c in m.get("commands", [])}


def test_keybinds_target_declared_commands():
    m = _manifest()
    ids = _command_ids(m)
    for kb in m.get("keybinds", []):
        target = kb["command"].removeprefix(f"plugin.{m['id']}.")
        assert target in ids, f"keybind targets unknown command {kb['command']!r}"


def test_keybind_chords_parse_for_the_host():
    m = _manifest()
    for kb in m.get("keybinds", []):
        tokens = [t.strip().lower() for t in kb["key"].split("+") if t.strip()]
        base = [t for t in tokens if t not in ALLOWED_MODIFIERS]
        assert len(base) == 1, f"chord {kb['key']!r} must have exactly one base key"
        mods = [t for t in tokens if t in ALLOWED_MODIFIERS or t == base[0]]
        assert len(mods) == len(tokens), f"chord {kb['key']!r} uses a modifier the host rejects"


def test_client_action_points_at_declared_per_session_slot():
    m = _manifest()
    declared = {(u["slot"], u.get("id", "")) for u in m.get("ui", [])}
    for c in m.get("commands", []):
        action = c.get("action")
        if not action:
            continue
        assert action["kind"] == "open-ui-link", f"unknown action kind {action['kind']!r}"
        assert action["slot"] in PER_SESSION_SLOTS, f"{action['slot']!r} is not per-session"
        assert (action["slot"], action["id"]) in declared, "action references an undeclared ui slot"


def test_client_action_requires_browser_open_capability_and_api_6():
    m = _manifest()
    if any(c.get("action") for c in m.get("commands", [])):
        assert "browser_open" in m.get("capabilities", []), "client action needs browser_open"
        assert m["api_version"] >= 6, "client action needs api_version >= 6"


def test_open_pr_command_is_wired():
    m = _manifest()
    open_pr = next((c for c in m.get("commands", []) if c["id"] == "open_pr"), None)
    assert open_pr is not None, "open_pr command missing"
    assert open_pr["action"] == {"kind": "open-ui-link", "slot": "row-badge", "id": "github_pr_badge"}
    assert any(kb["command"] == "open_pr" for kb in m.get("keybinds", [])), "open_pr has no keybind"

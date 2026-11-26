"""Lint the shipped ``aoe-plugin.toml`` so a command/keybind contribution stays
consistent with what the host accepts: every keybind targets a declared command,
every chord parses to the host's accepted form, and a client `action` points at a
declared per-session UI slot and carries the capability the host gates it on,
and every slot the worker pushes is declared.
"""

from __future__ import annotations

from pathlib import Path

import tomlkit

from aoe_github_plugin import uistate

MANIFEST = Path(__file__).resolve().parent.parent / "aoe-plugin.toml"

# UiSlot variants the host treats as per-session (UiSlot::is_per_session).
PER_SESSION_SLOTS = {"row-badge", "row-column", "pane", "detail-badge"}

# Modifiers the host's parse_chord accepts; Alt/Cmd/Meta/Super are rejected.
ALLOWED_MODIFIERS = {"ctrl", "control", "shift"}


SCOPE_COPY = {
    "suggestion_organizations": ("Only these organizations", "Leave empty for your Projects' repos."),
    "suggestion_repositories": ("Only these repos", "Like acme/widgets. Leave empty for all."),
    "suggestion_ignore": ("Never suggest from", "Organizations or repos. Wins over the two lists above."),
    "suggestion_limit": ("Keep up to", "Suggestions from GitHub at a time."),
}


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


_BARE_VERB_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _bare_verb(name: str) -> bool:
    return bool(name) and name[0] != "-" and set(name) <= _BARE_VERB_CHARS


def test_commands_nest_under_a_declared_group():
    # api_version 26: a command's `group` must name a declared [[command_groups]]
    # entry, and both ids must be one bare lowercase verb the CLI can graft.
    m = _manifest()
    groups = {g["id"] for g in m.get("command_groups", [])}
    assert groups, "the CLI nesting depends on a declared command group"
    for group in groups:
        assert _bare_verb(group), f"group id {group!r} is not a bare CLI verb"
    for c in m.get("commands", []):
        group = c.get("group")
        if group is None:
            continue
        assert group in groups, f"command {c['id']!r} names undeclared group {group!r}"
        assert _bare_verb(c["id"]), f"nested command id {c['id']!r} is not a bare CLI verb"
    assert m["api_version"] >= 26, "command groups need api_version >= 26"


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


def test_every_pushed_ui_slot_is_declared():
    # The host only accepts ui.state.set for a declared (slot, id), so a push the
    # manifest misses (e.g. a new global filter-facet) would be rejected.
    m = _manifest()
    declared = {(u["slot"], u.get("id", "")) for u in m.get("ui", [])}
    snapshot = {"sessions": [{"session_id": "s1", "repos": []}]}
    pushed = {(p["slot"], p["id"]) for p in uistate.snapshot_ui_state_params(snapshot)}
    assert pushed <= declared, f"undeclared ui slots: {sorted(pushed - declared)}"


def test_icon_asset_exists_on_disk():
    m = _manifest()
    icon_asset = m.get("icon_asset")
    if icon_asset:
        path = MANIFEST.parent / icon_asset
        assert path.is_file(), f"icon_asset {icon_asset!r} does not exist on disk"


def test_icon_requires_api_version_7():
    m = _manifest()
    if m.get("icon") or m.get("icon_asset"):
        assert m["api_version"] >= 7, "icon/icon_asset requires api_version >= 7"


def test_suggestion_projects_setting_is_gone():
    assert "suggestion_projects" not in {setting["key"] for setting in _manifest()["settings"]}


def test_scope_settings_use_the_plain_labels_and_help():
    settings = {setting["key"]: setting for setting in _manifest()["settings"]}
    assert {key: (s["label"], s["description"]) for key, s in settings.items() if key in SCOPE_COPY} == SCOPE_COPY


def test_the_plugin_id_stays_stable():
    # Installed settings and the featured index key on this id.
    assert _manifest()["id"] == "agent-of-empires.github"

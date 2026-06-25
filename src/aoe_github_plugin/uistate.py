"""Derive host UI state from a ``github.status`` result.

The host renders UI slots itself; the worker only pushes typed display state
through the ``ui.state.set`` host RPC (issue #2366, design section D9). Plugin
code never runs in the dashboard. This module is the pure half: it turns the
fail-soft ``github.status`` dict into one ``ui.state.set`` ``params`` object per
declared slot. No id, no IO, so the mapping is unit-testable on its own.

The params shape ``{slot, id, state}`` mirrors the ``[[ui]]`` manifest keys
(``slot`` + ``id``); ``state`` carries the renderable fields a badge / status
segment needs. D9 is not merged yet, so this shape is an informed guess pinned
to the issue; expect to remap ``state`` when the contract lands.
"""

from __future__ import annotations

from typing import Any

# (slot, contribution id) pairs this plugin fills, matching the [[ui]] entries
# in aoe-plugin.toml and the host's UiSlot enum. Same derived state goes to
# both: a status-bar segment and a per-session-row badge.
SLOTS: list[tuple[str, str]] = [
    ("status-bar", "github_status"),
    ("row-badge", "github_pr_badge"),
]


def _tone(pull: dict[str, Any] | None, error: Any) -> str:
    """A coarse state string the host maps to a colour."""
    if error:
        return "error"
    if pull is None:
        return "none"
    return "draft" if pull.get("draft") else "open"


def _text(pull: dict[str, Any] | None, error: Any) -> str:
    """Short badge label (the status-bar segment shows ``tooltip`` instead)."""
    if error:
        return "GitHub !"
    if pull is None:
        return "no PR"
    return f"PR #{pull['number']}"


def ui_state_params(status: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``ui.state.set`` ``params`` dict per slot, from a ``github.status``
    result. Pure and total: a malformed/partial status still yields valid
    params (missing keys read as empty), so the caller stays fail-soft.
    """
    pulls = status.get("pulls") or []
    pull = pulls[0] if pulls else None
    error = status.get("error")
    state: dict[str, Any] = {
        "text": _text(pull, error),
        "tone": _tone(pull, error),
        "tooltip": status.get("summary") or "",
    }
    if pull is not None and pull.get("url"):
        state["url"] = pull["url"]
    return [{"slot": slot, "id": cid, "payload": state} for slot, cid in SLOTS]

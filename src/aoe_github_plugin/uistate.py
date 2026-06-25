"""Derive host UI state from a ``github.status`` result.

The host renders UI slots itself; the worker only pushes typed display state
through the ``ui.state.set`` host RPC (issue #2366, design section D9). Plugin
code never runs in the dashboard. This module is the pure half: it turns the
fail-soft ``github.status`` dict into one ``ui.state.set`` ``params`` object per
declared slot. No id, no IO, so the mapping is unit-testable on its own.

The params shape ``{slot, id, payload[, session_id]}`` mirrors the ``[[ui]]``
manifest keys (``slot`` + ``id``); ``payload`` is the host's ``TextPayload``
(``text`` + optional ``tone``/``tooltip``, no extra fields: the host parses it
``deny_unknown_fields``). ``tone`` is one of the host's closed ``Tone`` set.

Slot scope follows the host: ``status-bar`` is global (one summary, no
``session_id``); ``row-badge`` is per session (the host keys it by
``session_id``, so a push without one is rejected). The caller passes the
session being described, or ``None`` for the global push.
"""

from __future__ import annotations

from typing import Any

# Contribution ids per slot, matching the [[ui]] entries in aoe-plugin.toml.
GLOBAL_SLOTS: list[tuple[str, str]] = [("status-bar", "github_status")]
SESSION_SLOTS: list[tuple[str, str]] = [("row-badge", "github_pr_badge")]

# github.status tone -> host Tone variant (neutral/info/success/warn/danger).
_TONES = {"error": "danger", "none": "neutral", "draft": "warn", "open": "success"}


def _tone(pull: dict[str, Any] | None, error: Any) -> str:
    """A host ``Tone`` variant for the PR's state."""
    if error:
        return _TONES["error"]
    if pull is None:
        return _TONES["none"]
    return _TONES["draft"] if pull.get("draft") else _TONES["open"]


def _text(pull: dict[str, Any] | None, error: Any) -> str:
    """Short badge label (the status-bar segment shows ``tooltip`` instead)."""
    if error:
        return "GitHub !"
    if pull is None:
        return "no PR"
    return f"PR #{pull['number']}"


def ui_state_params(status: dict[str, Any], session_id: str | None = None) -> list[dict[str, Any]]:
    """``ui.state.set`` ``params`` dicts derived from a ``github.status`` result.

    With ``session_id`` ``None`` the global slots are produced (no
    ``session_id`` key); with a ``session_id`` the per-session slots are
    produced (each carrying it). Pure and total: a malformed/partial status
    still yields valid params, so the caller stays fail-soft.
    """
    pulls = status.get("pulls") or []
    pull = pulls[0] if pulls else None
    error = status.get("error")
    payload: dict[str, Any] = {
        "text": _text(pull, error),
        "tone": _tone(pull, error),
        "tooltip": status.get("summary") or "",
    }
    slots = SESSION_SLOTS if session_id is not None else GLOBAL_SLOTS
    out = [{"slot": slot, "id": cid, "payload": payload} for slot, cid in slots]
    if session_id is not None:
        for params in out:
            params["session_id"] = session_id
    return out

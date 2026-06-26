# Multi-session GitHub refresh — implementation plan

**Goal:** The worker enumerates all agent sessions, discovers the git repos in each
session's workspace, looks up the open PR for each repo's branch, and pushes one
per-session `row-badge` plus one global `status-bar` to the host UI — efficiently
(dedup + ETag + backoff) and without racing stdin.

## Debate summary

- **gemini:** queue-driven event loop (ReaderThread + `queue.Queue`), few files.
- **openai:** reactor + background executor with generation state machine, typed
  models, ~6 modules.

**Agreement:** kill `_poll_loop`; single stdin owner; worktree-safe discovery via
`git rev-parse --show-toplevel`; dedup by `(owner,repo,branch)`; ETag/`304`;
`403/429` backoff; `github.refresh` schedules + returns immediately; new `refresh.py`
so `handlers.py` stays a thin handler module.

**Resolved:**
- Loop: **ReaderThread + queue** (gemini) — portable, no `select()` quirks, no drops.
- Refresh execution: **synchronous on the main thread** with hard git/HTTP timeouts,
  no overlap, no generation IDs (openai conceded; bg-thread earns nothing at 300s).
- Tone: **danger > success > warn > neutral**; hard errors only (auth/rate-limit/
  network/API) → danger; benign (no github remote / detached HEAD / no PR) never warns.
- `github.status`: stays a **live single-path** lookup; aggregate is push-only via refresh.
- Data shapes: plain dicts (match existing fail-soft style) + a `(owner,repo,branch)`
  tuple key for dedup. No dataclasses.

## Files

- `client.py` — add `get_json_conditional(path, params, etag)` → `(status, etag, json|None)`,
  handling `304` before the `is_success` check.
- `refresh.py` (new) — discovery, repo identity, `RepoKey` dedup, module-level ETag
  cache + backoff (lock-guarded), `ThreadPoolExecutor` fan-out, aggregate snapshot dict.
- `uistate.py` — aggregate mappers: `snapshot_ui_state_params(snapshot)` → global
  status-bar + per-session row-badges; tone/text cascade. Keep payload `{text,tone?,tooltip?}`.
- `main.py` — ReaderThread + queue runtime; `_call_host` reads the queue; synchronous
  `run_refresh()` on timeout / on `github.refresh`; remove `_poll_loop` and single-path startup push.
- `handlers.py` — unchanged behavior (live single-path `github_status` + `github_open`).
- tests — rewrite `test_uistate.py` for the aggregate shape; add `refresh` + client-conditional tests.

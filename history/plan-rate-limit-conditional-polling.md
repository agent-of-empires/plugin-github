# Rate-limit conditional polling (#21 #22 #23)

**Goal:** Make steady-state polling cost ~0 by gating the expensive GraphQL query behind a free REST conditional (304) check, raise + harden the network cadence, and trim GraphQL cost when it does run.

## Debate Summary

**Positions:**
- **gemini:** Initially rejected REST gating (keep GraphQL live every tick + trim + batch). After the shared-budget math, conceded live-every-tick burns too much of the user's 5000 pts/hr; final pitch was a state-aware gate (force GraphQL only when cached state is active: CI running/queued or review waiting).
- **openai (gpt-5.5):** REST conditional gate primary + bounded max-staleness ceiling; defer batching; keep `contexts(first:50)` (trimming to 20 is a correctness bug). Lowered ceiling 600s -> 300s.

**Points of agreement:**
- REST conditional gate is the right primitive for idle polling; a free 304 beats continuous GraphQL spend against the user's shared budget.
- Trimming `contexts(first:50)` -> 20 is a real correctness bug: the failure-first sort is client-side (`_STATE_RANK`), so a failing check beyond the first 20 fetched contexts would be silently dropped. **Keep `contexts(first:50)`.**
- Trim `reviews(last:20)` -> `reviews(last:1, states:[COMMENTED])`, `reviewThreads(first:50)` -> 20, `pullRequests(first:5)` -> 3. Add `rateLimit.cost`.
- Serialize (drop ThreadPoolExecutor), 120s default interval, positive jitter.
- Defer cross-key batching this PR; batching can cut GraphQL points (query minimums/rounding), not just round-trips, but it complicates per-key fail-soft normalization and only helps the many-branches-one-repo topology. Measure `rateLimit.cost` first.

**Resolved disagreements:**
- Staleness mitigation: gemini wanted a state-aware gate; gpt-5.5 wanted a simple time ceiling. **Verdict:** single 300s max-staleness ceiling. The state-aware gate is genuinely better UX for the running->done transition but adds branching on cached state, exceeds what #21 literally asks ("fire GraphQL only on change / user refresh / first load"), and fights minimal-diff. The ceiling bounds CI staleness to ~5 min, the Refresh button gives instant `force=True`. Document the state-aware gate as a future enhancement.
- Ceiling value: 600 vs 300. **Verdict:** 300s. Bounds stale CI/review to ~5-6 min once quantized by the 120s tick, ~66% idle-GraphQL reduction vs live-every-tick.
- Main-loop latency from serial fetches: real (N serial requests block the synchronous refresh), but acceptable at 120s cadence with the reader thread draining stdin. Document; bounded pool later only if measured slow.

**Verdict:** Implement the REST-conditional gate + `GRAPHQL_MAX_STALE=300` (replacing `GRAPHQL_TTL`), 120s default, serial fetch, positive jitter, conservative query trim (keep contexts:50), add `rateLimit.cost`. Defer batching and the state-aware gate with documented rationale.

## Tasks

### graphql.py
- `reviews(last:1, states:[COMMENTED])`; `reviewThreads(first:20)`; `pullRequests(... first:3 ...)`; keep `contexts(first:50)`.
- `rateLimit { cost remaining resetAt }`.

### refresh.py
- Add `GRAPHQL_MAX_STALE = 300.0`; remove `GRAPHQL_TTL`, `MAX_WORKERS`, `ThreadPoolExecutor` import.
- Split a REST conditional probe helper returning `(changed, basic_pulls)`; no-token `_fetch_key` delegates to it.
- Token path `_fetch_key_rich_gated`: probe REST; fire GraphQL (force=True internally) on `force or rest_changed or rich_cache is None or age >= GRAPHQL_MAX_STALE`, else serve rich cache. GraphQL failure falls back to rich cache, then basic pulls, then raise.
- Serialize `_fetch_all` (sorted keys for determinism).

### main.py
- `DEFAULT_REFRESH_SECS = 120`; manifest default 30 -> 120.
- `import random`; positive bounded jitter helper; apply to `_next_network` at startup and in `_refresh_and_reset`.

### README.md
- New polling model (REST gate -> GraphQL on change), 120s default + rate math, 300s freshness bound, note CI/review state refreshes within ~5 min or on Refresh.

### Tests
- 304 + fresh cache: no GraphQL. REST 200: GraphQL. force: GraphQL. age >= ceiling: GraphQL. no rich cache: GraphQL. GraphQL fail -> basic/stale fallback. serial order. interval 120. jitter positive+bounded. trimmed query shape. cost parsed.

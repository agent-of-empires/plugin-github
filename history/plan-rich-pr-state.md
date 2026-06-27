# Rich per-PR state in the GitHub pane (implementation plan)

**Goal:** Show merged state (purple), review state, unresolved-comment list, and CI check state for each PR in the GitHub plugin pane, via a token-gated GraphQL fetch, degrading to the current open-PR view (with a banner) when no token is present.

## Debate Summary

**Positions:**
- **gemini:** `Tone` becomes a serde union (semantic OR hex), inline style; 1 new `comment` block; 60s TTL, no probe.
- **openai:** separate validated `color` hex field, controlled inline style; TTL + read `rateLimit` + serve stale; 1 `comment` block; merged pane-only; infer "commented"; pane note + one-shot notify.
- **anthropic:** pivoted to arbitrary hex; render via CSS var + `color-mix` (single audited boundary); conceded rateLimit caching + reviewDecision fix.
- **grok:** validated hex but web-only (no host struct change), `updatedAt`+headOid caching, single `pr` block.

**Points of agreement:**
- GraphQL only with a token; keep the existing REST open-PR path as the no-token fallback.
- `reviewDecision` cannot express "commented" → infer it from `reviews[].state == COMMENTED` or any unresolved thread.
- Drop the `updatedAt` short-circuit: a check-run state change does NOT bump PR `updatedAt`, so it serves stale CI.
- Merged PRs belong in the pane, not the row badge (avoids stale-branch noise).
- No-token UX: a persistent pane `note` banner.

**Resolved disagreements:**
- Color field shape: gemini's `Tone` union corrupts the semantic axis; closed palettes (anthropic/grok) fail the "allow all colors" mandate. **Verdict:** validated hex, rendered web-side only. grok is right that for the pane it is web-only: pane blocks are opaque, so NO host Rust change is needed, and adding an unused host `color` field would violate AoE's no-dead-code rule. Merged lives in the pane, so web-only hex covers the feature.
- Caching: `updatedAt` probe rejected by 3 of 4. **Verdict:** no probe. The worker's refresh cadence (`ui_refresh_secs`, default 30s) already gates frequency; per refresh, fetch each deduped key, read `rateLimit`, set the existing backoff on 403/429/low-remaining, and serve the last-good cached result while backed off. No separate TTL.
- Block kinds: 1 vs 3. **Verdict:** ONE new `comment` block; render PR headline, review state, CI rollup, and per-check rows with the existing `row`/`section`/`note` blocks. Minimal, keeps the vocabulary generic.
- Notify: **Verdict:** skip `ui.notify` (needs the `notifications` capability + a reinstall prompt). The persistent pane `note` banner is the agreed UX and needs no new capability.

**Verdict:** Worker (Python) switches to a single GraphQL query per `(owner, repo, branch)` when a token is present, normalizes it to a richer per-PR dict, and emits richer pane blocks (merged purple via a `color` field on the row block, a review-state row, a CI section, and a comments section of `comment` blocks). Without a token it keeps the current REST open-PR view and prepends a warn `note`. Web (agent-of-empires) gains a `comment` block renderer and validated-hex `color` support on `row`/`comment` blocks and badge chips. No Rust host change.

---

### Task 1: Web — validated hex color + `comment` block (agent-of-empires)

**Files:**
- Modify `web/src/lib/pluginUi.ts`: add `validColor(v): string | undefined` (strict `#RGB`/`#RRGGBB`, normalized lowercase `#rrggbb`, else undefined) and `accentStyle(color)` returning a React style object (`color`, `backgroundColor` via `color-mix`).
- Modify `web/src/components/plugin/PluginSlots.tsx`: `BlockRow` and `BadgeChip` apply `accentStyle(block.color)` when present (overrides tone color only; tone classes still apply when no color). Add `BlockComment` + `case "comment"` in `DetailBlock`.
- Add tests under `web/src/components/plugin/__tests__/` and/or `web/src/lib/__tests__/`: hex accepted, junk rejected, `comment` renders read-only.

### Task 2: Worker — GraphQL client + query/normalize module (plugin-github)

**Files:**
- Modify `src/aoe_github_plugin/client.py`: add `post_graphql(query, variables) -> dict` (POST `/graphql`, reuse `classify_status`, decode JSON, surface partial data).
- Create `src/aoe_github_plugin/graphql.py`: `QUERY` constant, `normalize_pull(node)`, `review_state(node)`, `check_state(rollup)`, `excerpt(body)`. Pure, unit-tested.
- Tests `tests/test_graphql.py`.

### Task 3: Worker — token-gated fetch + snapshot enrichment (plugin-github)

**Files:**
- Modify `src/aoe_github_plugin/refresh.py`: when a token is present, fetch via GraphQL keyed by `(owner,repo,branch)`; read `rateLimit`, set backoff on rate-limit, serve last-good from a module `_graphql_cache`. When absent, keep REST path and set snapshot `auth.present = False`.
- Tests `tests/test_refresh.py`.

### Task 4: Worker — richer pane blocks (plugin-github)

**Files:**
- Modify `src/aoe_github_plugin/uistate.py`: per PR, emit headline `row` (merged → `color:"#8957e5"`, icon `git-merge`), a Review `row`, a CI `section` of check `row`s with a rollup, and a comments `section` of `comment` blocks. Prepend a warn `note` when `auth.present` is false. Row badge unchanged (merged omitted).
- Tests `tests/test_uistate.py`.

### Task 5: Docs

**Files:**
- Modify `plugin-github/README.md`: token requirement for rich data; what degrades without it.
- Modify the plugin-UI doc in agent-of-empires (the `pane` block vocabulary): document the `comment` block kind and the `color` field + hex validation + no-arbitrary-CSS note.

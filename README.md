# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request state alongside your agent sessions, and (later) drive the
common git/GitHub operations from #658 without dropping into a terminal.

> Status: **read operations**. This release ports the GitHub client + token-auth
> layer from AoE core (PR #1681 / issue #1667) into a Tier 1 plugin worker, with
> structured `github.status` (open PRs for the branch, each with its URL) and
> `github.open` (open-in-GitHub). The per-session pane shows rich PR state
> (state incl. merged, review state, CI checks, and unresolved comments) when a
> token is present, degrading to open PRs only without one. The write operations
> (create/merge PR, push, pull, fix-CI) land in follow-ups.

## Layout

```
src/aoe_github_plugin/
  main.py            JSON-RPC stdio loop + method dispatch (entrypoint)
  auth.py            token resolution (GITHUB_TOKEN/GH_TOKEN, then `gh auth token`)
  client.py          GitHubClient + header-driven error classification
  errors.py          error taxonomy, each variant with an actionable hint
  handlers.py        the plugin's features (github.status)
  utils/             gitctx (remote/branch introspection), rpc (response builders)
tests/               pytest suite (no network, no real gh)
```

## Install

From the dashboard (Settings -> Plugins -> Discover) or the CLI:

```sh
aoe plugin install agent-of-empires/plugin-github
```

Installing prompts for the plugin's declared capabilities (`net`,
`runtime.worker`, `session.read`, `notifications`) before anything is written.

## What it contributes

| Kind    | Detail                                                             |
| ------- | ------------------------------------------------------------------ |
| Command | `status` -> worker method `github.status`                          |
| Command | `refresh` -> worker method `github.refresh`                        |
| Command | `open` (open-in-GitHub) -> worker method `github.open`             |
| UI      | a `row-badge` (`github_pr_badge`) and a `pane` tool-window (`github_pane`) |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

At install/update the host runs the manifest's `[[runtime.build]]` steps in the
plugin directory: create a venv under `.aoe-build/` and `pip install .` into it.
The worker then launches from the plugin-relative
`.aoe-build/venv/bin/aoe-github-worker`, so the daemon's PATH never decides
whether it starts (#2406). The venv lives under `.aoe-build/` because the host
excludes that directory from the plugin tree_hash; a venv contains symlinks,
which tree_hash rejects, so building into the source root breaks the load-time
hash check. Build steps are scoped to macOS/Linux.

### Methods

`github.status` is a live, single-checkout lookup, fail-soft: it always returns
a structured result, never a JSON-RPC error, so a caller always has something to
render. `github.refresh` returns `{ "accepted": true }` immediately and triggers
a full multi-session UI refresh (below).

```jsonc
{
  "summary": "owner/repo: PR #12 open for my-branch",  // one line for a status bar
  "repo": "owner/repo",      // null outside a github.com checkout
  "branch": "my-branch",     // null outside a github.com checkout
  "pulls": [                 // open PRs whose head is the current branch
    { "number": 12, "url": "https://github.com/owner/repo/pull/12",
      "title": "...", "state": "open", "draft": false }
  ],
  "error": null              // else { "kind": "...", "hint": "..." } (still fail-soft)
}
```

`github.open` resolves the URL to open in a browser and returns
`{ "url", "kind" }`: `kind: "pull"` (an open PR exists for the branch) or
`kind: "compare"` (the create-PR page). Finding an existing PR is best-effort:
any API failure falls back to the compare URL so it still works offline. It
raises a typed error only when the checkout has no github.com remote.

### Multi-session refresh

Beyond answering requests, the worker proactively drives the UI. On startup, on
`github.refresh`, and on a background poll it runs one refresh:

1. `sessions.list` (host RPC) -> every session and its workspace `project_path`.
2. For each workspace, discover the git checkouts: the workspace root plus each
   immediate child directory that is its own checkout (worktree-safe -- a
   worktree's `.git` is a file, so discovery asks `git rev-parse --show-toplevel`
   rather than looking for a `.git` directory).
3. Resolve each checkout to `(owner, repo, branch)`, deduplicate (a branch shared
   across workspaces is fetched once), and look up its PRs. Lookups run serially
   (no concurrent fan-out) to stay clear of GitHub's secondary/concurrency
   limits. With a token the per-branch lookup is a cheap REST conditional check
   first (see "Rate limits" below); only when that reports a change (or on a
   forced refresh, or when the cached rich data has aged out) does it spend a
   GraphQL query for the rich state (PR state incl. MERGED, `reviewDecision`, the
   head commit's check rollup + per-check runs, and every unresolved review
   thread with its first comment). Branches of the same repo that need a fresh
   fetch are aliased into one batched GraphQL query rather than one per branch, so
   a workspace of many worktrees of one repo costs a single query (split into a
   few once it exceeds the per-query alias cap). Without a token it is the basic
   REST open-PR lookup only.
4. Push two `ui.state.set` per session: a `row-badge` (`{items: [...]}` -- one
   colored, clickable PR icon per repo with an OPEN/draft PR; merged-only repos
   are omitted, since the badge is an actionable indicator) and a `pane`
   (`{title, default_location, icon, blocks: [...]}` -- the in-session GitHub
   tool-window listing, per PR, a headline row, a review-state row, a Checks
   section, and an unresolved-comments section; `icon` is a lucide name for its
   activity-bar button). The pane ends with an `action` block ("Refresh") whose
   click POSTs back to the host, which forwards `github.refresh` to this worker.

Rate limits: the user token's budgets (REST 5000 req/hr, GraphQL 5000 points/hr)
are shared with the user's own `gh` usage, so the worker spends as little as it
can. A REST conditional request (ETag / `If-None-Match`) is the primary poll: a
`304 Not Modified` means nothing changed and does NOT count against the primary
rate limit, so a steady state where nothing changed costs ~0. GraphQL (which has
no `304`) fires only when the conditional check reports a change, on a forced
refresh, or when the cached rich result is older than its freshness ceiling. That
ceiling exists because the `/pulls` list ETag does not reliably bump when a CI
check completes or a review thread changes, so CI/review state could otherwise go
stale indefinitely between PR-list changes. It is state-aware: a branch whose
cached state is active (a CI check running or queued) uses a short ceiling so a
finishing CI run shows up on the next tick, while a terminal or awaiting-review
branch uses the 300s ceiling (awaiting review can sit idle for days, so polling it
every tick would waste budget). Either way a click on Refresh updates immediately.
The GraphQL query reads `rateLimit { cost remaining resetAt }` and trips a short
backoff (serving the last-good cached result, honoring `resetAt`) when the budget
runs low or a `403`/`429`/`RATE_LIMITED` is returned.

Worst-case math (every key changes every tick, so each spends one REST request,
and same-repo branches batch into one GraphQL query per repo): for N unique
`(owner, repo, branch)` keys at a T-second network tick, that is `N * 3600 / T`
REST req/hr; the GraphQL queries scale with the number of distinct repos (each
capped at `MAX_GRAPHQL_ALIASES` branches per query), not N. At the 120s default,
a 20-worktree single-repo workspace tops out around 600 REST req/hr but only ~60
GraphQL queries/hr (two batched queries per tick), both a small fraction of
5000/hr. The realistic steady state is far cheaper: most ticks are a `304`, so the
REST cost is ~0 and no GraphQL fires. The fast local session tick (a couple
seconds, no network) is separate and unaffected by `ui_refresh_secs`.

When a user-initiated refresh (the pane's Refresh action) hits an active backoff,
the worker raises one in-app `ui.notify` (a warning, "GitHub rate limited") via
the `notifications` capability, at most once per backoff window. The body shows
the reset countdown ("Resets in ~Xm (HH:MM)") when the GraphQL `resetAt` is
known, and a generic message for the REST path, which has no real reset. Background
polls never notify, so a rate-limited workspace is not nagged on every tick.

Each push is `params: { slot, id, session_id, payload }`. A badge item is
`{ icon, tone?, href?, tooltip? }` (`icon` is a lucide name, e.g.
`git-pull-request-arrow`; `tone` colors it; `href` opens the PR). A pane block
is one of a small, extensible set (`heading`, `row`, `note`, `divider`,
`section`, `action`, `comment`) -- a `row` is
`{ label, value?, sublabel?, icon?, tone?, color?, href? }`, a `comment` is
`{ author, body, path?, line?, resolved?, href? }` (read-only), and an `action`
is `{ label, method, icon? }` (a button that forwards `method` to this worker).
The host renders the block kinds it knows and ignores the rest, so the pane can
grow without a lockstep host change. `tone` is one of the host's `Tone` set
(`neutral`, `info`, `success`, `warn`, `danger`): a non-draft open PR is
`success`, a draft `warn`, a hard error (auth/rate-limit/network) `danger`. A
merged PR has no semantic tone, so its headline row carries a validated hex
`color` (`#8957e5`, GitHub purple) instead; `color` accepts only `#rgb`/`#rrggbb`
literals so it can never carry arbitrary CSS. When no token is present the pane
prepends a warn `note` telling the user a token unlocks review/CI/comments/merged.
The host replies on stdin; the worker ignores the reply (a push is best-effort).

The network poll interval comes from the `ui_refresh_secs` setting, which the
worker reads at startup via the `config.get` host RPC (`agent-of-empires#2399`).
Precedence: the setting, else the `AOE_GITHUB_UI_REFRESH_SECS` env override,
else 120s (sized for the rate-limit budget above); `0` disables the background
poll (startup and refresh pushes still happen). Unlike a push, the startup `config.get` blocks for its reply, which is
safe: the host always answers a worker call (an unknown method comes back as an
error, not silence).

> Rendering these in the TUI / web UI is host-side and lands with the core
> plugin UI slots (`agent-of-empires#2366`) over the worker protocol
> (`agent-of-empires#2095`); this repo ships the data those slots consume. The
> `ui.state.set` params shape and the slot strings above track #2366's D9
> design; that section is not merged yet, so expect a rebase if the contract
> shifts.

## Developing

Uses [uv](https://docs.astral.sh/uv/). The one runtime dependency is `httpx`;
`dev` brings ruff, mypy, pytest, and pre-commit.

```sh
uv sync                       # create the env
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format --check .  # format
uv run mypy src               # type-check
uv run pre-commit install     # enable git hooks (ruff, mypy, conventional commits)
```

The worker speaks ndjson JSON-RPC: one JSON object per line in, one per line
out. Drive a handler without aoe:

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"github.status","params":{"args":{"path":"."}}}' \
  | uv run aoe-github-worker
echo '{"jsonrpc":"2.0","id":2,"method":"github.open","params":{"args":{"path":"."}}}' \
  | uv run aoe-github-worker
```

Token resolution order: `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token`
(only when `gh` is installed and authenticated). `gh` is an optional source,
never required; without a token the worker still shows open PRs (the basic REST
view), but review state, CI checks, unresolved comments, and merged PRs need a
token, since they come from the authenticated GraphQL query.

## Releases

Tagging `vX.Y.Z` runs the checks and publishes a GitHub Release with a source
archive and its content hash (`.github/workflows/release.yml`). On a successful
publish the `featured-pr` job then opens a PR on `agent-of-empires/agent-of-empires`
that pins the release's source tree hash in `plugins/featured.toml`, marking it
trusted. It no-ops if the version is already pinned.

That job needs an `AOE_FEATURED_PR_TOKEN` secret (a fine-scoped PAT or App token
with contents and pull-request write on `agent-of-empires/agent-of-empires`); it
is skipped with a warning if the secret is unset. It runs behind the
required-reviewer `release` environment, so it waits for maintainer approval
before the cross-repo PR opens.

## Discovery

This repository is tagged with the `aoe-plugin` GitHub topic so it shows up in
the in-app plugin discovery. Featured (curated) status is granted separately by
the AoE maintainers via the embedded featured index.

## License

MIT. See [LICENSE](LICENSE).

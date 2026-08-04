# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request state alongside your agent sessions, and (later) drive the
common git/GitHub operations from #658 without dropping into a terminal.

> Status: **read operations**. This release ports the GitHub client + token-auth
> layer from AoE core (PR #1681 / issue #1667) into a Tier 1 plugin worker, with
> structured `github.status` (open PRs for the branch, each with its URL) and
> `github.open` (open-in-GitHub). The per-session pane shows rich PR state
> (state incl. merged, review state, merge conflicts, CI checks, and unresolved comments) when a
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
`runtime.worker`, `session.read`, `notifications`, `browser_open`) before
anything is written.

## What it contributes

| Kind    | Detail                                                             |
| ------- | ------------------------------------------------------------------ |
| Command | `status` -> worker method `github.status`                          |
| Command | `refresh` -> worker method `github.refresh`                        |
| Command | `open` (open-in-GitHub) -> worker method `github.open`             |
| Command | `open_pr` (`GitHub: open PR`) -> client `open-ui-link` action, default chord `Ctrl+Shift+G` |
| UI      | a `row-badge` (`github_pr_badge`), a `row-column` status cell (`github_pr_status`), a `sort-key` (`github_pr_attention`), and a `pane` tool-window (`github_pane`) |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

`open_pr` is the command-palette / shortcut way to open the active session's PR
(requires a host on api_version 6+). It is a client action, not a worker call:
the host opens the PR `href` the worker already publishes on the `github_pr_badge`
row-badge slot, synchronously in the keypress or palette click, so it works on a
remote web dashboard without a popup-blocked async open. The badge href is the
highest-attention PR and is present whenever an open PR exists, independent of the
`show_status_text` setting.

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
a UI refresh (below). When its params carry a `session_id` (a clicked pane is
one session), the refresh is scoped to that session; without one it covers every
session.

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
`github.refresh`, and on a background poll it runs one refresh. A `github.refresh`
that names a `session_id` refreshes only that session and pushes additively (it
never prunes another session's slots); startup, background polls, and a
session-less `github.refresh` cover every session and prune vanished ones:

1. `sessions.list` (host RPC) -> every active session and its workspace
   `project_path`. Archived and snoozed sessions are skipped: they are inactive,
   so polling them would only spend GitHub quota for no UI change.
2. For each workspace, discover the git checkouts: the workspace root plus each
   immediate child directory that is its own checkout (worktree-safe, a
   worktree's `.git` is a file, so discovery asks `git rev-parse --show-toplevel`
   rather than looking for a `.git` directory). Immediate child submodules are
   skipped by default; turn off `ignore_submodules` to include them again. The
   workspace root is still scanned when it is itself a submodule.
3. Resolve each checkout to `(owner, repo, branch)`, deduplicate (a branch shared
   across workspaces is fetched once), and look up its PRs. Local git identity
   and the REST lookups fan out on small bounded thread pools so a big workspace
   set refreshes in seconds; GraphQL queries stay serial to stay clear of
   GitHub's secondary/concurrency limits.
   With a token the per-branch lookup is a cheap REST conditional check
   first (see "Rate limits" below); only when that reports a change (or on a
   forced refresh, a cold cache, or a cheap digest query detecting a change past
   the freshness ceiling) does it spend the expensive
   GraphQL query for the rich state (PR state incl. MERGED, `reviewDecision`,
   mergeability (merge conflicts with the base), the
   head commit's check rollup + per-check runs, and every unresolved review
   thread with its first comment). Branches of the same repo that need a fresh
   fetch are aliased into one batched GraphQL query rather than one per branch, so
   a workspace of many worktrees of one repo costs a single query (split into a
   few once it exceeds the per-query alias cap). Without a token it is the basic
   REST open-PR lookup only.
4. Push one global `sort-key` and three `ui.state.set` per session: a `row-badge` (`{items: [...]}` -- a
   chip sequence per open PR: a clickable PR icon, a merge-conflict chip when the
   PR conflicts with its base, then review-state, CI-rollup,
   and unresolved-comment chips, each shown only when a token supplies that field,
   concatenated across the workspace's repos, plus an error marker per failed
   repo. Each chip is colored by tone, so failing CI / changes requested read as
   danger, unresolved comments as warn, and a healthy PR as success,
   distinguishing a broken PR from a healthy one at a glance; a draft shows the PR
   chip alone and merged-only repos are omitted, since the badge is an actionable
   indicator), a `row-column`
   (`{text, tone, tooltip, sort_value?}`, or `{}` to clear -- one deterministic
   words summary of the session's most-urgent PR signal so the list is scannable
   without hovering the badge; a multi-repo workspace collapses to its single
   highest-attention candidate, with the pane keeping the per-repo breakdown;
   `sort_value` lets the dashboard sort sessions by GitHub PR attention, even
   when `show_status_text` hides the visible words), and
   a `pane`
   (`{title, default_location, blocks: [...], footer}` -- the in-session GitHub
   tool-window, described in [The pane](#the-pane) below. Its `action` blocks POST
   back to the host, which forwards the named method (`github.refresh`,
   `github.select_pr`) to this worker).

On restart: each successful full refresh is persisted to an on-disk cache
(`${XDG_CACHE_HOME:-~/.cache}/agent-of-empires/github-plugin/snapshot.json`). On
startup the worker repaints that last-known snapshot (marked stale, filtered to
sessions that still exist) before its first network refresh runs, so a restarted
`aoe serve` shows data immediately instead of a blank pane while the cold serial
fan-out completes. The cache is fail-soft: a missing or corrupt file is ignored.

Rate limits: the user token's budgets (REST 5000 req/hr, GraphQL 5000 points/hr)
are shared with the user's own `gh` usage, so the worker spends as little as it
can. A REST conditional request (ETag / `If-None-Match`) is the primary poll: a
`304 Not Modified` means nothing changed and does NOT count against the primary
rate limit, so a steady state where nothing changed costs ~0. GraphQL (which has
no `304`) is two-tier: the expensive rich query (per-check runs, review threads,
comment excerpts; roughly 25-30 points per 10-branch batch) fires only when the
conditional check reports a change, on a forced refresh, on a cold cache, or when
the cheap DIGEST query detects a change. The digest (~1 point per batch) carries
just the change-detection fields (PR state, `reviewDecision`, `updatedAt`, head
commit, check rollup state, review-thread count) and revalidates cached rich
state past its freshness ceiling; a matching signature serves the cache and
re-arms the window, so an idle workspace revalidates for pennies instead of
re-paying the rich query every 5 minutes. The ceilings exist because the `/pulls`
list ETag does not reliably bump when a CI check completes or a review thread
changes. They are state-aware: a branch whose cached state is active (a CI check
running or queued) digests on a short ceiling so a finishing CI run shows up on
the next tick, while a terminal or awaiting-review branch uses the 300s ceiling.
Because the digest cannot see everything the pane renders (a comment body edit,
a per-check change under an unchanged rollup), a hard full-refresh ceiling
(2h idle, 5m active) bounds how long a full fetch can be deferred. Either way a
click on Refresh updates immediately. Every GraphQL response's
`rateLimit { cost remaining resetAt }` is logged to stderr (the per-worker log)
and tracked: a low remaining budget stretches the background ceilings before the
hard backoff floor trips, any non-rate query failure (digest or full, warm key
or cold) cools that key down before it may retry so a persistent error cannot
re-spend a query every tick, and a `403`/`429`/`RATE_LIMITED` (or a nearly spent
budget) trips a short backoff serving the last-good cached result, honoring
`resetAt`.

Worst-case math (every key changes every tick, so each spends one REST request,
and same-repo branches batch into one GraphQL query per repo): for N unique
`(owner, repo, branch)` keys at a T-second network tick, that is `N * 3600 / T`
REST req/hr; the GraphQL queries scale with the number of distinct repos (each
capped at `MAX_GRAPHQL_ALIASES` branches per query), not N. At the 120s default,
a 20-worktree single-repo workspace tops out around 600 REST req/hr and ~60
batched GraphQL queries/hr. The realistic steady state is far cheaper: most ticks
are a `304`, so the REST cost is ~0 and GraphQL spends ~1 point per repo per
ceiling expiry on the digest (a large multi-repo workspace holds around a couple
hundred points/hr where the pre-digest design burned its way through the entire
5000/hr budget). The fast local session tick (a couple seconds, no network) is
separate and unaffected by `ui_refresh_secs`.

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
`section`, `action`, `comment`, `callout`, `bar`, `columns`); the full field list
per kind is in the host's
[plugin API reference](https://github.com/agent-of-empires/agent-of-empires/blob/main/docs/plugin-api.md#pane-payload).
The host renders the block kinds it knows and ignores the rest, so the pane can
grow without a lockstep host change; the kinds used here need `api_version >= 12`,
which is why the manifest declares it. `tone` is one of the host's `Tone` set
(`neutral`, `info`, `success`, `warn`, `danger`): a non-draft open PR is
`success`, a draft `warn`, a hard error (auth/rate-limit/network) `danger`. A
merged PR has no semantic tone, so its selector row carries a validated hex
`color` (`#8957e5`, GitHub purple) instead; `color` accepts only `#rgb`/`#rrggbb`
literals so it can never carry arbitrary CSS. When no token is present the pane
prepends a warn `note` telling the user a token unlocks review/CI/comments/merged.

### The pane

The pane is one-PR-focused, and reads top to bottom:

1. **PR selector.** Every PR in the session, most-actionable first (the same
   attention ladder the session row uses, with merged PRs sinking below every open
   one). Each row carries `#number`, the title, `branch · author`, and a glyph
   strip for CI / review / conflicts / unresolved-comment count. Clicking a row
   fires `github.select_pr { pr }`; the trailing arrow is a separate link to
   GitHub, so picking a PR never navigates away. The selection is remembered in
   memory per session, and falls back to the top of the list whenever the PR it
   named is no longer in the snapshot (merged, closed, renamed).
2. **Merge verdict** (`callout`). Why the PR can or cannot merge, derived here
   rather than fetched: `mergeStateStatus` collapses conflicts, policy, CI and
   review into one enum and so cannot say which is blocking. The ladder is
   draft, conflicts, changes requested, failing checks, running checks, unresolved
   comments, ready, awaiting review, ordered by who can clear the block. **This
   plugin never merges for you:** a mergeable PR gets a link out to GitHub, and a
   blocked one an inert button naming the block.
3. **Review.** One row per reviewer (initials, name, current position), from
   `latestReviews` plus any outstanding `reviewRequests`, under the summary GitHub
   itself would show. Degrades to the single aggregate review-state row when
   per-reviewer data is absent.
4. **Checks.** Count pills in the header; the runs needing attention (failing,
   running, queued) listed outright with `workflow · duration`; the passes folded
   into a collapsed group; skipped runs on their own line. The body scrolls in
   place, so a twenty-check repo does not bury everything below it. A skipped run
   still counts as passing for the rollup (it blocks nothing) but is listed apart
   from the real passes.
5. **Unresolved comments**, as read-only `comment` blocks.
6. **Diff and Linked** (`columns`), side by side: `+added / -removed` with a
   proportional bar and a file count, and the issues the PR closes on merge (from
   GitHub's own `closingIssuesReferences`). Either can be absent, and a lone
   survivor spans the full width.
7. **Activity.** Recent timeline events, newest first, with local wall-clock times.
8. **Footer**, pinned below the scroll: the last refresh time and the verdict in a
   word.

Repos that contribute no PR (a failed lookup, a non-GitHub checkout, a clean
branch) collect in a folded "Other repos" section, or render outright when they are
all there is to say.
The host replies on stdin; the worker ignores the reply (a push is best-effort).

The network poll interval comes from the `ui_refresh_secs` setting, which the
worker reads at startup via the `config.get` host RPC (`agent-of-empires#2399`).
Precedence: the setting, else the `AOE_GITHUB_UI_REFRESH_SECS` env override,
else 120s (sized for the rate-limit budget above); `0` disables the background
poll (startup and refresh pushes still happen). Unlike a push, the startup `config.get` blocks for its reply, which is
safe: the host always answers a worker call (an unknown method comes back as an
error, not silence).

`ignore_submodules` defaults on. It removes initialized child submodules from
workspace discovery so dependency checkouts do not spend GitHub API budget or add
noise to the session row and pane. Set it to `false` when a workspace intentionally
tracks PR state for immediate child submodule checkouts.

The advanced `ci_required_checks_only` setting changes the row-level CI rollup
to use only classic branch-protection required checks. Optional failures remain
listed in the GitHub pane. Repos whose required-check metadata cannot be read
fall back to the all-check rollup.

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

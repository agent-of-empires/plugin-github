# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request state alongside your agent sessions, and (later) drive the
common git/GitHub operations from #658 without dropping into a terminal.

> Status: **read operations**. This release ports the GitHub client + token-auth
> layer from AoE core (PR #1681 / issue #1667) into a Tier 1 plugin worker, with
> structured `github.status` (open PRs for the branch, each with its URL) and
> `github.open` (open-in-GitHub). The per-session pane shows rich PR state
> (state incl. merged, review state, merge conflicts, merge queue position and
> state, CI checks, and unresolved comments) when a token is present, degrading
> to open PRs only without one. The write operations (create/merge PR, push, pull,
> fix-CI) land in follow-ups.

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
| UI      | a `row-badge` (`github_pr_badge`), a `row-column` status cell (`github_pr_status`), a `sort-key` (`github_pr_attention`), six `filter-facet`s (`github_ci`, `github_review`, `github_pr`, `github_comments`, `github_conflicts`, `github_queue`), and a `pane` tool-window (`github_pane`) |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

### GitHub suggestions

On Agent of Empires 1.21.0+ (SDK API 20), this plugin publishes host-managed
suggested sessions, independent of AoE sessions. Each source below is one
criterion declared in the manifest's `[suggestions]` block. The host owns the
per-profile on/off switch for every criterion. Before each discovery the worker
asks the host which are on and makes no GitHub request for one that is off; if
the host does not answer, the worker publishes `suggestions.error` and queries
nothing.

| Criterion | What it finds | GitHub query |
|---|---|---|
| `changes_requested` | Your open PRs with a current changes-requested verdict on the head commit | `is:open is:pr author:@me` |
| `review_requested` | Open PRs where your review is requested | `is:open is:pr review-requested:@me` |
| `re_review` | PRs you reviewed that got new commits since, without a formal re-request | `is:open is:pr reviewed-by:@me -author:@me -review-requested:@me` |
| `review_thread_reply` | Your open PRs with unresolved review threads where someone else commented last | shares the `author:@me` search |
| `merge_conflict` | Your open PRs that conflict with their base branch | shares the `author:@me` search |
| `ci_failed` | Your open PRs whose head commit has failed or errored checks | shares the `author:@me` search |
| `mentioned` | Unread issue or PR comments that mention you or your team | `GET /notifications` (ETag-cached, so an unchanged inbox is free) |
| `assigned_issues` | Open issues assigned to you | `is:open is:issue assignee:@me` |
| `new_issues` | Issues opened by others in the last 7 days | `is:open is:issue -author:@me created:>=<date>` over the repositories below |

The four `author:@me` criteria run one search and publish one item per PR; its
criterion is the first match in the order above and its reason lists every
match. Items are ranked in that table order, then by recency, and deduplicated
by source URL. Every item carries `variables` for the host's prompt templates
(`repo`, `org`, `title`, `url`, `timestamp`, plus criterion-specific names); the
manifest lists them per criterion, and an authored-PR variable that did not
match is an empty string.

Scope: `suggestion_repositories` (comma-separated `owner/repo`) and
`suggestion_organizations` restrict every criterion; empty means everything
the token can see. `new_issues` needs a repository list: the allowlist or
organizations when set, else the repositories of your registered AoE projects
(read from each project's `origin` remote via the host's `projects.list`).
`suggestion_ignore` (comma-separated organizations such as `acme` and
repositories such as `acme/widgets`, case-insensitive) hides those sources from
every criterion and wins over both allowlists. Searches also carry matching
`-org:`/`-repo:` qualifiers, as many as fit GitHub's query limit, so ignored
results do not use up the search budget.
Each item carries a `mapping_key` (the lowercased `owner/repo`); the project an
item starts in comes from the mapping you set in AoE's suggestion mappings, not
from this plugin.
`suggestion_limit` caps the published snapshot (1-500) after a complete fetch.
Each item's `source_ref` is a short reference such as `acme/widgets#123`, which
AoE shows on suggestion rows when its "Show source reference" setting is on.
`suggestion_ref_org`, `suggestion_ref_repo` and `suggestion_ref_number` choose
its parts (`widgets#123` without the owner); with all three off it stays whole.
A long allowlist is split across several searches, each within GitHub's 256-character query limit.

Discovery runs at startup, on the host's `suggestions.refresh`, and on the
`ui_refresh_secs` tick. Searches and review histories are fetched to
completion (10 pages of 50, 10 pages of 100 reviews, review threads and check
contexts per PR); exceeding a cap, a
GraphQL error, or a rate limit reports `suggestions.error`, and the host keeps
its last good snapshot. The notification inbox is read newest first and stops
after 500 entries. A rate limit arms the plugin's shared backoff gate for the
affected budget (GraphQL searches or REST notifications), so the next refresh
waits for it to reset. Dismissing a `mentioned` item in AoE does not mark the
GitHub notification read.

Setup checks: at startup, on the host's `suggestions.checks.run` and on every
`suggestions.refresh` (so a sources or scope change is never stale), the
worker reports four checks with `suggestions.checks.set`: `gh` installed (only
a warning when `GITHUB_TOKEN` or `GH_TOKEN` is set), signed in, the configured
repositories and organizations visible to the token, and the token able to
read notifications (ok as not needed while the `mentioned` source is off, a
warning when the host does not say). One batched GraphQL request covers the
sign-in check and up to 200 scope entries (the rest is reported, not checked);
an entry that fails the `owner/repo` grammar is ignored and reported. The
notifications probe is `GET /notifications?per_page=1`. With no token nothing
is sent to GitHub, and a probe skipped by the rate-limit backoff reports
`warn`. The checks are not repeated on the `ui_refresh_secs` tick.

Suggestion text from GitHub is source material, not trusted instructions. The
provider proposes a task prompt but never creates sessions. Each draft prompt is
fixed text that names only the PR or issue URL and starts with an untrusted-content
notice; GitHub-controlled strings (titles, logins, check names, SHAs) live in
`reason` and `variables`, never in the prompt prose.

`open_pr` is the command-palette / shortcut way to open the active session's PR
(requires a host on api_version 6+). It is a client action, not a worker call:
the host opens the PR `href` the worker already publishes on the `github_pr_badge`
row-badge slot, synchronously in the keypress or palette click, so it works on a
remote web dashboard without a popup-blocked async open. The badge href is the
highest-attention PR and is present whenever an open PR exists, independent of the
`show_status_text` setting.

### Works with the Board plugin

Compatible with the [Board](https://github.com/agent-of-empires/plugin-kanban)
(kanban) plugin. Its Cards show the PR badges and status text as the sidebar
does, and its Signal columns can match on the six filter facets below, e.g. a
column holding every session where `GitHub CI is failing` or `GitHub review is
changes requested`.

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
2. Prefer the host's explicit `workspace_repos` list: each entry supplies a
   source checkout path and the branch attached to this session. This branch
   is authoritative even when the source checkout's current branch differs.
   Metadata paths must name checkout roots (including worktrees and symlink
   aliases); stale paths and repository subdirectories are skipped. If no
   metadata entry names a checkout, use shallow discovery under
   `project_path`: the workspace root and immediate child checkouts. Immediate
   child submodules are skipped by default; turn off `ignore_submodules` to
   include them.
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
   mergeability (merge conflicts with the base), merge queue membership/state/
   position, the head commit's check rollup + per-check runs, and every unresolved
   review thread with its first comment). Branches of the same repo that need a fresh
   fetch are aliased into one batched GraphQL query rather than one per branch, so
   a workspace of many worktrees of one repo costs a single query (split into a
   few once it exceeds the per-query alias cap). Without a token it is the basic
   REST open-PR lookup only.
4. Push one global `sort-key`, six global `filter-facet`s (below), and three `ui.state.set` per session: a `row-badge` (`{items: [...]}` -- a
   chip sequence per open PR: a clickable PR icon, a merge-conflict chip when the
   PR conflicts with its base, a merge-queue status/position chip when queued, then
   review-state, CI-rollup, and unresolved-comment chips, each shown only when a
   token supplies that field, concatenated across the workspace's repos, plus an error marker per failed
   repo. Each chip is colored by tone, so failing CI / changes requested read as
   danger, unresolved comments as warn, and a healthy PR as success,
   distinguishing a broken PR from a healthy one at a glance; a draft shows the PR
   chip alone and merged-only repos are omitted, since the badge is an actionable
   indicator), a `row-column`
   (`{text, tone, tooltip, sort_value?, filter_values?}`, or `{}` to clear -- one deterministic
   words summary of the session's most-urgent PR signal so the list is scannable
   without hovering the badge; a multi-repo workspace collapses to its single
   highest-attention candidate, with the pane keeping the per-repo breakdown;
   `sort_value` lets the dashboard sort sessions by GitHub PR attention and
   `filter_values` lets it filter them by PR fact, both even when
   `show_status_text` hides the visible words), and
   a `pane`
   (`{title, default_location, blocks: [...], footer}` -- the in-session GitHub
   tool-window, described in [The pane](#the-pane) below. Its `action` blocks POST
   back to the host, which forwards the named method (`github.refresh`,
   `github.select_pr`) to this worker).

Filter facets: each session's `row-column` carries `filter_values`, the sorted
union of facts across every open (non-merged) PR in the workspace, drafts
included. They are data, not display, so the session-row chip toggles do not
affect them. The six global `filter-facet` entries point at that column
(`column: "github_pr_status"`) and list the values a user can pick; a facet
matches a session when its `filter_values` holds any selected value, so the
dashboard sidebar and other plugins (such as the Board) can filter sessions by
"CI failing" without a host change:

| Facet id          | Label           | Values                                                                                                   |
| ----------------- | --------------- | -------------------------------------------------------------------------------------------------------- |
| `github_ci`       | GitHub CI       | `ci:passing`, `ci:failing`, `ci:running`, `ci:queued`, `ci:unknown` (only when the PR has a check rollup) |
| `github_review`   | GitHub review   | `review:approved`, `review:partially-approved`, `review:changes-requested`, `review:changes-pushed`, `review:commented`, `review:waiting` |
| `github_pr`       | GitHub PR       | `pr:open`, `pr:draft`                                                                                     |
| `github_comments` | GitHub comments | `comments:unresolved` (at least one unresolved review thread)                                            |
| `github_conflicts` | GitHub conflicts | `conflicts:yes` (the PR conflicts with its base)                                                        |
| `github_queue`    | GitHub merge queue | `queue:queued`, `queue:awaiting-checks`, `queue:mergeable`, `queue:locked`, `queue:unmergeable` (only while the PR is in a merge queue) |

Each option is `{ value, label, tone }`, with the label and tone matching the
pane's CI and review vocabulary. Without a token only the `pr:*` values exist.
A session with no open PR has no `row-column` entry, so it matches no facet.

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
just the change-detection fields (PR state, `reviewDecision`, `updatedAt`, head commit, merge queue membership/state/position, check rollup state, review-thread count) and revalidates cached rich
state past its freshness ceiling; a matching signature serves the cache and
re-arms the window, so an idle workspace revalidates for pennies instead of
re-paying the rich query every 5 minutes. The ceilings exist because the `/pulls`
list ETag does not reliably bump when a CI check completes or a review thread
changes. They are state-aware: a branch whose cached state is active (a CI check
running or queued, or a PR in a merge queue) digests on a short ceiling so
finishing checks and queue transitions show up on the next tick, while a terminal or awaiting-review branch uses the 300s ceiling.
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

1. **PR selector.** Every PR in the session plus any additional members of its
   GitHub-native stacks, most-actionable first (the same attention ladder the
   session row uses, with merged PRs sinking below every open one). Each row
   carries `#number`, the title, `branch · author`, and a glyph strip for CI /
   review / conflicts / merge-queue state and position / unresolved-comment
   count. Clicking a row fires `github.select_pr { pr }`; the trailing arrow is a
   separate link to GitHub, so picking a PR never navigates away or changes a
   checkout. The selection is remembered in memory per session, and falls back
   to the top of the list whenever the PR it named is no longer in the snapshot
   (merged, closed, renamed).
2. **Stack map.** GitHub's native stack membership and ordering show the selected
   PR's position and provide one-click selection of every layer. The readiness
   row covers only unmerged PRs from the bottom through the selected PR: GitHub
   must report each as `clean` (or already merged). A draft, `dirty`, or
   `blocked` state blocks readiness. Advisory `behind`, `unstable`, and
   `has_hooks` states remain visible but leave readiness unknown; missing status
   is also unknown.
   A fully merged range reads `Already merged`; closed, unmerged members are
   blocked and do not count as open PRs or show an inferred review verdict.
   PRs above the selected one do not affect its readiness. Stack data is fetched
   only when a token is available. Successful data is rechecked after five
   minutes; on failures, the last stack data may remain available and the pane
   freshness is marked stale. Manual refresh bypasses the freshness TTL only
   when no REST backoff or stack retry cooldown is active; data may be absent
   when GitHub does not return it.
3. **Merge verdict** (`callout`). Why the PR can or cannot merge, derived here
   rather than fetched: `mergeStateStatus` collapses conflicts, policy, CI and
   review into one enum and so cannot say which is blocking. The ladder is
   draft, merge queue state, conflicts, changes requested, awaiting re-review, failing checks, running
   checks, unresolved comments, awaiting more approvals, ready, awaiting review,
   ordered by who can clear the block. "Awaiting more approvals"
   (`partially approved` in the row) is an approved PR that still has a requested
   reviewer or team, or that GitHub blocks while every check is green; only a PR
   nobody else is being asked to review reads "ready". "Awaiting re-review" (`pushed since review` in the row) is a
   changes-requested decision whose blocking reviews were all made on an older
   commit; it still blocks the merge on GitHub until the reviewer re-reviews or
   dismisses, but reads as info, not an alarm. A queued PR is never shown as
   directly mergeable. **This plugin never merges for you:** queued PRs link to
   the merge queue on GitHub; a directly mergeable PR gets a merge link, and a
   blocked one gets an inert button naming the block.
4. **Review.** One row per reviewer (initials, name, current position), from
   `latestReviews` plus any outstanding `reviewRequests`, under the summary GitHub
   itself would show. Degrades to the single aggregate review-state row when
   per-reviewer data is absent.
5. **Checks.** Count pills in the header; the runs needing attention (failing,
   running, queued) listed outright with `workflow · duration`; the passes folded
   into a collapsed group; skipped runs on their own line. The body scrolls in
   place, so a twenty-check repo does not bury everything below it. A skipped run
   still counts as passing for the rollup (it blocks nothing) but is listed apart
   from the real passes.
6. **Unresolved comments**, as read-only `comment` blocks.
7. **Diff and Linked** (`columns`), side by side: `+added / -removed` with a
   proportional bar and a file count, and the issues the PR closes on merge (from
   GitHub's own `closingIssuesReferences`). Either can be absent, and a lone
   survivor spans the full width.
8. **Activity.** Recent timeline events, newest first, with local wall-clock times.
9. **Footer**, pinned below the scroll: the last refresh time and the verdict in a
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

The advanced `ignored_commenters` setting (default `coderabbitai, codecov`) is a
comma-separated list of GitHub logins whose COMMENTED reviews and unresolved
threads never make a PR read "commented", so that state means a person
commented. The PR author's own comments (e.g. replies to review threads) never
count either. Their unresolved threads still count toward unresolved comments.

The advanced `approved_overrides_stale_block` setting (default off) makes a PR
read "approved" when a reviewer requested changes on an older commit and another
reviewer approved the current head, unless someone else is still requested, in
which case it reads "partially approved". Off, it stays "pushed since review" and
the reviewer summary notes the approval. GitHub still blocks the merge until the
stale review is re-done or dismissed.

"approved" means nobody else is still being asked to review. GitHub's
`reviewDecision` can read `APPROVED` while a code-owner group or ruleset
requirement is still unmet, so an approved PR with a requested reviewer or team,
or one `mergeStateStatus` reports `BLOCKED` while every check is green (drafts
excluded), reads "partially approved" (`review:partially-approved` facet). Merge
blockers other than review, such as failing CI, are not inferred from
`mergeStateStatus`.

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

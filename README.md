# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request state alongside your agent sessions, and (later) drive the
common git/GitHub operations from #658 without dropping into a terminal.

> Status: **read operations**. This release ports the GitHub client + token-auth
> layer from AoE core (PR #1681 / issue #1667) into a Tier 1 plugin worker, with
> structured `github.status` (open PRs for the branch, each with its URL) and
> `github.open` (open-in-GitHub). The write operations (create/merge PR, push,
> pull, fix-CI) land in follow-ups.

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

Installing prompts for the plugin's declared capabilities (`net-fetch`) before
anything is written.

## What it contributes

| Kind    | Detail                                                             |
| ------- | ------------------------------------------------------------------ |
| Command | `status` -> worker method `github.status`                          |
| Command | `refresh` -> worker method `github.refresh`                        |
| Command | `open` (open-in-GitHub) -> worker method `github.open`             |
| Setting | `show_in_status_bar`                                               |
| UI      | a `status-bar` (`github_status`) and `row-badge` (`github_pr_badge`) slot |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

At install/update the host runs the manifest's `[[runtime.build]]` steps in the
plugin directory: create an in-tree `.venv` and `pip install .` into it. The
worker then launches from the plugin-relative `.venv/bin/aoe-github-worker`, so
the daemon's PATH never decides whether it starts (#2406). Build steps are
scoped to macOS/Linux.

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
   across workspaces is fetched once), and look up the open PRs concurrently.
4. Push one `ui.state.set` per slot: a `row-badge` per session (summarizing that
   session's repos) and one global `status-bar`.

GitHub lookups are conditional (ETag / `If-None-Match`; a `304` does not count
against the rate limit) and a `403`/`429` trips a short backoff that serves
cached values, so a many-repo, many-session setup stays well under GitHub's
60 req/hr unauthenticated ceiling.

Each push is `params: { slot, id, payload }` (the per-session `row-badge` adds
`session_id`; the global `status-bar` omits it). `payload` is the host's
`TextPayload`: `{ text, tone?, tooltip? }`, where `tone` is one of the host's
`Tone` set (`neutral`, `info`, `success`, `warn`, `danger`). Per session the
tone is a severity cascade: `danger` (a hard error -- auth/rate-limit/network)
> `success` (an open non-draft PR) > `warn` (only drafts) > `neutral` (no PRs,
or only non-github checkouts). The host replies on stdin; the worker ignores the
reply (a push is best-effort).

The poll interval comes from the `ui_refresh_secs` setting, which the worker
reads at startup via the `config.get` host RPC (`agent-of-empires#2399`).
Precedence: the setting, else the `AOE_GITHUB_UI_REFRESH_SECS` env override,
else 300s; `0` disables the background poll (startup and refresh pushes still
happen). Unlike a push, the startup `config.get` blocks for its reply, which is
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
never required; requests fall back to unauthenticated when no token resolves.

## Releases

Tagging `vX.Y.Z` runs the checks and publishes a GitHub Release with a source
archive and its content hash (`.github/workflows/release.yml`). AoE maintainers
pin that hash in the featured index (#2364) to mark the release trusted.

## Discovery

This repository is tagged with the `aoe-plugin` GitHub topic so it shows up in
the in-app plugin discovery. Featured (curated) status is granted separately by
the AoE maintainers via the embedded featured index.

## License

MIT. See [LICENSE](LICENSE).

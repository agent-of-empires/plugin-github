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
| UI      | a `status-bar-segment` slot (`github_status`)                      |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

### Methods

`github.status` (and its alias `github.refresh`) is fail-soft: it always returns
a structured result, never a JSON-RPC error, so a status poll always has
something to render.

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

> Rendering these in the TUI / web UI is host-side and lands with the core
> plugin UI slots (`agent-of-empires#2366`) over the worker protocol
> (`agent-of-empires#2095`); this repo ships the data those slots consume.

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

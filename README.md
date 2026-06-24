# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request state alongside your agent sessions, and (later) drive the
common git/GitHub operations from #658 without dropping into a terminal.

> Status: **foundation**. This release ports the GitHub client + token-auth
> layer from AoE core (PR #1681 / issue #1667) into a Tier 1 plugin worker, with
> a real `github.status`. The write operations (create/merge PR, push, pull,
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

Installing prompts for the plugin's declared capabilities (`net-fetch`) before
anything is written.

## What it contributes

| Kind    | Detail                                                             |
| ------- | ------------------------------------------------------------------ |
| Command | `aoe github status` -> worker method `github.status`               |
| Action  | `refresh` (palette / keybindable) -> `github.refresh`              |
| Setting | `show_in_status_bar` (toggle)                                      |
| UI      | a `status-bar-segment` slot titled "GitHub"                        |
| Worker  | `aoe-github-worker`, ndjson JSON-RPC over stdio                    |

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

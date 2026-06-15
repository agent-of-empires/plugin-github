# GitHub plugin for Agent of Empires

GitHub integration for [Agent of Empires](https://github.com/agent-of-empires/agent-of-empires).
Surface pull request and issue state alongside your agent sessions.

> Status: scaffold. The worker returns placeholder text; the contribution
> wiring (command, action, setting, UI slot, Tier 1 worker) is complete and
> installable. Build the real integration into `worker.py`.

## Install

From the dashboard (Settings -> Plugins -> Discover) or the CLI:

```sh
aoe plugin install agent-of-empires/plugin-github
```

Installing prompts for the plugin's declared capabilities before anything is
written. This scaffold declares none.

## What it contributes

| Kind     | Detail                                                          |
| -------- | --------------------------------------------------------------- |
| Command  | `aoe github status` -> worker method `github.status`            |
| Action   | `refresh` (palette / keybindable) -> `github.refresh`           |
| Setting  | `show_in_status_bar` (toggle)                                   |
| UI       | a `status-bar-segment` slot titled "GitHub"                     |
| Worker   | `worker.py`, ndjson JSON-RPC over stdio                         |

## Developing

The worker speaks ndjson JSON-RPC: one JSON object per line in, one per line
out. Test a handler without aoe:

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"github.status","params":{}}' | ./worker.py
```

To add real GitHub calls, declare the `net-fetch` capability in
`aoe-plugin.toml` and implement the handlers in `worker.py`.

After editing the manifest, validate the tree hash matches what aoe will pin:

```sh
aoe plugin hash .
```

## Discovery

This repository is tagged with the `aoe-plugin` GitHub topic so it shows up in
the in-app plugin discovery. Featured (curated) status is granted separately by
the AoE maintainers via the embedded featured index.

## License

MIT. See [LICENSE](LICENSE).

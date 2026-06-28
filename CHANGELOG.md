# Changelog

All notable changes to the Agent of Empires GitHub plugin will be documented in this file.

The format follows [Conventional Commits](https://www.conventionalcommits.org/).

## [1.3.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.3.0) - 2026-06-28



### Bug Fixes

- **graphql:** Collapse same-named check runs to latest per name in [#38](https://github.com/agent-of-empires/plugin-github/pull/38) by [@Seluj78](https://github.com/Seluj78) ([`1176e3a`](https://github.com/agent-of-empires/plugin-github/commit/1176e3a4c78edf9920a95f6b5c92fd159ce66b79))
- **refresh:** Skip archived and snoozed sessions in proactive refresh in [#43](https://github.com/agent-of-empires/plugin-github/pull/43) by [@Seluj78](https://github.com/Seluj78) ([`417cb4b`](https://github.com/agent-of-empires/plugin-github/commit/417cb4b609ef168b80e1020fa37b54e6e99acc0c))


### Features

- **ci:** Open AoE featured-index PR after release in [#40](https://github.com/agent-of-empires/plugin-github/pull/40) by [@Seluj78](https://github.com/Seluj78) ([`cdc3cca`](https://github.com/agent-of-empires/plugin-github/commit/cdc3cca46e7b857163b40173b4eb6c4d344b3f48))
- **uistate:** Surface PR attention state in session rows in [#39](https://github.com/agent-of-empires/plugin-github/pull/39) by [@Seluj78](https://github.com/Seluj78) ([`f951aac`](https://github.com/agent-of-empires/plugin-github/commit/f951aac85f2304f5bef01f27726fc5f80e1de599))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.2.0...v1.3.0
## [1.2.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.2.0) - 2026-06-27



### Features

- **refresh:** Batch same-repo GraphQL queries, state-aware staleness, surface all comments in [#32](https://github.com/agent-of-empires/plugin-github/pull/32) by [@Seluj78](https://github.com/Seluj78) ([`b7ffd5e`](https://github.com/agent-of-empires/plugin-github/commit/b7ffd5ea9115de37c414205c00b2ca47b3c9fed8))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.1.0...v1.2.0
## [1.1.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.1.0) - 2026-06-27



### Features

- **refresh:** Notify in-app when a forced refresh is rate-limited in [#24](https://github.com/agent-of-empires/plugin-github/pull/24) by [@Seluj78](https://github.com/Seluj78) ([`265694f`](https://github.com/agent-of-empires/plugin-github/commit/265694f03bbc301fc889cc01a6b5d7f5f20f2285))
- **uistate:** Send full unresolved-comment list under new 64KB pane cap in [#30](https://github.com/agent-of-empires/plugin-github/pull/30) by [@Seluj78](https://github.com/Seluj78) ([`7b530fd`](https://github.com/agent-of-empires/plugin-github/commit/7b530fdb05b3339d2353c4945e337e4f4bb39494))


### Performance

- **refresh:** Conditional REST polling to cut GitHub rate-limit usage in [#27](https://github.com/agent-of-empires/plugin-github/pull/27) by [@Seluj78](https://github.com/Seluj78) ([`fcb2159`](https://github.com/agent-of-empires/plugin-github/commit/fcb21599d5e7da4eebd882112863b60c82280b3e))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.0.0...v1.1.0
## [1.0.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.0.0) - 2026-06-27



### Bug Fixes

- **manifest:** Conform aoe-plugin.toml to the merged #2093 contribution schema in [#4](https://github.com/agent-of-empires/plugin-github/pull/4) by [@Seluj78](https://github.com/Seluj78) ([`dce1226`](https://github.com/agent-of-empires/plugin-github/commit/dce12262c2086b7e9411bf9809406f3b941638ba))
- **plugin:** Change capabilities from 'net-fetch' to 'net' to comply with known capabilities by [@Seluj78](https://github.com/Seluj78) ([`e745413`](https://github.com/agent-of-empires/plugin-github/commit/e74541354ba848ff18201c9e749aa8a36b5f07c1))
- **uistate:** Suppress active review/CI/comments for merged PR in [#15](https://github.com/agent-of-empires/plugin-github/pull/15) by [@Seluj78](https://github.com/Seluj78) ([`bd9d7d6`](https://github.com/agent-of-empires/plugin-github/commit/bd9d7d6656fdfa7b5f62e5ceb72720bba23bb38a))


### Features

- Scaffold the Agent of Empires GitHub plugin by [@Seluj78](https://github.com/Seluj78) ([`96ef093`](https://github.com/agent-of-empires/plugin-github/commit/96ef093a835d63be8059d054d8655f2a5c228082))
- **worker:** GitHub client + auth foundation, packaging, and CI in [#1](https://github.com/agent-of-empires/plugin-github/pull/1) by [@Seluj78](https://github.com/Seluj78) ([`7cce10d`](https://github.com/agent-of-empires/plugin-github/commit/7cce10da9483507f72f12e4ec87d905eac5d7780))
- **worker:** Structured github.status + github.open (P2 read ops) in [#3](https://github.com/agent-of-empires/plugin-github/pull/3) by [@Seluj78](https://github.com/Seluj78) ([`1f9e228`](https://github.com/agent-of-empires/plugin-github/commit/1f9e22835cba4b420c2d29eaed57479cb075da79))
- **worker:** Proactively push PR status to host UI slots in [#5](https://github.com/agent-of-empires/plugin-github/pull/5) by [@Seluj78](https://github.com/Seluj78) ([`6efa8c2`](https://github.com/agent-of-empires/plugin-github/commit/6efa8c2ffeb35f9796d2d5f41422aad33a4548a7))
- Multi-session multi-repo GitHub UI refresh (+ manifest/worker fixes) in [#6](https://github.com/agent-of-empires/plugin-github/pull/6) by [@Seluj78](https://github.com/Seluj78) ([`352a981`](https://github.com/agent-of-empires/plugin-github/commit/352a981eca8473059dbc4f171f2570d70b28b07d))
- Dockable pane slot, fresh-on-session-change, and a Refresh button in [#7](https://github.com/agent-of-empires/plugin-github/pull/7) by [@Seluj78](https://github.com/Seluj78) ([`795c6d3`](https://github.com/agent-of-empires/plugin-github/commit/795c6d367f5e81be0bf32aa3aff63c639cff8e27))
- **pane:** Rich per-PR state (merged/review/CI/comments) via token-gated GraphQL in [#13](https://github.com/agent-of-empires/plugin-github/pull/13) by [@Seluj78](https://github.com/Seluj78) ([`4b66d82`](https://github.com/agent-of-empires/plugin-github/commit/4b66d8212709a4dca18185f35e78db490ea5fc04))

# Changelog

All notable changes to the Agent of Empires GitHub plugin will be documented in this file.

The format follows [Conventional Commits](https://www.conventionalcommits.org/).

## [2.0.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v2.0.0) - 2026-08-04



### Features

- **pane:** Rebuild the pane around the selected PR in [#86](https://github.com/agent-of-empires/plugin-github/pull/86) by [@Seluj78](https://github.com/Seluj78) ([`3ceb33c`](https://github.com/agent-of-empires/plugin-github/commit/3ceb33c8197742b7479ab8a947c741fb0e2ac046))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.8.0...v2.0.0
## [1.8.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.8.0) - 2026-07-16



### Features

- **github:** Surface PR merge conflicts in the badge and pane in [#81](https://github.com/agent-of-empires/plugin-github/pull/81) by [@Seluj78](https://github.com/Seluj78) ([`410cc20`](https://github.com/agent-of-empires/plugin-github/commit/410cc20dfe5fc5b2dfff95ec6158d78122fd1e64))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.7.2...v1.8.0
## [1.7.2](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.7.2) - 2026-07-06



### Bug Fixes

- **worker:** Exclude trashed sessions from sessions.list in [#75](https://github.com/agent-of-empires/plugin-github/pull/75) by [@Seluj78](https://github.com/Seluj78) ([`10f13b0`](https://github.com/agent-of-empires/plugin-github/commit/10f13b00670600436c5983efc13061c4536d7422))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.7.1...v1.7.2
## [1.7.1](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.7.1) - 2026-07-05



### Bug Fixes

- **pane:** Drop the per-pane icon, now redundant with icon_asset in [#64](https://github.com/agent-of-empires/plugin-github/pull/64) by [@Seluj78](https://github.com/Seluj78) ([`fbe1c82`](https://github.com/agent-of-empires/plugin-github/commit/fbe1c82b6e1bc4042fdd7c0021bbddacbc137e56))
- **refresh:** Meter REST and GraphQL rate-limit budgets separately, surface backoff in pane in [#65](https://github.com/agent-of-empires/plugin-github/pull/65) by [@Seluj78](https://github.com/Seluj78) ([`bd48cc1`](https://github.com/agent-of-empires/plugin-github/commit/bd48cc1e815561e3346ebbd18772248411142706))
- **refresh:** Clear empty row-column via remove instead of rejected ui.state.set in [#67](https://github.com/agent-of-empires/plugin-github/pull/67) by [@Seluj78](https://github.com/Seluj78) ([`9be992d`](https://github.com/agent-of-empires/plugin-github/commit/9be992d2707387fef37ee0a9d314a82cd8b839af))
- Repaint last-known GitHub data instantly on aoe serve restart in [#68](https://github.com/agent-of-empires/plugin-github/pull/68) by [@Seluj78](https://github.com/Seluj78) ([`859fed2`](https://github.com/agent-of-empires/plugin-github/commit/859fed254b0b960a74ad96cf9f87a81ebacbe0c6))
- **refresh:** Two-tier digest polling kills the GraphQL budget burn, bounded pools cut big-refresh latency in [#71](https://github.com/agent-of-empires/plugin-github/pull/71) by [@Seluj78](https://github.com/Seluj78) ([`2610965`](https://github.com/agent-of-empires/plugin-github/commit/26109651a679fbf33d407c8ba4039fd3e9adab64))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.7.0...v1.7.1
## [1.7.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.7.0) - 2026-07-04



### Features

- **manifest:** Identity icon (api_version 7) in [#60](https://github.com/agent-of-empires/plugin-github/pull/60) by [@Seluj78](https://github.com/Seluj78) ([`0e5a974`](https://github.com/agent-of-empires/plugin-github/commit/0e5a9741ee228aa69dd256c1312c1d967caf1592))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.6.1...v1.7.0
## [1.6.1](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.6.1) - 2026-06-30



### Bug Fixes

- **github:** Show pane Last refreshed in local time and advance it on background polls in [#58](https://github.com/agent-of-empires/plugin-github/pull/58) by [@Seluj78](https://github.com/Seluj78) ([`447ca8c`](https://github.com/agent-of-empires/plugin-github/commit/447ca8cefb00a5b8d834961117f0427bd687b3a0))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.6.0...v1.6.1
## [1.6.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.6.0) - 2026-06-29



### Features

- **refresh:** Show GitHub pane refresh freshness in [#53](https://github.com/agent-of-empires/plugin-github/pull/53) by [@Seluj78](https://github.com/Seluj78) ([`8d2170b`](https://github.com/agent-of-empires/plugin-github/commit/8d2170b27954567bd0503bdfe24e47cfaf2f17f4))
- **refresh:** Ignore git submodules by default in [#52](https://github.com/agent-of-empires/plugin-github/pull/52) by [@Seluj78](https://github.com/Seluj78) ([`14eff7c`](https://github.com/agent-of-empires/plugin-github/commit/14eff7c217ab36863f27fd8de0c7edb90936cdbd))
- **uistate:** Sort sessions by github pr attention in [#54](https://github.com/agent-of-empires/plugin-github/pull/54) by [@Seluj78](https://github.com/Seluj78) ([`4a7c290`](https://github.com/agent-of-empires/plugin-github/commit/4a7c290f93338203eefbbb6cf99229b7d75a7af6))
- **github:** Gate ci rollup on required checks in [#55](https://github.com/agent-of-empires/plugin-github/pull/55) by [@Seluj78](https://github.com/Seluj78) ([`ab23b24`](https://github.com/agent-of-empires/plugin-github/commit/ab23b24f3a10a48bba0510e2ace624f99dc42a86))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.5.0...v1.6.0
## [1.5.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.5.0) - 2026-06-28



### Features

- **refresh:** Scope a manual github.refresh to the clicked session in [#48](https://github.com/agent-of-empires/plugin-github/pull/48) by [@Seluj78](https://github.com/Seluj78) ([`880a46b`](https://github.com/agent-of-empires/plugin-github/commit/880a46be5192b252a10857730b8141304da3ea34))
- **commands:** Open_pr command + keybind to open the active session's PR in [#49](https://github.com/agent-of-empires/plugin-github/pull/49) by [@Seluj78](https://github.com/Seluj78) ([`b6cc62b`](https://github.com/agent-of-empires/plugin-github/commit/b6cc62b88e3ebcccaf5cbb757fd3d3d8170892df))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.4.0...v1.5.0
## [1.4.0](https://github.com/agent-of-empires/plugin-github/releases/tag/v1.4.0) - 2026-06-28



### Features

- **manifest:** Marketplace screenshots (api_version 5) in [#44](https://github.com/agent-of-empires/plugin-github/pull/44) by [@Seluj78](https://github.com/Seluj78) ([`2d1ed6f`](https://github.com/agent-of-empires/plugin-github/commit/2d1ed6fdcb36cca124a292d97cd43305cf8fa41d))


**Full Changelog**: https://github.com/agent-of-empires/plugin-github/compare/v1.3.0...v1.4.0
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




# GitHub Enterprise Server Implementation Plan

**Goal:** Implement PR #90 with its credential-routing review feedback.
**Architecture:** A frozen GitHubHost owns host facts. A runtime-cached trust map feeds handlers and refresh; every repository key, network budget, and cache includes the host.
**Tech Stack:** Python >=3.10, httpx, PyYAML, pytest, uv, Ruff, mypy.
**Spec:** ../specs/2026-09-08-github-enterprise-server-design.md
**Execution:** Inline, as requested by the user's implementation instruction.

## Global constraints

- Offline discovery from gh hosts.yml; preserve previous map on read/parse failure.
- No new plugin setting, RPC, schema probing, or login flow.
- GH_TOKEN precedes GITHUB_TOKEN on dotcom; GHES environment tokens require matching GH_HOST.
- Ignore unsupported ghe.com tenancy/localhost hosts. Canonical DNS names; no custom HTTPS ports.
- Never log/persist tokens or forward them across API origins; disable redirects.
- No em dashes in added text. Snapshot version 2.

## Review focus

- Two GHES hosts sharing owner/repo/branch: separate credentials, ETags, rich caches, cooldowns.
- Malformed or unreadable configuration during manual refresh: preserve trust for all consumers.
- One limited/low-budget host: other hosts retain normal requests and pacing, in either observation order.
- Stored gh token fallback: subprocess must not inherit shared token variables.
- Selected PR on an unauthenticated server: show that host's hint, including when no PR exists.

## Tasks

- [x] 1. Revise spec; test host parsing/discovery, token binding, and API origin rules before implementing hosts.py, auth.py, errors.py, client.py, and gitctx.py. Run focused tests and commit.
  Interfaces: GitHubHost.dotcom()/enterprise(name), discover_trusted_hosts(env, previous), parse_owner_repo(url, trusted) -> (host, owner, repo), resolve_token(env, host), GitHubClient(host=host, token=token).
- [x] 2. Add three-host refresh integration and per-host budget/backoff regression tests. Migrate RepoKey to (host, owner, repo, branch), group fetches by host, partition state, add snapshot version gate. Update existing assertions for the new contract. Run refresh tests and commit.
- [x] 3. Add GHES handler/runtime/pane tests. Cache discovery in Runtime, rediscover on manual refresh, share map with direct handlers; show selected-host auth hints and construct compare links with web_base. Run affected tests and commit.
- [x] 4. Update dependency lock, README, and net capability comment; inspect host API enforcement. Run uv run pytest, uv run ruff check ., uv run ruff format --check ., uv run mypy src, and git diff --check. Review complete diff before updating PR #90.

## Verification record

- Merged current main at 3d13c6f (v2.1.0), preserving its CI/review fixes and session filter facets.
- Full merged suite: 344 passed. Ruff lint and format checks, mypy (13 source files), and diff whitespace checks passed.
- Fresh review found an uncached GHES backoff error with a dotcom hint. The regression test failed first; passing client.host fixed it and the full suite passed.
- Separate integration review found no actionable issues. Live GHES compatibility remains unverified; HTTP coverage uses MockTransport.

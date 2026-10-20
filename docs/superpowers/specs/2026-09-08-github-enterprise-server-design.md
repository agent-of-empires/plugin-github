# GitHub Enterprise Server design

Date: 2026-09-08
Status: proposed for PR #90

## Scope and host identity

Support github.com and multiple GitHub Enterprise Server (GHES) hosts in one
worker. A frozen `GitHubHost` owns the host kind, API endpoints, web origin and
authentication hints. Only `dotcom` and `ghes` kinds are supported.

| Host | REST base | GraphQL endpoint | Web base |
| --- | --- | --- | --- |
| github.com | `https://api.github.com` | `https://api.github.com/graphql` | `https://github.com` |
| GHES | `https://<host>/api/v3` | `https://<host>/api/graphql` | `https://<host>` |

Canonicalize DNS names to lowercase and remove one trailing dot, identically
for configuration keys, `GH_HOST` and remotes. Reject URLs, userinfo, whitespace,
IP literals, empty labels and port-bearing configuration keys. Skip localhost
(including subdomains) and GitHub Cloud tenancy (`ghe.com` and its subdomains).

Accept scp-style SSH, HTTPS (optionally with a username), and `ssh://` remotes.
HTTPS uses port 443 only; an explicit SSH port does not change the API origin.
Reject unsupported schemes, queries, fragments and malformed repository paths.
`parse_owner_repo(url, trusted)` returns `(host, owner, repo)` only for a trusted
host; otherwise it returns `None` and the checkout is skipped.

## Offline trust

Discover trusted hosts from top-level keys in GitHub CLI's local `hosts.yml`.
Resolve its directory using nonempty `GH_CONFIG_DIR`, then `XDG_CONFIG_HOME/gh`,
then `AppData/GitHub CLI` on Windows, otherwise `~/.config/gh`. Parse with
`yaml.safe_load`; do not retain or log account/token fields. `github.com` is
always trusted. `GH_HOST` selects credentials; it never establishes trust.

Discovery does not contact servers, resolve credentials, invoke `gh auth status`
or unlock keyrings. Configured hosts remain visible while offline or holding
invalid credentials; API errors surface in their repo records instead.

The runtime discovers once at startup and shares its map with refresh and the
direct status/open handlers. Manual Refresh rereads it and atomically publishes
a replacement. A successful empty or missing file leaves only github.com.
Unreadable files, malformed YAML and invalid mappings preserve the previous map;
at startup they fall back to github.com. Discovery works without a `gh` binary
when its configuration file is available.

## Credential and request boundaries

| Target | Token resolution order |
| --- | --- |
| github.com | `GH_TOKEN`, `GITHUB_TOKEN`, stored gh token |
| GHES matching canonical `GH_HOST` | `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, stored gh token |
| Other GHES | stored gh token only |

Enterprise environment variables hold a single token, so requiring an explicit
`GH_HOST` binding prevents broadcasting that credential to multiple servers.
The server must also be registered in `hosts.yml`. Tokens resolve per host on
each refresh, so rotations take effect without restarting the worker.

Stored credentials are read with `gh auth token --hostname <host>`. Strip all
four token environment variables from the subprocess environment so gh cannot
reintroduce an unbound token. Retain the existing token-resolution timeout.
Never log helper stderr, persist tokens or include them in snapshots.

Each host gets its own HTTP client and Authorization header. Disable redirects
and reject off-origin or userinfo-bearing absolute REST URLs. Post GraphQL to
the absolute per-host endpoint: joining `/graphql` onto GHES's REST base would
incorrectly target `/api/v3/graphql`. Login/error hints identify the target host
and applicable token family; dotcom guidance explains `GH_TOKEN` precedence.

## Refresh, state and UI

Network identity is `(host, owner, repo, branch)`. ETags, rich GraphQL caches,
retry cooldowns, REST/GraphQL backoff and budget observations are partitioned by
host. One host's low budget stretches only its own freshness intervals. Shared
state is protected by the existing cache lock.

Honor usable host `workspace_repos` metadata, including its session branch;
otherwise retain shallow checkout discovery. Local identity deduplication uses
the checkout's real path and branch override, preserving separate sessions that
share a source checkout but query different branches.

Fetch host groups on up to four workers, each with its own token and client.
REST retains bounded concurrency; GraphQL stays serial within each host to
respect secondary limits. Collect results deterministically in sorted host
order. Healthy hosts can start requests while another host is offline, though
the combined snapshot waits for all groups to finish.

Snapshots use version 2, repo records include `host`, and authentication is
`auth: {host: {present: bool}}`. Ignore older disk snapshots and rebuild on the
first refresh; in-memory caches rebuild at worker startup. Tokens never enter
either cache format.

PR selection keys include the host as well as owner/repo/PR number. Auth notices
follow the selected repo's host, including repos without PRs. Single-host
rate-limit notices name that host and budget; multi-host notices use aggregate
wording. The one-shot rate-limit notification remains global. Compare links use
`host.web_base`; PR links retain the server's absolute URLs.

## Validation and limits

Offline tests cover discovery failures and rereads, canonicalization and remote
forms, two-GHES credential isolation, subprocess token stripping, request origin
checks, cache/backoff/budget isolation, session branch overrides, and UI host
selection and hints. HTTP tests use `httpx.MockTransport`, never real credentials
or a live GHES server. Existing partial GraphQL error handling remains in place.

No new plugin setting, host RPC, login flow, GitHub App integration or schema
probing is introduced. The host's existing `net` capability covers direct GHES
requests without a hostname allowlist. Custom HTTPS ports, IP-literal servers,
localhost and GitHub Cloud tenancy require a separate endpoint/token design.

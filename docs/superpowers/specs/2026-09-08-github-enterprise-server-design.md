# GitHub Enterprise Server support

Date: 2026-09-08
Status: approved, ready for an implementation plan

## Problem

The plugin only works against github.com. Four separate blocks stop a GitHub
Enterprise Server (GHES) checkout:

1. `utils/gitctx.py` matches three literal remote prefixes, all `github.com`. A
   GHES remote returns `None`, so `_identify` skips the checkout and the row
   shows "not a github.com remote".
2. `client.py` defines `DEFAULT_GITHUB_API_BASE = "https://api.github.com"`. The
   `api_base` parameter exists, but no caller passes it, and no setting supplies
   one.
3. `post_graphql` posts to the relative path `/graphql`, which is correct only
   for github.com. GHES serves REST at `/api/v3` and GraphQL at `/api/graphql`.
4. `auth.py` runs `gh auth token` with no `--hostname`, so a user logged in to
   both hosts gets the github.com token, which GHES rejects with a 401.
   `handlers.py` also builds a compare URL from the literal `https://github.com`.

## Decisions

Four product decisions were settled before design:

| Decision | Choice |
| --- | --- |
| Host scope | Multi-host. github.com and one or more GHES hosts work in the same AoE instance, in the same refresh tick. |
| Host trust | Discovered offline from the host names in gh's local `hosts.yml`. No new setting. |
| Token order | `gh`'s own documented rules, per host. |
| Auth banner | Per host, naming the host and the matching hint. |

Approach chosen: a `GitHubHost` value object, with the refresh partitioned by
host. Two alternatives were rejected:

- **One client, absolute URLs per request.** The token lives in the client's
  default headers, so a single client leaks host A's token to host B.
- **Keep `RepoKey` at three elements, carry the host beside it.** Smaller diff,
  but the same `owner/repo/branch` on two hosts collides in `_etag_cache` and
  `_graphql_cache`. That is a silent wrong-data bug, not a visible failure.

## Architecture

One new module owns every host fact. No other module knows a hostname.

```
hosts.py         GitHubHost + discovery + remote->host resolution   (new)
auth.py          resolve_token(env, host)                           (signature)
client.py        GitHubClient(host=..., token=...)                   (signature)
utils/gitctx.py  parse_owner_repo(url, trusted) -> (host, owner, repo)
refresh.py       RepoKey gains host; _fetch_all groups by host
uistate.py       per-host auth note
handlers.py      compare URL from host.web_base
```

### GitHubHost

A frozen dataclass with two constructors, so the URL rules live in one place.

```python
@dataclass(frozen=True)
class GitHubHost:
    name: str          # "github.com" | "github.mycorp.com"
    rest_base: str
    graphql_url: str
    web_base: str
    enterprise: bool
```

| Constructor | rest_base | graphql_url | web_base |
| --- | --- | --- | --- |
| `GitHubHost.dotcom()` | `https://api.github.com` | `https://api.github.com/graphql` | `https://github.com` |
| `GitHubHost.enterprise(name)` | `https://<name>/api/v3` | `https://<name>/api/graphql` | `https://<name>` |

`GitHubClient.__init__` takes a `GitHubHost` in place of the `api_base` string.
`DEFAULT_GITHUB_API_BASE` and the unused `api_base` parameter are both removed.

`post_graphql` must post to `host.graphql_url` as an **absolute** URL. Verified
httpx join behaviour:

```
base_url="https://gh.corp/api/v3" + "/graphql"          -> https://gh.corp/api/v3/graphql   WRONG
base_url="https://gh.corp/api/v3" + "/repos/o/r/pulls"  -> https://gh.corp/api/v3/repos/... correct
absolute "https://gh.corp/api/graphql"                  -> https://gh.corp/api/graphql      correct
```

So REST keeps its relative paths, and only GraphQL changes to an absolute URL.

## Host discovery and trust

Discovery runs once per worker lifetime, not once per refresh. The runtime
object resolves the trusted map at startup, holds it, and passes it down to
both the refresh path and the direct handlers (`github.status`,
`github.open`), which call `parse_owner_repo` outside any refresh. A manual
`github.refresh` re-discovers, so a user who runs `gh auth login` mid-session
has a way to pick the new host up without a worker restart.

The lifetime cache avoids reading a configuration that rarely changes on every
120-second background tick. Discovery reads local configuration only; it does
not check credentials, contact a host, or unlock a keyring.

```python
def discover_trusted_hosts(
    env: TokenEnvironment,
    previous: dict[str, GitHubHost] | None = None,
) -> dict[str, GitHubHost]
```

Read the top-level host keys from gh's `hosts.yml`. Resolve its directory using
[gh's configuration path precedence](https://github.com/cli/go-gh/blob/trunk/pkg/config/config.go):
non-empty `GH_CONFIG_DIR`, then `XDG_CONFIG_HOME/gh`, then
`AppData/GitHub CLI` on Windows, otherwise `~/.config/gh`.

Use `yaml.safe_load` (add PyYAML to the runtime dependencies and lockfile),
validate a mapping with hostname keys, and return only those names from the
environment seam. Do not log the file contents, retain account/token fields,
or use them for token resolution; that remains the responsibility of `auth.py`.

`gh auth status --json hosts` is unsuitable for discovery: it checks accounts
over the network and emits JSON only after the checks finish. A five-second
subprocess timeout can therefore discard every host when just one VPN-only
server is unreachable. Discovery must not invoke that command, even at startup.

Rules:

- `github.com` is always present in the result, whether or not the file lists it.
  Today's behaviour must never regress when `gh` is absent.
- Every other valid hostname becomes `GitHubHost.enterprise(name)`. Validate
  hostnames before constructing URLs; a URL, path, or userinfo is not a host key.
- A configured host remains trusted when offline or when its token is invalid.
  Subsequent API requests surface network or authorization errors in the pane;
  discovery never hides a repo because an authentication check failed.
- A successful read replaces the map, so manual refresh picks up logins and
  removals. An empty file, empty mapping, or missing file is a successful empty
  configuration and produces only github.com.
- An unreadable file, malformed YAML, or invalid mapping preserves a copy of
  `previous`; at startup with no previous map, fall back to github.com. These
  failures never raise or erase a previously discovered enterprise host. The
  runtime passes its current map on manual refresh and atomically publishes the
  result for both refresh and direct handlers.
- Discovery works without an installed `gh` binary if its configuration remains
  available. `GH_AUTH_TIMEOUT` still bounds token-resolution shell-outs only.

`TokenEnvironment` gains `gh_config_hosts() -> list[str] | None`, beside the
existing `gh_auth_token()`. An empty list means a successful empty configuration;
`None` means a read or parse failure. This keeps discovery testable with fixture
configuration and no real `gh`, network, or user credentials.

### Remote matching

`hosts.host_for_remote(url, trusted) -> GitHubHost | None` replaces the literal
prefix list. It accepts these forms, because GHES installs commonly use them:

```
git@<host>:owner/repo.git
https://<host>/owner/repo.git
https://user@<host>/owner/repo
ssh://git@<host>/owner/repo.git
ssh://git@<host>:2222/owner/repo.git
```

`parse_owner_repo(url, trusted)` returns `(host, owner, repo)`. A remote on an
untrusted host still returns `None`, so the existing benign-skip path is
unchanged.

## Per-host tokens

`resolve_token(env, host)` follows `gh`'s documented rules.

| Host kind | Order |
| --- | --- |
| github.com | `GH_TOKEN` -> `GITHUB_TOKEN` -> `gh auth token -h github.com` |
| GHES | `GH_ENTERPRISE_TOKEN` -> `GITHUB_ENTERPRISE_TOKEN` -> `gh auth token -h <host>` |

`gh auth token` gains `--hostname <host>`. Tokens resolve once per refresh per
host and are held in a local dict for that refresh only. They are never written
to disk and never put in the snapshot. Unlike the trusted-host map, tokens are
NOT cached for the worker's lifetime: a token can expire or rotate, and the
per-refresh resolve is what today's code already does.

`errors.py` needs the host in its hints. `NoTokenNoGhError` and
`GhNotAuthenticatedError` currently suggest `gh auth login`, which is wrong
advice for a GHES host. Both gain a host field, so the hint reads
`gh auth login --hostname github.mycorp.com` and names `GH_ENTERPRISE_TOKEN`
instead of `GITHUB_TOKEN`.

### Accepted behaviour change

Today a user with only `GITHUB_TOKEN` set gets that token for every repo. After
this change that token no longer applies to a GHES host. Such a user sees the
GHES repo degrade to the unauthenticated open-PR view, with a note naming
`GH_ENTERPRISE_TOKEN`. This is `gh`'s rule, and it is what stops a work token
from reaching github.com.

## Partitioning the refresh

`RepoKey` becomes `(host_name, owner, repo, branch)`. `RichCacheKey` gains the
same leading element. That one change carries `_etag_cache`, `_graphql_cache`,
and `_retry_cooldown` across, because the host is now part of every lookup.

- `_identify` returns the host with the repo string.
- `entry["branch"]` reads `key[3]`, not `key[2]`.

`_fetch_all` gains one grouping layer around today's logic:

```python
by_host = group keys by key[0]
for host_name, host_keys in sorted(by_host.items()):
    host  = trusted[host_name]
    token = _resolve_optional_token(env, host)   # existing helper, gains host
    with GitHubClient(host=host, token=token) as client:
        results |= _fetch_rich(client, host_keys, ...)
```

Hosts are iterated in sorted order for determinism, matching how `ordered` is
already sorted today. `_fetch_rich`, `_probe_all`, `_fetch_chunk`, and the digest
tier retain their pipeline, but must pass the host through cache-key unpacking,
backoff checks, budget observations, and freshness calculations.

`token_present` becomes a per-host map rather than one boolean.

### Backoff and budget pacing must become per host

`_backoff` is a module dict with the fixed keys `rest`, `graphql`, and
`notified`. Today a github.com rate limit would also gate GHES polling, which is
wrong: the two budgets are unrelated, and a GHES install often has a far larger
one. So `_backoff` nests one level, `_backoff[host_name][api]`, created on
demand. `_set_backoff` and `_active_backoff_locked` take a host.

Partition `_graphql_budget` as well: `_graphql_budget[host_name]` holds that
host's `remaining` and `valid_until`, created on demand under `_cache_lock`.
`_observe_rate_limit` and `_budget_multiplier` take the host, as do
`_digest_stale` and `_full_stale`. Thread the host through every rich and digest
query observation and every freshness check. An expired observation clears only
that host's budget; a host with no observation uses multiplier 1.

The existing multiplier of 4 below 1,000 remaining points applies only to the
host that reported that budget. A low-budget host must not stretch another
host's refresh intervals, and a healthy response from another host must not
remove the depleted host's pacing. Adding the host to `RepoKey` alone does not
partition this module-level state.

The `notified` one-shot flag stays global, so the user still gets one message per
window rather than one per host. When exactly one host is limited, the message
names it.

### Snapshot shape

```python
"auth": {"github.com":        {"present": True},
         "github.mycorp.com": {"present": False}}

"repos": [{"path": ..., "host": "github.mycorp.com", "repo": "api/core", ...}]
```

## Links and the pane

PR links need no change. REST `html_url` and GraphQL `url` are absolute URLs the
server returns, so a GHES PR already arrives with a GHES link. The
`github_pr_badge` href and the `open_pr` client command therefore work on GHES
with no edit.

The direct handlers (`github.status`, `github.open`) receive the trusted map
from the runtime object, the same one the refresh path uses, so a GHES checkout
resolves identically on both paths.

One link in the codebase is built by hand, `handlers.py:131`. It takes
`host.web_base`:

```python
f"{host.web_base}/{owner}/{repo}/compare/{quoted}?expand=1"
```

`uistate` reads the selected PR's repo record, looks up that host in the `auth`
map, and warns only when that host lacks a token:

```
No token for github.mycorp.com: showing open PRs only.
Run: gh auth login --hostname github.mycorp.com
```

For github.com the wording keeps today's `GITHUB_TOKEN` and `gh auth login`
hint, so nothing regresses for existing users.

The summary strings in `handlers.py` change from "not a github.com remote" to
"not a GitHub remote", because the host is no longer fixed.

## Testing

Test-driven, module by module, against the current green baseline of 260 tests.
`uv run pytest` is the command. No test touches the network or a real `gh`.

The security test goes in first, because it justifies the whole design:

- **Token isolation.** Two hosts in one refresh through `httpx.MockTransport`.
  Assert that each request's `Authorization` header carries only its own host's
  token, and that a GHES token never appears in a github.com request.

Then, per module:

- `test_hosts.py` — URL derivation for both constructors; remote parsing for
  `git@`, `https://`, `https://user@`, `ssh://`, and a `:port` form; an
  untrusted host returns `None`.
- `test_auth.py` — both token tables; `--hostname` is passed; each hint names the
  right variable and the right `gh auth login` form for its host kind.
- `test_client.py` — GraphQL posts to the absolute `graphql_url`; REST joins onto
  `/api/v3`.
- Discovery — fixture `hosts.yml` files cover configuration-directory precedence,
  multiple hosts, invalid hostname keys, empty/missing files, malformed YAML,
  and read failures. Verify failed rereads retain the previous map, successful
  reads apply additions/removals, and startup failures fall back to github.com.
  Assert discovery never invokes a subprocess or network request: with one
  offline/VPN-only host configured, every configured host is still discovered
  on a cold start. Include invalid credentials and an absent `gh` binary.
- `test_refresh.py` — cache keys separate two hosts that share an
  `owner/repo/branch`; a rate-limit trip on one host does not gate the other.
  In one refresh, give host A 100 remaining GraphQL points and host B 5,000;
  verify only A's digest and full-refresh intervals stretch by 4, regardless of
  observation order. Reset A's observation and verify B's budget is untouched;
  a host with no observation keeps multiplier 1.
- `test_gitctx.py` and `test_handlers.py` — both change, because
  `parse_owner_repo` changes shape and the handlers take the trusted map. The
  handlers' tests cover a GHES checkout end to end: `github.status` summary and
  the `github.open` compare URL built from `host.web_base`.
- `test_uistate.py` — the note fires only for the selected PR's host, and its
  hint matches that host's kind. Before the pane work starts, grep for every
  reader of the snapshot's `auth` key and update each in the same change: the
  version gate in `load_snapshot` protects the disk cache, not in-memory
  consumers inside the release.
- A GHES-style partial GraphQL error on `mergeable`, proving the existing
  `_error_aliases` and `_fallback_pulls` path already covers an older GHES
  schema. No schema probing is added.

## Compatibility

`load_snapshot` accepts any dict with a `sessions` list, and its docstring states
that no version envelope is needed. That is no longer true: an old cached
snapshot has `auth: {"present": bool}` and repo records with no `host`. The
snapshot therefore gains `"version": 2`, and `load_snapshot` ignores any other
value. The cost is one cold refresh after upgrade, which is what a fresh install
already does.

The in-memory caches need no migration, because the worker rebuilds them on
start.

## Out of scope

- No new plugin setting. Discovery is automatic, so the manifest's settings list
  is untouched.
- No new host RPC. Discovery reads gh's local configuration in the worker,
  alongside its existing local git and snapshot reads.
- No GraphQL schema probing for older GHES versions. The existing partial-error
  tolerance handles it.
- No GitHub App or OAuth device-flow work. Token sources stay as they are.

## Docs to update

- `pyproject.toml` and `uv.lock` — add PyYAML for safe configuration parsing.
- `aoe-plugin.toml` — the `net` capability comment names `api.github.com`.
- `README.md` lines 80 and 81 — "null outside a github.com checkout" becomes
  "outside a trusted GitHub host".
- `README.md` — a short GHES section covering discovery and the two enterprise
  token variables.

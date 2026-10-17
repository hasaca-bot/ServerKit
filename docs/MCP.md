# MCP server

ServerKit's panel is an MCP server. An AI assistant (Claude Code, claude.ai,
any MCP client) connects with **your own API key** and can publish and run
**your own** services: deploy a GitHub repository, set environment variables,
read logs, add a database or a cron job, roll back, delete. It never sees
another user's services, and secrets never leave the panel.

This replaces the shared-key MCP of the Windows publish gateway
(`windows/gateway/gateway.py`). The gateway's `core` mode forwards its `/mcp`
and `/mcp/<key>` to the endpoints below unchanged.

## Endpoint

| | |
|---|---|
| URL | `POST https://<panel>/api/v1/mcp` |
| Key in the path | `POST https://<panel>/api/v1/mcp/k/<key>` (for clients that cannot send headers) |
| Transport | MCP Streamable HTTP, JSON responses (no SSE stream, no sessions; `GET` answers 405) |
| Protocol | JSON-RPC 2.0; `initialize` negotiates `2025-06-18`, `2025-03-26` or `2024-11-05` |
| Methods | `initialize`, `notifications/initialized` (202, no body), `ping`, `tools/list`, `tools/call`; batches are accepted |

Tool results are `{"content": [{"type": "text", "text": "<json>"}], "isError": bool}`.
A refused or failed action is a tool result with `isError: true` and an
`error` message; protocol mistakes are JSON-RPC errors (`-32700` parse,
`-32600` invalid request, `-32601` unknown method, `-32602` bad params or
unknown tool).

## Authentication

1. In the panel open **Settings → API → MCP server** and press **Create MCP
   key**. It creates an API key with the `mcp` scope (a key with full access
   `*` works too; any other scope is refused with 403).
2. Send it as `Authorization: Bearer <key>` (or `X-API-Key: <key>`), or put it
   in the path: `/api/v1/mcp/k/<key>`. **A path URL is a secret** — anyone who
   has it can act as you; use it only where headers cannot be set.

A browser session (JWT) is not accepted on this endpoint. Bad keys are
throttled per client IP (the login brute-force throttle, `AUTH_IP_*`), each
key may make 60 calls a minute (429 + `Retry-After`), revoked or expired keys
and deactivated accounts are refused. The path key is masked
(`/api/v1/mcp/k/<key>`) in the access log, the audit log, API analytics and
error logs.

### Client configuration

Claude Code:

```bash
claude mcp add --transport http serverkit https://panel.example.com/api/v1/mcp \
  --header "Authorization: Bearer sk_..."
```

Generic (`.mcp.json` and most clients):

```json
{
  "mcpServers": {
    "serverkit": {
      "type": "http",
      "url": "https://panel.example.com/api/v1/mcp",
      "headers": { "Authorization": "Bearer sk_..." }
    }
  }
}
```

claude.ai custom connector: `https://panel.example.com/api/v1/mcp/k/sk_...`

### Raw example

```bash
curl -s https://panel.example.com/api/v1/mcp \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"list_sites","arguments":{}}}'
```

## Tools

Slow actions return `{"job": "<id>"}` at once; poll `get_job` every 15-30 s
until `status` is `succeeded` or `failed` (jobs are ordinary deployment jobs:
they also appear in the panel's Deploy Activity with their live log).

| Tool | Writes | What it does |
|---|---|---|
| `get_instructions` | | How the server works, the publish workflow and the limits |
| `list_sites` | | Your services: address, status, repository, branch |
| `site_info` | | Status, port, auto-deploy, domains, env **key names**, databases, cron |
| `get_job` | | Progress, log (redacted) and result of a job you started or of your service |
| `get_logs` | | Last 1-1000 container log lines, `since` filter, redacted |
| `get_metrics` | | CPU, memory, network and disk I/O of the site's containers |
| `list_deploys` | | Deployment history and recent jobs |
| `list_env_keys` | | Env variable names and whether each is secret (never values) |
| `list_cron` | | The site's scheduled commands |
| `list_domains` | | Host names of the site |
| `list_previews` | | Pull-request previews of the site |
| `list_repos` | | Repositories your GitHub connection grants |
| `get_usage` | | Plan limits and current usage |
| `publish_site` | yes | Publish (or update) a site from one of your GitHub repositories (job) |
| `set_env` | yes | Set/unset variables (string, generated secret, or supplied secret), then redeploy (job) |
| `redeploy_site` | yes | Rebuild from the latest pushed commit (job) |
| `rollback_site` | yes | Back to an earlier successful deployment (job) |
| `restart_site` | yes | Restart without rebuilding |
| `stop_site` / `start_site` | yes | Sleep (containers stopped, data kept) / wake |
| `create_database` | yes | postgresql, mysql, mariadb or redis for the site; sets `DATABASE_URL` / `REDIS_URL`, redeploys (job) |
| `delete_database` | yes | Removes the database and all its data, unsets the key, redeploys (job) |
| `add_cron` / `delete_cron` | yes | Scheduled command inside the site's container |
| `run_command` | yes | One command inside the site's container, exit code + output |
| `add_domain` / `remove_domain` | yes | A custom domain of yours on the site |
| `delete_site` | yes | Everything: databases, cron, webhooks, domains, containers, volumes, checkout (job) |

`publish_site` arguments: `name` (3-32 chars `a-z 0-9 -`, unique, not
reserved; becomes `https://<name>.<base domain>`), `github` (`owner/repo`
among `list_repos`), `branch`, `port`, `dockerfile` (text, optional),
`health`, `volumes` (up to 5 container paths kept across deploys), `env`,
`auto_deploy` (registers the core push webhook on GitHub through your
connection), `root_dir` and `build_filter` (monorepos; a head commit with
`[skip deploy]` is not auto-deployed). Publishing the same name again updates
your site.

```json
{"name": "shop", "github": "you/shop", "port": 8000, "health": "/health",
 "volumes": ["/data"], "auto_deploy": true,
 "env": {"SECRET_KEY": {"generate": "hex32"}, "ADMIN_PASSWORD": {"generate": "password"}}}
```

## Limits (same as the gateway)

- Env keys are `UPPER_SNAKE_CASE` (≤ 64 chars), never `SERVERKIT_*`; values
  ≤ 4000 chars; ≤ 50 keys per call; `DATABASE_URL` / `REDIS_URL` belong to the
  database tools. Generators: `hex32`, `password`, `fernet`; a generated value
  is never returned.
- At most 2 databases per site, one per engine, one SQL database.
- At most 10 cron jobs per site, never more often than every 5 minutes; five
  fields of digits and `* / , -`.
- `run_command` / cron argv: 1-20 words of `A-Z a-z 0-9 _ . / : = @ , + % -`
  (`%` not in cron) — no shell, quotes, pipes, `$`, `;`, backticks. Commands
  run with `docker exec` in the site's own container only; `run_command` stops
  after at most 60 s and its output is capped at 20 000 chars.
- One job per site at a time. `delete_site` needs `confirm: "DELETE <name>"`.
- Plan quotas (`PlanQuotaService`): new services (`apps`), databases, cron jobs
  and custom domains are checked before creation; a refusal is a tool error
  carrying `quota: <kind>`.

## Security model

- **Identity = the key's owner.** Every tool resolves services only among the
  apps the owner owns plus those explicitly shared with them (resource
  grants) — the non-admin branch of `WorkspaceService.scope_query`, applied to
  admins too, so an admin's MCP key does not expose other tenants. A name that
  belongs to someone else answers exactly like a name that does not exist.
- **Role decides the tool set.** Viewers get only the read tools (write tools
  are hidden from `tools/list` and refused). Developers and admins may write,
  but only where they could in the panel: a read-only grant stays read-only,
  and `delete_site` / `delete_database` need the owner (or app admin).
- **GitHub only through your connection.** `publish_site` accepts only a
  repository your own GitHub source connection grants, cloned with that
  connection's token; no raw git URL, no other owner's repository.
- **No secrets out.** Env values, database passwords, webhook secrets and
  tokens are never returned; logs, command output and job logs are redacted
  (credentials in URLs, `password=`/`token:`-style pairs, long token-like
  strings). Databases are wired with a service reference, so the connection
  string never exists outside the panel and the container.
- **Containment.** Commands run only inside the site's container, resolved
  from the app record, never from the request. Database engines are private
  (bound to localhost, reached over the internal services network).
- **Audit.** Each write tool call adds an audit row (`mcp.tool_call`: tool,
  site, ok, key id — never the arguments).

## Parity with the gateway

All 17 gateway tools keep their names and argument shapes. Differences:
`list_sites` lists only your sites (no `ports_in_use`, no foreign sites);
`publish_site` takes an optional `port` (build packs detect it) and no host
port choice (the panel allocates it); job ids are deployment-job UUIDs;
`delete_cron` takes the panel's cron job id string from `list_cron`.

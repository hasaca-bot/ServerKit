# Hosting Roadmap — from a self-hosted panel to a community hosting platform

Status: living plan, started 2026-10-17. Working product name: **Deployra**
(placeholder, set in one place: `landing/src/config.js` and `HOSTING_BRAND` env;
pick the final name only after a trademark clearance search, see §6).

Two products are sold on the same core:

1. **App hosting**: push a GitHub repository and get `https://<name>.<domain>`
   with databases, env vars, logs, cron, previews and auto-deploy.
2. **AI-managed VPS**: a dedicated VPS with the ServerKit agent preinstalled,
   which the customer operates from their AI assistant through MCP.

## 1. Principles

- **The core is the product.** Deploy, webhooks, manifests, previews, rollback,
  slots, health gate, env groups and backups already exist in `backend/app`.
  The Windows scripts (`windows/sites/publish.py`, `sync.py`, `gateway.py`)
  move onto them instead of re-implementing them.
- **Tenant isolation like Render.** A customer sees only their own apps,
  databases, domains, servers and keys, and can deploy only repositories their
  own GitHub connection grants. Non-admins never type a raw git URL or another
  owner's repo.
- **MCP is a first-class client.** Every capability the gateway MCP had (17
  tools) stays, now per-user and scoped. New tools are added only when they are
  safe under the same scoping.
- **One quota seam.** Every create path calls
  `PlanQuotaService.check(user, kind)` (`backend/app/services/plan_quota_service.py`).

## 2. Feature gap vs Render

| Area | Render | Here today | Plan |
|---|---|---|---|
| Web services, workers, cron | yes | yes (Docker, buildpacks, Procfile workers) | keep |
| Static sites | CDN | buildpack static, no CDN | Cloudflare cache rules (P3) |
| Private services | internal hostnames | docker network via `netlink.sh` | per-tenant network + `<app>.internal` alias (P2) |
| GitHub auto-deploy | webhook | core webhook exists; Windows uses 3-min polling | switch to core webhooks (P0) |
| Skip deploy / path filters | `[skip render]`, buildFilter | no | `[skip deploy]` + `build_filter` globs (P1) |
| Pre-deploy command | yes | `release_phase_service` | expose in UI/MCP (P1) |
| Zero-downtime, health checks, rollback | yes | slots, health gate, rollback | expose in MCP (P0) |
| PR previews | yes | `previews` | per-plan limit (P1) |
| Blueprints | render.yaml | `manifests` (reads render.yaml) | keep, document (P1) |
| Postgres / Key Value | managed | database engines + backups | per-plan size limits (P1) |
| Scaling | manual + autoscale | resources (CPU/RAM) only | manual replicas (P3), autoscale later |
| Free tier sleep | 15 min idle, wake on request | sleep exists, no wake on request | wake-on-request via nginx (P1) |
| Teams, roles, audit, SSO | yes | yes | keep |
| Plans and quotas | yes | workspace `max_*` only | `PlanQuotaService` + Plan model (P0) |
| API keys / MCP | API, CLI, MCP | per-user API keys; MCP only in gateway (shared key) | core MCP at `/api/v1/mcp`, per-user keys (P0) |
| VPS product | — | agent fleet, cloud-provision extension | tenant-owned servers + VPS MCP tools (P2) |
| Billing | Stripe-like | none | P2 (provider TBD, Turkey: iyzico/PayTR) |

## 3. Phases

### P0 — Core platform (this sprint)
- **Core MCP endpoint** `POST /api/v1/mcp` (Streamable HTTP, JSON). Auth: the
  caller's own API key (`Authorization: Bearer` or `/api/v1/mcp/k/<key>` for
  clients that cannot send headers; masked in logs). Tool parity with the
  gateway's 17 tools, every tool scoped to the caller's own resources.
  New safe tools: `list_repos`, `list_deploys`, `rollback_site`,
  `get_metrics`, `list_env_keys`, `list_domains`, `add_domain`,
  `remove_domain`, `restart_site`, `list_previews`, `get_usage`.
- **Tenancy and plans**: `Plan` model + migration (`hobby`, `pro`, `team`,
  admin-assigned), `PlanQuotaService` real limits, enforcement in app/db/cron/
  domain/preview create paths; non-admins may deploy only from their own
  GitHub connection; servers become owner-scoped for the VPS product.
- **Auto-deploy via webhooks**: publishing registers the core webhook
  (`/api/v1/git/webhooks/receive/<token>`) on the repo through the user's
  GitHub App installation; `windows/sites/sync.py` polling is retired.
- **Windows scripts generalised**: one config file
  `windows/serverkit-windows.json` (domain, tunnel, GitHub owner allowlist,
  ports, limits, paths) read by every script; no owner-specific values in code.
  The Cloudflare Tunnel routes `*.<domain>` to nginx once, so publishing no
  longer adds a DNS record per site.
- **Landing page** (`landing/`), English, features + pricing, built on the
  panel's design tokens.

### P1 — Render parity for developers
- `[skip deploy]` / `[deploy skip]` in the head commit message; path
  `build_filter` (include/ignore globs) for monorepos.
- Wake-on-request for sleeping free apps (nginx `error_page 502` → wake
  endpoint → retry), plus per-plan idle timeout.
- UI screens missing vs Render: service creation wizard from GitHub
  (repo → branch → runtime → plan → env), Events timeline per service,
  Usage & Plan page, MCP/API keys page with ready-to-copy client config.
- Pre-deploy command and health-check path editable in UI and MCP.

### P2 — Commercial launch
- Billing (subscriptions, invoices, VAT/KDV), plan upgrades/downgrades.
- AI-managed VPS: provisioning via `serverkit-cloud-provision`, servers owned
  by a workspace, VPS MCP tools (`vps_list`, `vps_metrics`, `vps_run`,
  `vps_docker_*`, `vps_snapshot`) scoped to the owner's servers.
- Per-tenant docker networks and internal hostnames.
- Legal pages live (ToS, Privacy/KVKK, AUP, takedown form) — see §6.
- Traffic log retention required by Law 5651.

### P3 — Scale
- Multiple worker hosts per region, manual replicas, then autoscaling.
- Static-site CDN rules, log streams (syslog/OTLP), status page.

## 4. Pricing (example numbers, edit `landing/src/pricing.js`)

Workspace plans: **Hobby** $0 (1 member, 3 services, apps sleep after 15 min,
1 database) · **Pro** $19/mo (unlimited members, 25 services, previews,
custom domains, 5 MCP keys) · **Team** $59/mo (audit log, 30-day logs,
priority support, SSO later).
Compute per service: Free 0.1 CPU/256 MB (sleeps) · Starter $5 0.5 CPU/512 MB
· Standard $12 1 CPU/2 GB · Pro $24 2 CPU/4 GB.
Databases: Free 256 MB (no backups) · Basic $6 1 GB daily backups ·
Standard $18 10 GB.
AI-managed VPS: VPS-1 $9 (2 vCPU/2 GB/40 GB) · VPS-2 $17 (2 vCPU/4 GB/80 GB)
· VPS-4 $33 (4 vCPU/8 GB/160 GB), MCP endpoint and weekly snapshots included.

## 5. Work split (agents)

| Package | Owner | Files |
|---|---|---|
| A. Tenancy, plans, quotas, GitHub-only deploys, server ownership | Opus | `models/plan*`, `services/plan_quota_service.py`, migrations, app/db/cron/domain/preview create paths |
| B. Core MCP endpoint + tools | Opus | `api/mcp.py`, `services/mcp/*`, tests |
| C. Landing page | Sonnet | `landing/` |
| D. Windows scripts → core + config file | Sonnet | `windows/**` |
| E. Skip-deploy + build filters | Sonnet | `services/git_deploy_service.py`, `api/git.py`, tests |
| F. Legal templates + NOTICE | Haiku | `docs/legal/`, `NOTICE` |

## 6. Legal checklist (not legal advice; have a Turkish lawyer review)

- Keep `LICENSE` ("Copyright (c) 2026 Juan Denis", MIT) in every copy; add
  `NOTICE` and a credits link in the landing footer and panel About page.
- Do not sell under the name "ServerKit" (MIT grants no trademark rights; the
  name is also used by an unrelated app). Run TÜRKPATENT/EUIPO/USPTO clearance
  for the final name.
- Landing page: our own copy, icons and screenshots only; no Render name,
  logo, text, illustrations or trade dress; no "Render" in ads or keywords.
- **Cloudflare**: self-serve terms forbid reselling access to third parties
  (§2.2.1(a)). Before selling, get a written Enterprise/reseller agreement or
  run customer traffic through infrastructure we own (own reverse proxy +
  Let's Encrypt), using Cloudflare only for our own domains.
- **GitHub**: use a GitHub App with per-repository permissions (already
  supported by `source_connection_service`), state + PKCE on OAuth.
- Ship `THIRD-PARTY-NOTICES` (pip-licenses + license-checker output) and the
  IBM Plex OFL file with every image. Code deps are all permissive.
- App templates: in the hosted catalog hide SSPL (MongoDB), BSL (Outline,
  Vault > 1.15), Sustainable Use (n8n), CockroachDB and Plex templates; offer
  AGPL apps only as unmodified upstream images; pin Redis to 7.2.x.
- Brand logos from `react-icons/si` are third-party trademarks (nominative use
  only); Font Awesome icons need CC BY 4.0 attribution.
- Stay MIT: a closed or dual-licensed edition would need consent from the
  other contributors (no CLA exists).
- Publish ToS, Privacy Policy (KVKK + GDPR), AUP, takedown process (5651
  "yer sağlayıcı" duties incl. traffic-log retention), distance-sales
  pre-contract information (6502), VERBIS check, DPAs with processors, VAT.

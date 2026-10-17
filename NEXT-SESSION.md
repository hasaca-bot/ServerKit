# Next session: continue here

(TR: Yeni oturumda yapay zekâya "NEXT-SESSION.md'yi oku ve devam et" demen yeterli.)

Branch: `ccr-0583b883-dof7ej` on `hasaca-bot/ServerKit`. Plan: `docs/HOSTING-ROADMAP.md`.
Older context: `HANDOFF.txt` (Windows/WSL port), `windows/README.md`.

## 1. Where things stand (2026-10-17)

Done and pushed:
- `docs/HOSTING-ROADMAP.md`: Render-like hosting plan, feature gap table, phases
  P0–P3, example pricing, legal checklist.
- `landing/`: English landing + pricing site (placeholder brand "Deployra",
  edit `landing/src/config.js` and `landing/src/pricing.js`).
- Deploy controls: `[skip deploy]` / `[skip cd]` commit markers, `[skip preview]`
  PR titles, per-app `root_dir` + `build_filter` (migration 111). GitLab push
  hooks now deploy (they were answered as pings before).
- Windows port generalised: `windows/serverkit-windows.json` config (copy the
  `.example.json`), webhook auto-deploy (`auto_deploy_mode`), wildcard tunnel
  (`domain-tunnel.sh wildcard`), gateway `mode: "core"` proxy to `/api/v1/mcp`.
- `NOTICE`, `scripts/gen-third-party-notices.sh`, `docs/legal/` templates
  (need a lawyer's review).

Wave 2 (Opus only) — MERGED (commits up to `fcddd43`), but both agents were
stopped by the owner before their final full-suite run. Checked after merge:
blueprint registry, authz static/sweep/boundary, jwt-only census and the new
MCP/plan/tenant tests = 311 passed. NOT yet run: the full backend suite and
the frontend `npm run lint` / `npm run build` on the merged branch — do that
first and fix what fails. Migration chain: 111 → 112_plans_and_ownership (no 113).
- **A. Tenancy, plans, quotas**: `plans` table (migration 112), real
  `PlanQuotaService`, enforcement on every create path, non-admins deploy only
  from their own GitHub connection, owner-scoped servers, `GET /api/v1/plan`,
  Plan & usage UI.
- **B. Core MCP**: `POST /api/v1/mcp` and `/api/v1/mcp/k/<key>`, per-user API
  key with scope `mcp`, the 17 gateway tools plus `list_repos`, `list_deploys`,
  `rollback_site`, `restart_site`, `get_metrics`, `list_env_keys`,
  `list_domains`, `add_domain`, `remove_domain`, `list_previews`, `get_usage`;
  `docs/MCP.md`; MCP card in Settings → API.
Review both packages against their goals before building on them (the agents'
own final reports were never delivered): every create path calls
`PlanQuotaService.check`, non-admins cannot deploy raw git URLs or another
user's connection, and every MCP tool resolves resources through tenant
scoping (user B can never reach user A's site).

## 2. Agent rules (from the owner)

- At most **2 Opus** agents at a time, and while Opus runs **no other agent**
  runs. Otherwise up to **3 Sonnet** + any number of **Haiku** (no Opus).
- Give each agent its own git worktree; make it start from the current branch
  head (`git reset --hard <head>` if the worktree starts elsewhere); merge and
  run tests yourself before pushing.
- **Gemini CLI** is used as an extra sub-agent, only in the Sonnet/Haiku waves
  (never alongside Opus).

## 3. Gemini CLI setup (do this first)

The owner's Google login is stored as the environment variable
`GEMINI_OAUTH_CREDS` (base64 of `~/.gemini/oauth_creds.json`). Never print it.

```bash
npm install -g @google/gemini-cli
mkdir -p ~/.gemini
echo "$GEMINI_OAUTH_CREDS" | base64 -d > ~/.gemini/oauth_creds.json
chmod 600 ~/.gemini/oauth_creds.json
echo '{ "security": { "auth": { "selectedType": "oauth-personal" } } }' > ~/.gemini/settings.json
cd /tmp && gemini --skip-trust -p "Reply with exactly: gemini ok"
```

Use it headless, one task per call, from the repo root:
`gemini --skip-trust --approval-mode plan -p "<task>"` for read-only research,
`--approval-mode auto_edit` inside a dedicated worktree for edits.
The auto-mode classifier blocked `gemini` in the previous session; the owner
has to allow it with a Bash permission rule (e.g. `Bash(gemini:*)`) before it
can run. If `GEMINI_OAUTH_CREDS` is empty, ask the owner to add it in the
environment settings (never in chat).

Security: the previous token was pasted into chat in full. The owner should
revoke it at https://myaccount.google.com/permissions, log in again locally
and replace `GEMINI_OAUTH_CREDS`.

## 4. Next steps

1. Verify the branch: backend `cd backend && python -m pytest -q` (venv:
   `python3 -m venv /home/user/sk-venv && /home/user/sk-venv/bin/pip install -r backend/requirements.txt pytest`),
   frontend `cd frontend && npm ci && npm run lint && npm run build`.
   The last full-suite run was cut off by a container restart.
2. Point the `windows/` gateway at `mode: "core"` and test one publish end to
   end on the owner's PC (create an API key with scope `mcp` in Settings → API).
3. P1 from the roadmap: wake-on-request for sleeping free apps, service
   creation wizard polish, Events/usage pages, pre-deploy command and health
   check in UI and MCP.
4. P2: billing, AI-managed VPS provisioning (`serverkit-cloud-provision`) and
   VPS MCP tools on owner-scoped servers.
5. Fix: `frontend/public/fonts/ibm-plex-sans-400.woff2` is corrupt (Chromium
   cannot decode it); `landing/fonts/` has a good copy.
6. Before selling: Cloudflare reseller terms, trademark clearance for the
   final name, hide SSPL/BSL/SUL templates in the hosted catalog, lawyer
   review of `docs/legal/` (see roadmap §6).

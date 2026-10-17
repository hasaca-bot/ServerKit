# ServerKit on Windows

ServerKit is a Linux server panel: it drives nginx, systemd, the firewall,
cron, Docker and mail directly. Windows has none of those, so this folder does
not port the features one by one. It gives the panel the Linux host it expects
— a dedicated WSL2 distro with systemd — and installs it there with the
unmodified upstream `install.sh`. Nothing in `backend/` or `frontend/` is
changed, so every feature behaves as it does on a VPS.

## Use

Double-click `ServerKit.bat` in the repository root and pick from the menu, or
run a command directly:

```powershell
.\ServerKit.bat install         # first-time setup (also repairs an install)
.\ServerKit.bat start           # start and open http://localhost
.\ServerKit.bat stop
.\ServerKit.bat status
.\ServerKit.bat setup-code      # code asked for when creating the first admin
.\ServerKit.bat update
.\ServerKit.bat cli list-users  # any `serverkit` CLI command
.\ServerKit.bat shell           # root shell inside the Linux environment
.\ServerKit.bat theme           # apply the flat black/white square theme
.\ServerKit.bat autostart-on    # start with Windows (autostart-off to undo)
.\ServerKit.bat uninstall       # deletes the environment and everything in it
```

`install -Profile minimal|standard|full` chooses what goes on the box
(`standard`, the default, includes Docker), exactly as on Linux.

## What it sets up

- A WSL2 distro named `ServerKit` (Ubuntu 24.04), separate from any other
  distro on the PC. Removing it removes everything this installer created.
- `/etc/wsl.conf` inside it: systemd on, root as the default user, and Windows
  programs kept off `PATH` so the panel only sees tools that really run there.
- ServerKit itself in `/opt/serverkit`, as on any Linux server.

## Things to know

- **It manages the Linux environment, not Windows itself.** Sites, databases
  and Docker apps live inside the distro.
- **The panel is at `http://localhost` (port 80)** and is reachable from this
  PC only. Windows port 80 must be free.
- **WSL stops an idle distro**, so `start` keeps one hidden `wsl.exe` session
  open. `stop` ends it; so does signing out of Windows.
- The firewall page works inside the distro, but the installer does not turn
  UFW on by itself there — the same decision it makes in any container, since
  Windows is the machine facing the network.
- Plain HTTP: there is no domain to issue a certificate for on a desktop.

`themes/mono-square.json` is an ordinary ServerKit theme (colours, radii and
shadows only — see `docs/THEMING.md`). `theme` installs it through the panel's
own theme service and makes it the default; switch back any time under
Settings → Appearance.

`_old-native-attempt/` keeps the earlier native-Windows experiment (a patch of
the three files it edited plus its scripts). It is not used.

## Sites (`sites/`)

Each folder describes one site published on the panel: a `Dockerfile` overlay
and a `site.json` (port, hostname, volumes, variables). `python
windows/sites/publish.py windows/sites/<folder>` creates or updates the site
through the panel's API. Sources come from read-only mirrors in `/srv/git`
inside the Linux environment, served on loopback by `serverkit-git-local`; the
overlay lives on a local `serverkit` branch there, so the GitHub repositories
are never written to. Generated passwords and secrets are stored as the
service's variables in the panel (Service → Environment).

## Remote link

`ServerKit.bat remote-on` opens the panel to the internet through a Cloudflare
quick tunnel (no account, no router changes, HTTPS included). The address is
random and changes whenever the tunnel restarts; `ServerKit.bat remote-link`
prints the current one. `remote-off` closes it. A permanent address needs a
domain on Cloudflare (named tunnel).

## Panel fixes (`panel-fixes/`)

`service-domain-link.patch`: the service page linked every domain as
`https://`, so plain-http domains (all `*.localhost` ones) failed to open. The
installed web interface was rebuilt from v1.11.20 with this patch; the
original is kept next to it as `frontend/dist.orig`. `serverkit update`
replaces the interface, so re-apply the patch after an update until it is
fixed upstream.

## Publish gateway & MCP (`gateway/`)

Lets an AI that is not on this PC publish a project to
`https://<name>.tadadigital.shop`. The gateway (`gateway/gateway.py`, systemd
unit `serverkit-gateway` in the distro, 127.0.0.1:8711) sits behind the tunnel
and offers two front doors with the same bearer key:

- REST: `GET /` (instructions, no key), `GET /v1/sites`, `POST /v1/publish`,
  `GET /v1/jobs/<id>`.
- MCP (Streamable HTTP, plain JSON): `https://deploy.tadadigital.shop/mcp`,
  tools `get_instructions`, `list_sites`, `publish_site`, `get_job`.

Owner commands:

```powershell
.\ServerKit.bat gateway-prompt   # writes the ready prompt with the key to Desktop\serverkit-yayin-metni.txt
.\ServerKit.bat gateway-rotate   # replaces the key
.\ServerKit.bat gateway-on       # start the gateway
.\ServerKit.bat gateway-off      # stop the gateway
```

The panel shows it in Settings -> API: the "Remote publish & MCP" card (status,
start/stop, MCP and REST addresses, reveal key, generate new key, ready-made
Claude Code command and MCP JSON) and the "Auto deploy" table with one switch
per service. Every service page also has an "Auto deploy" switch. This is
provided by the builtin extension `serverkit-remote-publish` (admin only, API
under `/api/v1/remote-publish`).

Safety limits (enforced by the gateway):

- only `hasaca-bot/*` repositories;
- only `<name>.tadadigital.shop` addresses;
- only new sites, or sites the gateway created (`origin: gateway` in `site.json`);
- no delete;
- one job at a time;
- 10 bad keys per IP per 10 minutes -> HTTP 429;
- request body up to 256 KB.

### Tools added 2026-10-16 (verified end to end)

The gateway now has 17 MCP tools: `get_instructions`, `list_sites`, `publish_site`, `get_job`, `site_info`, `set_env`, `get_logs`, `redeploy_site`, `stop_site`, `start_site`, `create_database`, `delete_database`, `list_cron`, `add_cron`, `delete_cron`, `run_command`, `delete_site`. REST mirrors: `GET /v1/sites/<site>` (site_info) and `POST /v1/sites/<site>/<action>` with action `env_set`, `logs`, `redeploy`, `stop`, `start`, `db_create`, `db_delete`, `cron_list`, `cron_add`, `cron_delete`, `exec`, `delete_site`.

- Ownership: every tool except `publish_site`, `list_sites`, `get_instructions` and `get_job` works only on sites created through the gateway (`origin: gateway` in `site.json`). Other sites, the panel, users, firewall and the host shell are out of reach.
- Long actions (`publish_site`, `set_env`, `redeploy_site`, `create_database`, `delete_database`, `delete_site`) return a job id to poll with `get_job`; the others answer directly.
- `create_database`: engine `postgresql`, `mysql`, `mariadb` or `redis`; at most 2 per site and one per engine. Creates container `<site>-<engine>`, stores `DATABASE_URL` (`REDIS_URL` for redis) on the site as a secret, links both containers on network `serverkit-services` (service `serverkit-netlink` keeps the link after every redeploy) and redeploys the site. Verified: the site container reached `<site>-postgresql:5432` using `DATABASE_URL`.
- `set_env`: UPPER_SNAKE_CASE keys; `SERVERKIT_*` and database-owned keys are refused; values may be `{"generate": "hex32"|"password"|"fernet"}`; redeploys by default.
- `run_command` and `add_cron` run only inside the site's own container (docker exec); argv words limited to `A-Z a-z 0-9 _ . / : = @ , + % -` (no spaces, quotes or shell characters); at most 10 cron jobs per site, not more often than every 5 minutes.
- `stop_site` / `start_site` use the panel's sleep/wake.
- `delete_site` needs confirm text `DELETE <site>` and removes everything: databases, cron jobs, containers, volumes, images, panel service, tunnel hostname, local mirror and recipe. The "no delete" safety limit above applies to `publish_site` only; `delete_site` is the exception.
- Limits: 30 authenticated requests per minute per IP; 10 bad keys per IP per 10 minutes -> HTTP 429.
- Code: `gateway/gateway.py` (runs in the distro, MCP/REST), `gateway/remote_ops.py` (Windows-side worker), `gateway/netlink.sh` (installed as `/usr/local/sbin/serverkit-netlink`).

## Automatic brain notes (`brain/`)

`brain/note.py` keeps the HASACA-beyin vault current without anyone asking:
it collects what changed in this setup since its last run (files under
`windows/`, the remote-publish extension and the serverkit skill, new
auto-deploy log lines, remote publishes through the gateway, services added
or changed in the panel) and, only when something changed, runs a Haiku agent
(`claude -p --model haiku`) in the vault that adds the facts to
`10_PROJELER/serverkit-windows/CURRENT-STATE.md` and the day's log, commits
and pushes those files. Secrets are masked before anything leaves the script.
It runs from the Claude Code Stop hook `~/.claude/hooks/serverkit-note.sh`
(every session, in the background) and from the scheduled task
"ServerKit Brain Notes" (every 30 minutes, for things that happen without a
Claude session: cloud AIs publishing, auto-deploys). Log and state:
`%LOCALAPPDATA%\ServerKit\brain\`.

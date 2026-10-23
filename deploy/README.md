# Deploying to marvin

Every push to `main` runs `.github/workflows/deploy.yml`:

1. **image**: builds the Dockerfile and pushes `ghcr.io/hatstand/triptych:<sha>` (and `:main`)
   using the job's own `GITHUB_TOKEN`.
2. **deploy**: joins the tailnet as an ephemeral `tag:ci` node through Tailscale **workload
   identity federation** (GitHub's OIDC token is exchanged for a short-lived Tailscale credential,
   so nothing secret is stored in GitHub), then runs `tailscale ssh deploy@marvin <sha>`.
   On marvin, the `deploy` user's login shell is [`triptych-deploy`](triptych-deploy). It checks
   out that commit, pulls the image and runs `docker compose up -d`.

No long-lived credential exists anywhere. The repo and the GHCR package are public, so marvin
needs no registry login. The Tailscale client ID and audience aren't secrets either.

## One-time setup

### Tailscale (admin console)

Tailscale SSH only allows a tagged source (the CI runner) to reach a **tagged** destination, so
marvin has to become `tag:marvin`. Tagged devices aren't `autogroup:member`, so they also need
their own Funnel attribute and any access rules you rely on for your own devices.

Add to the tailnet policy file:

```jsonc
"tagOwners": {
  "tag:ci":     ["autogroup:admin"],
  "tag:marvin": ["autogroup:admin"],
},
"grants": [
  // CI may reach only marvin's SSH port.
  {"src": ["tag:ci"], "dst": ["tag:marvin"], "ip": ["22"]},
  // Keep your own access to marvin (skip if you still allow everything).
  {"src": ["autogroup:member"], "dst": ["tag:marvin"], "ip": ["*"]},
],
"ssh": [
  {"action": "accept", "src": ["tag:ci"], "dst": ["tag:marvin"], "users": ["deploy"]},
  // autogroup:self no longer covers marvin once it's tagged.
  {"action": "check", "src": ["autogroup:member"], "dst": ["tag:marvin"], "users": ["autogroup:nonroot", "root"]},
],
"nodeAttrs": [
  // Funnel for /inbound keeps working once marvin is tagged.
  {"target": ["tag:marvin"], "attr": ["funnel"]},
],
// Checked on every policy save: CI gets SSH as deploy and nothing else on marvin.
"tests": [
  {"src": "tag:ci", "accept": ["tag:marvin:22"],
   "deny": ["tag:marvin:8000", "tag:marvin:9070", "tag:marvin:11434"]},
],
"sshTests": [
  {"src": "tag:ci", "dst": ["tag:marvin"], "accept": ["deploy"], "deny": ["root"]},
],
```

Then:

1. Tag marvin: Machines → marvin → **Edit ACL tags** → `tag:marvin`.
2. Trust credentials → **Credential** → **OpenID Connect** → issuer **GitHub**.
   Subject: `repo:hatstand@314491/triptych@1430922288:environment:marvin`. This repo's tokens
   carry the owner and repo IDs; if it doesn't match, Tailscale shows the subject it received.
   Scope: **Auth Keys (write)** with tag `tag:ci`. Copy the **Client ID** and **Audience**.

### GitHub (repo settings)

1. Environments → **New environment** `marvin` → Deployment branches: **Selected branches** → `main`.
2. In that environment, add **variables** (not secrets) `TS_OAUTH_CLIENT_ID` and `TS_AUDIENCE`.
3. After the first image build, set the `triptych` package to **Public** (Packages → triptych →
   Package settings) so marvin can pull without logging in.

### marvin

```bash
sudo tailscale set --ssh
sudo install -m 0755 deploy/triptych-deploy /usr/local/bin/triptych-deploy
echo /usr/local/bin/triptych-deploy | sudo tee -a /etc/shells
sudo useradd --system --create-home --shell /usr/local/bin/triptych-deploy --groups docker deploy
sudo git clone https://github.com/hatstand/triptych /srv/triptych
sudo cp .env /srv/triptych/.env            # your current .env, with COMPOSE_FILE set
sudo chown -R deploy: /srv/triptych
```

The compose project is named `triptych` regardless of directory, so the Restate and Ollama volumes
carry over from wherever you run it today. Stop any Tilt session first, since it uses the same
project name.

Membership in the `docker` group is effectively root on marvin. The restricted login shell and the
`environment: marvin` trust subject are what keep that down to "deploy a commit from main".

### First sign-in

The UI only accepts passkeys, and the first one needs an invite. On marvin:

```bash
cd /srv/triptych && sudo -u deploy docker compose exec web python -m triptych.invite you@example.com
```

Open the printed link (it uses `PUBLIC_BASE_URL`, which must be the address you browse to, e.g.
`https://triptych.axolotl-goblin.ts.net`) and create a passkey. Add a second one from the account
page so a lost device doesn't lock you out; if it does, issue yourself another invite.

### Restate UI

The Restate admin API + UI stays on marvin's loopback unless you set `RESTATE_ADMIN_BIND` in
`/srv/triptych/.env` to marvin's Tailscale IP (`tailscale ip -4`). Then it's at
`http://marvin:9070` from the tailnet, while Postgres, ingress, web and smtp stay on loopback. It
has no auth of its own, so the tailnet policy is what guards it (the `tag:ci` test above already
denies CI access to 9070).

If the docker service can start before tailscaled has the address, the bind fails and the
restate container doesn't come up. Allow binding addresses that aren't up yet:

```bash
echo 'net.ipv4.ip_nonlocal_bind = 1' | sudo tee /etc/sysctl.d/99-triptych.conf && sudo sysctl --system
```

Without `RESTATE_ADMIN_BIND`, tunnel instead: `ssh -L 9070:127.0.0.1:9070 marvin`.

## Admin pages

`https://triptych.axolotl-goblin.ts.net/admin` shows ingest status across every user and whether
the LLM is up. There is no app login: who you are comes from Tailscale.

`tailscale serve` deletes any `Tailscale-User-*` headers a client sends and, for requests from a
user-owned tailnet device, sets `Tailscale-User-Login` to that user's login. Tagged devices and
Funnel traffic get no identity header. This works the same for a Tailscale Service as for a
machine's own name; what matters is the device the request comes from, not that marvin is tagged.
The admin app accepts a request only if that login is listed in `ADMIN_TAILSCALE_LOGINS`.

The header is only trustworthy if Serve is the only way in, so the admin app (`compose` service
`admin`) listens on a Unix socket and nothing else. Funnel (`/inbound`), the compose network, and
any future public route all reach the web app on port 8000, which ignores these headers. Never
point a proxy at the admin socket. Being read-only, the admin page gives a cross-site request
from another tab nothing to trigger.

One-time setup on marvin (needs a Tailscale release whose `tailscale serve --help` lists
`unix:` targets):

```bash
sudo install -d -o 10001 -g 10001 -m 0700 /srv/triptych/run    # the image's `app` user
sudo tee -a /srv/triptych/.env <<'ENV'
COMPOSE_PROFILES=admin
ADMIN_TAILSCALE_LOGINS=john.maguire@gmail.com
ENV
sudo tailscale serve --service=svc:triptych --https=443 --set-path=/admin unix:/srv/triptych/run/admin.sock
```

The next deploy starts the `admin` service. The socket's directory is mode 0700, so only root
(which includes tailscaled) and the container can connect to it. `ADMIN_TAILSCALE_LOGINS` is a
comma-separated list; leaving it empty locks everyone out.

To link the Restate UI from the admin page, set `RESTATE_UI_URL` in `.env` to where a browser
reaches it, e.g. `http://marvin:9070` with `RESTATE_ADMIN_BIND` set (see "Restate UI" above).

## Manual deploy / rollback

Run the **Deploy** workflow from the Actions tab, or from a tailnet device that can SSH as
`deploy`: `tailscale ssh deploy@marvin <sha>`. Any commit on `main` that has an image works.

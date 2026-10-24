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
Once the admin pages below are set up, the **Invites** form there does the same from a browser.

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
point a proxy at the admin socket. Because Serve vouches for any request from your device,
including a form another site's tab posts, the invite form also requires a matching `Origin`
(`ADMIN_ORIGINS`, defaulting to `PUBLIC_BASE_URL`'s).

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
reaches it: `https://marvin.axolotl-goblin.ts.net:9070` if you serve it with
`tailscale serve --bg --https=9070 127.0.0.1:9070`, or `http://marvin:9070` with `RESTATE_ADMIN_BIND`.

## Public access

`https://triptych.purplehatstands.com` goes through a Cloudflare Tunnel: `cloudflared` on marvin
(`compose/public.yaml`) keeps outbound connections open to Cloudflare, whose edge serves the name,
terminates TLS and sends requests down the tunnel to the web app. Nothing at home is forwarded, so
the ISP gateway (which drops inbound 443) and the double NAT don't matter, and marvin's address
isn't published. Cloudflare sees the traffic in the clear, as it already does for mail.

What the internet can reach is the web app: sign-in, invite links, the UI, calendar feeds and
`/inbound`. It can't reach `/admin` (a Unix socket behind `tailscale serve`; the web app has no
such route), the Restate UI (loopback, served on the tailnet by `tailscale serve --https=9070`),
Postgres, Ollama or SMTP (`compose/public.yaml` unpublishes them). Sign-in, registration and the
invite page are rate limited per client address, adding plans and asking questions per user
(`src/triptych/web/ratelimit.py`). Accounts still only come from invites.

Passkeys are tied to their RP ID, so the ones made on `triptych.axolotl-goblin.ts.net` don't work
on the public name; everyone re-registers from an invite after the switch. The ts.net name keeps
serving `/admin` (and the `/static` it uses), but signing in there stops working. Calendar feed
URLs change too (new host and `SECRET_KEY`), so re-subscribe.

### One-time setup

1. **Secret key.** In `/srv/triptych/.env`, set `SECRET_KEY` to a new random value
   (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`). The repo is public, so the
   default is too, and it signs calendar feed URLs and passkey ceremony state.
   `compose/public.yaml` refuses to start without it. (The Postgres password can stay: the overlay
   unpublishes its port, so only compose neighbours, which hold `DATABASE_URL` anyway, reach it.)

2. **DNS**: delete any existing `triptych` record in the purplehatstands.com zone; the tunnel
   creates its own (a proxied CNAME to `<tunnel id>.cfargotunnel.com`). Mail at
   `@triptych.purplehatstands.com` isn't possible alongside it; use the apex for mail.

3. **Tunnel** (Cloudflare dashboard → Zero Trust → Networks → Tunnels → **Create a tunnel** →
   Cloudflared): name it `triptych`, and copy the token from the install command it shows
   (the long string after `--token`). Under **Public hostnames** add subdomain `triptych`,
   domain `purplehatstands.com`, service **HTTP** `web:8000` (the compose service name). Put the token in `/srv/triptych/.env`
   as `TUNNEL_TOKEN=...`. It is a credential for running this tunnel and nothing else; if it
   leaks, rotate it from the tunnel's page.

4. **Edge settings** (purplehatstands.com → SSL/TLS → Edge Certificates): turn on **Always Use
   HTTPS** and **HSTS** (max-age 6 months or more, no preload needed).

5. **Switch over**: add to `/srv/triptych/.env` (keep the other lines):

   ```bash
   COMPOSE_FILE=compose.yaml:compose/ollama.yaml:compose/gpu-rocm.yaml:compose/public.yaml
   PUBLIC_BASE_URL=https://triptych.purplehatstands.com
   WEBAUTHN_RP_ID=purplehatstands.com
   ADMIN_ORIGINS=https://triptych.axolotl-goblin.ts.net
   ```

   `compose/public.yaml` needs Docker Compose 2.24.4 or later (`docker compose version`). Then
   redeploy (re-run the **Deploy** workflow, or `tailscale ssh deploy@marvin <sha>`). The
   `--remove-orphans` in the deploy script is fine: `cloudflared` is part of the project now.
   Remove the home router's 443 forward; nothing uses it.

6. **Check**: the tunnel shows **Healthy** in the dashboard, `docker compose logs cloudflared`
   shows registered connections, and `https://triptych.purplehatstands.com/login` loads.

7. **Passkeys**: open `https://triptych.axolotl-goblin.ts.net/admin`, create an invite for
   yourself (the link now points at the public host), and register a passkey from it; it's added
   to your existing account. Delete the old ts.net passkeys from the account page afterwards.

Once the Email Worker posts to `https://triptych.purplehatstands.com/inbound`, turn Funnel off
(`tailscale funnel status` to see what's on). Keep the ts.net `tailscale serve` for `/`, though:
the admin pages load their CSS and scripts from it.

To go back to tailnet-only, remove `:compose/public.yaml` and the lines above from `.env` and
redeploy. Passkeys registered on the public name then stop working on the ts.net name instead.

## Manual deploy / rollback

Run the **Deploy** workflow from the Actions tab, or from a tailnet device that can SSH as
`deploy`: `tailscale ssh deploy@marvin <sha>`. Any commit on `main` that has an image works.

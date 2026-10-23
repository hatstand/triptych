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

The deploy job is skipped until the `TS_OAUTH_CLIENT_ID` variable exists, so merging this before
the setup below only publishes images.

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
   Subject: `repo:hatstand/triptych:environment:marvin`. Scope: **Auth Keys (write)** with tag
   `tag:ci`. Copy the **Client ID** and **Audience**.

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

## Manual deploy / rollback

Run the **Deploy** workflow from the Actions tab, or from a tailnet device that can SSH as
`deploy`: `tailscale ssh deploy@marvin <sha>`. Any commit on `main` that has an image works.

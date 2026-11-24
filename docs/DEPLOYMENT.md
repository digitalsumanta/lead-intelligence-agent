# Deploying to a Linux server

This runs the whole stack on one server behind HTTPS at a domain such as `https://leads.mindartdigital.com`:

```
Internet ──443──> Caddy (automatic HTTPS)
                    ├── /api/*, /unsubscribe/*, /health, /ready ──> backend (FastAPI)
                    └── everything else ──────────────────────────> frontend (Next.js)
           internal only: database (PostgreSQL 16), worker, nightly backup
```

Files: `docker-compose.prod.yml`, `deploy/Caddyfile`, `deploy/backup-loop.sh`, `scripts/deploy.sh`, `.env.production.example`.

## 1. Server

- **Size:** Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM, 40 GB disk is comfortable (Hetzner CX22/CPX21, DigitalOcean 4 GB, Lightsail 4 GB). With 2 GB RAM, add swap (step 3).
- **AI:** use Gemini in production. Running Ollama on a small CPU server is too slow.
- **Region:** pick one close to your users (for example Singapore or Mumbai for India).

## 2. DNS

At your DNS provider, create an **A** record `leads.mindartdigital.com` pointing to the server's IPv4 address (and an **AAAA** record for IPv6 if available). Wait until `nslookup leads.mindartdigital.com` returns the server IP.

## 3. Prepare the server (as root, once)

```bash
adduser deploy && usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh && cp ~/.ssh/authorized_keys /home/deploy/.ssh/ && chown -R deploy:deploy /home/deploy/.ssh

# SSH: keys only, no root login
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh

# Firewall: SSH, HTTP, HTTPS only
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable

# Automatic security updates
apt update && apt install -y unattended-upgrades git curl && dpkg-reconfigure -plow unattended-upgrades

# Docker
curl -fsSL https://get.docker.com | sh && usermod -aG docker deploy

# Optional on 2 GB servers: 2 GB swap
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

Log in again as `deploy` for the remaining steps.

## 4. Get the code (read-only deploy key)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "leads-server"
cat ~/.ssh/github_deploy.pub
```

Add that public key in GitHub under **Repository → Settings → Deploy keys → Add deploy key** (leave *Allow write access* unchecked). Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
EOF
git clone git@github.com:digitalsumanta/lead-intelligence-agent.git ~/lead-intelligence-agent
cd ~/lead-intelligence-agent
```

## 5. Configure

```bash
cp .env.production.example .env.production && chmod 600 .env.production && nano .env.production
```

Copy these from your current `.env` unchanged:

| Setting | Why it must stay the same |
| --- | --- |
| `CREDENTIAL_ENCRYPTION_KEY` | Provider keys in the migrated database are encrypted with it |
| `OUTREACH_UNSUBSCRIBE_SECRET` | Unsubscribe links in already-sent e-mails depend on it |
| AI, Places, Resend and sender settings | Same behaviour as today |

Create these fresh:

- `POSTGRES_PASSWORD`: a long random value (`openssl rand -base64 32 | tr -d '/+='`)
- `API_AUTH_TOKEN`: a new long random value

Set `DOMAIN`, `PUBLIC_URL` (`https://` plus the domain) and `ACME_EMAIL`.

## 6. Move your data from the PC

On the PC (PowerShell, in the project folder):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\backup-db.ps1
scp backups\lead_agent_<newest>.dump deploy@<server-ip>:~/lead-intelligence-agent/backups/
```

On the server:

```bash
C="docker compose -f docker-compose.prod.yml --env-file .env.production"
$C up -d database
$C cp backups/lead_agent_<newest>.dump database:/tmp/restore.dump
$C exec database sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists /tmp/restore.dump; rm /tmp/restore.dump'
```

User accounts, leads, the suppression list and encrypted provider keys all come across. `pg_restore` may print a few harmless warnings about objects that did not exist yet.

## 7. Deploy

```bash
./scripts/deploy.sh --no-pull
```

The script builds the images, takes a backup, runs migrations, starts everything and checks that the backend is ready and the public URL answers. The first HTTPS certificate takes a few seconds. Then open `https://leads.mindartdigital.com` and sign in with your existing account.

## 8. After the first deploy

- **Delivery webhooks:** add a Resend webhook to `https://leads.mindartdigital.com/api/sales-supervisor/webhooks/delivery/resend`, put its signing secret in `OUTREACH_RESEND_WEBHOOK_SECRET`, and run `./scripts/deploy.sh --no-pull`. Bounces, complaints and unsubscribes are then suppressed automatically.
- **Unsubscribe links:** they now work automatically, because `OUTREACH_UNSUBSCRIBE_BASE_URL` is set to `PUBLIC_URL`. New e-mails carry one-click unsubscribe headers.
- **Sending domain:** follow [SENDING_DOMAIN.md](SENDING_DOMAIN.md).
- **Uptime monitoring:** add a free check (for example UptimeRobot or Better Stack) on `https://leads.mindartdigital.com/health`.
- **Retire the PC stack** once you're happy:
  1. `docker compose down` in the project folder on the PC (the data volume stays until you remove it).
  2. Remove the Windows backup task: `Unregister-ScheduledTask -TaskName "Lead Intelligence - nightly database backup"`.

## Day-to-day operations

| Task | Command (in `~/lead-intelligence-agent`) |
| --- | --- |
| Deploy the latest `main` | `./scripts/deploy.sh` |
| Status | `docker compose -f docker-compose.prod.yml --env-file .env.production ps` |
| Logs | `docker compose -f docker-compose.prod.yml --env-file .env.production logs -f backend worker` |
| Backup now | `docker compose -f docker-compose.prod.yml --env-file .env.production run --rm backup now` |
| Create a user | `docker compose -f docker-compose.prod.yml --env-file .env.production exec backend python -m app.cli users create --email … --role …` |

**Backups:**
- A dump is written to `~/lead-intelligence-agent/backups/` nightly at `BACKUP_TIME` (UTC), and before every deploy. 14 days are kept.
- Copy them off the server regularly, for example with `rclone` to Google Drive or S3: `rclone copy backups remote:lead-intelligence-backups`, run from a daily cron entry.
- Keep `CREDENTIAL_ENCRYPTION_KEY` outside those backups.

**Restore:** stop the app, restore a dump as in step 6, then start it again:

```bash
$C stop backend worker
# restore commands from step 6, using the chosen dump
$C up -d
```

**Roll back a bad deploy:**
1. Run `git checkout <previous-commit> && ./scripts/deploy.sh --no-pull`.
2. If the bad deploy included a database migration, also restore the pre-deploy backup it took.
3. Return to normal deploys with `git checkout main`.

**Metrics:** `/metrics` is blocked publicly. To use Prometheus, run it on the same Docker network and scrape `http://backend:8000/metrics` with `METRICS_TOKEN`.

## Security checklist

- [ ] SSH keys only, root login disabled, firewall allows only 22, 80 and 443
- [ ] `.env.production` is `chmod 600` and exists only on the server (and in your password manager)
- [ ] Unattended security upgrades enabled
- [ ] Database not published (no `ports:` on `database` in `docker-compose.prod.yml`)
- [ ] Backups copied off the server; a restore has been tested once
- [ ] Uptime check on `/health`

#!/usr/bin/env bash
# Deploy or update the production stack on the server.
#   ./scripts/deploy.sh            pull the latest main, build, migrate, restart, verify
#   ./scripts/deploy.sh --no-pull  deploy the code already checked out
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE=.env.production
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")

log() { printf '\n==> %s\n' "$*"; }

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE (copy .env.production.example and fill it in)"; exit 1; }
chmod 600 "$ENV_FILE"
for var in DOMAIN PUBLIC_URL ACME_EMAIL POSTGRES_PASSWORD CREDENTIAL_ENCRYPTION_KEY OUTREACH_UNSUBSCRIBE_SECRET API_AUTH_TOKEN; do
	grep -Eq "^${var}=.+" "$ENV_FILE" || { echo "$var is empty in $ENV_FILE"; exit 1; }
done
mkdir -p backups

if [ "${1:-}" != "--no-pull" ]; then
	log "Updating code"
	git fetch --quiet origin
	git checkout --quiet main
	git pull --ff-only --quiet origin main
fi
echo "Deploying commit $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"

log "Building images"
"${COMPOSE[@]}" build

log "Starting database"
"${COMPOSE[@]}" up -d database

log "Backing up before migrating"
"${COMPOSE[@]}" run --rm backup now || echo "WARNING: pre-deploy backup failed (continuing)"

log "Running migrations"
"${COMPOSE[@]}" run --rm migrate

log "Starting services"
"${COMPOSE[@]}" up -d --remove-orphans

log "Waiting for the application to become ready"
for _ in $(seq 1 60); do
	if "${COMPOSE[@]}" exec -T backend python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/ready', timeout=5).status == 200 else 1)" 2>/dev/null; then
		echo "Backend ready."
		break
	fi
	sleep 5
done
"${COMPOSE[@]}" ps

PUBLIC_URL=$(grep -E '^PUBLIC_URL=' "$ENV_FILE" | cut -d= -f2-)
if curl -fsS -o /dev/null "$PUBLIC_URL/health"; then
	echo "Public endpoint OK: $PUBLIC_URL"
else
	echo "WARNING: $PUBLIC_URL/health not reachable yet (DNS or certificate may still be propagating)"
fi

docker image prune -f >/dev/null
log "Deploy complete"

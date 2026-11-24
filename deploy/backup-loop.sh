#!/bin/sh
# Nightly PostgreSQL backups for the production stack (runs inside the "backup" service).
# Writes /backups/lead_agent_<UTC timestamp>.dump (custom format, restore with pg_restore),
# verifies each archive, and deletes dumps older than BACKUP_RETENTION_DAYS (keeping >= 3).
set -eu

BACKUP_TIME="${BACKUP_TIME:-02:00}"            # UTC, HH:MM
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
export PGPASSWORD="$POSTGRES_PASSWORD"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') backup: $*"; }

backup_once() {
	stamp=$(date -u '+%Y%m%d-%H%M%S')
	tmp="/backups/.lead_agent_${stamp}.dump.partial"
	out="/backups/lead_agent_${stamp}.dump"
	if ! pg_dump -h database -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$tmp"; then
		rm -f "$tmp"; log "FAIL pg_dump"; return 1
	fi
	tables=$(pg_restore -l "$tmp" | grep -c "TABLE DATA" || true)
	if [ "${tables:-0}" -lt 1 ]; then
		rm -f "$tmp"; log "FAIL archive unreadable or empty"; return 1
	fi
	mv "$tmp" "$out"
	log "OK $(basename "$out") $(du -k "$out" | cut -f1) KB, $tables tables"
	# Retention: delete old dumps but always keep the newest 3.
	ls -1t /backups/lead_agent_*.dump 2>/dev/null | tail -n +4 | while read -r f; do
		if [ -n "$(find "$f" -mtime +"$RETENTION_DAYS" 2>/dev/null)" ]; then
			rm -f "$f"; log "DEL $(basename "$f")"
		fi
	done
}

if [ "${1:-}" = "now" ]; then
	backup_once
	exit $?
fi

log "scheduler started: daily at ${BACKUP_TIME} UTC, ${RETENTION_DAYS}-day retention"
while true; do
	now=$(date -u +%s)
	target=$(date -u -d "$(date -u +%Y-%m-%d) ${BACKUP_TIME}" +%s 2>/dev/null || echo 0)
	[ "$target" -le "$now" ] && target=$((target + 86400))
	sleep $((target - now))
	backup_once || log "will retry at the next scheduled time"
done

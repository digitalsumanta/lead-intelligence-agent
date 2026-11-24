# Security and operations

How sign-in, credential storage, e-mail compliance, CI and monitoring work. See the [README](../README.md) for setup.

## CI and monitoring

**Continuous integration.** `.github/workflows/ci.yml` runs on pushes to `main`/`master` and on pull requests:

- *Backend:* `ruff check backend` (rules in `ruff.toml`: syntax errors, undefined names and redefinitions), the full pytest suite against a PostgreSQL 16 service (including the migration tests), then a fresh `alembic upgrade head` and the PostgreSQL integration tests (`-m postgres_integration`).
- *Frontend:* `npm ci`, `tsc --noEmit`, and `next build`.
- *Dependency audit:* `pip-audit -r backend/requirements.txt` and `npm audit --omit=dev --audit-level=high`.

Run the same checks locally with `ruff check backend` and `python -m pytest backend/tests`.

**Request IDs.** Every response carries `X-Request-ID` (a safe client-supplied value of 8–64 characters `[A-Za-z0-9._-]` is reused; otherwise one is generated). The ID appears as `request_id` in every JSON log line written while the request is handled and in the `details` of `INTERNAL_ERROR` responses, so a reported error can be found in the logs.

**Metrics.** Set `METRICS_TOKEN` to enable `GET /metrics` (Prometheus text format; scrape with `Authorization: Bearer <METRICS_TOKEN>`). Without it the endpoint returns 404. Metrics include HTTP request counts and latency by route template, unhandled errors, lead processing jobs and outreach dispatches by status, seconds since the last worker heartbeat (`-1` when none), provider request outcomes over the last 15 minutes, and suppression-list size by reason. Useful alerts: worker heartbeat age above 120 s, any growth in `lead_agent_http_unhandled_errors_total`, dispatches stuck in `OUTCOME_UNKNOWN`, and a rising provider failure share.

**Error tracking.** Set `SENTRY_DSN` to send unhandled exceptions from the API and both workers to Sentry (optional: `SENTRY_RELEASE`, `SENTRY_TRACES_SAMPLE_RATE`, default `0`). `send_default_pii` is off and request bodies are never attached.

## Users and sign-in

People sign in to the dashboard with their own e-mail address and password. Accounts live in `app_users` (migration `0044`); passwords are stored only as scrypt hashes. A successful `POST /api/auth/login` sets an HttpOnly, `SameSite=Lax` session cookie (Secure in production/staging), and `auth_sessions` stores only a SHA-256 hash of the session token. `POST /api/auth/logout` ends the session and `GET /api/auth/me` returns the signed-in user. Sessions expire after `AUTH_SESSION_TTL_HOURS` (12 by default) and are invalidated when the password changes or the account is deactivated.

- **Roles.** `viewer`, `sales_rep`, `sales_manager`, and `admin`. The role comes from the account (never from request headers) and feeds the existing supervisor checks; `sales_manager` and `admin` can approve outreach.
- **CSRF protection.** Cookie-authenticated requests that change state must send `X-Requested-With: lead-agent` and, when the browser sends an `Origin`, it must be listed in `CORS_ORIGINS`.
- **Brute-force protection.** An account locks for `AUTH_LOCKOUT_MINUTES` after `AUTH_MAX_FAILED_LOGINS` consecutive failures, and each client IP is limited to 20 sign-in attempts per 10 minutes. Unknown e-mail addresses and wrong passwords return the same error.
- **Managing users.** There is no self-registration. From `backend/`: `python -m app.cli users create --email ... --role ...`, `users list`, `users set-role`, `users reset-password`, `users deactivate`, `users activate`, and `users sign-out`. Passwords are prompted for (minimum 12 characters) or read with `--password-stdin`.
- **Automation.** `Authorization: Bearer <API_AUTH_TOKEN>` still works for scripts, with the identity `API_AUTH_USER_ID` and role `API_AUTH_ROLE`. Never give this token to a browser.

## Stored credential encryption

Provider API keys copied from the environment into `api_keys` are encrypted at rest with Fernet (AES-128-CBC with HMAC-SHA256, from `cryptography`) and stored as `enc:v1:<token>`; the application decrypts them only when leasing a key for a provider call. Set `CREDENTIAL_ENCRYPTION_KEY` (generate one with `cd backend; python -m app.cli keys generate`) and keep it outside the database, so database dumps and backups do not expose usable keys. The API refuses to store keys without it.

Migration `0045` encrypts any existing plaintext rows and stops with an error if `CREDENTIAL_ENCRYPTION_KEY` is missing. `python -m app.cli keys status` reports how many stored keys are encrypted. To rotate, put a new key first in `CREDENTIAL_ENCRYPTION_KEY` with the old key after a comma, restart, run `python -m app.cli keys reencrypt`, then remove the old key. If the encryption key is lost, delete the `api_keys` rows and restart so they are re-created from the environment.

## Email compliance and the suppression list

All three email send paths (the supervisor outbox with Resend/SendGrid, Phase 10E approved execution, and Phase 8F outreach operations) go through `backend/app/services/email_compliance.py`:

- **Suppression list.** `email_suppressions` (migration `0043`) is a global do-not-email list keyed by a SHA-256 hash of the normalized address; only a masked form is kept for display. Suppressed recipients are refused at enqueue time and again in the pre-flight guard immediately before the provider call; supervisor dispatches end `ABORTED` with `RECIPIENT_SUPPRESSED`.
- **Automatic suppression.** Delivery webhooks add the correlated recipient on a permanent bounce (`HARD_BOUNCE`), a spam complaint (`COMPLAINT`), or a provider unsubscribe event (`UNSUBSCRIBED`).
- **Unsubscribe links and headers.** Every email gets a footer with an unsubscribe link (or mailto instruction) plus the sender name and postal address, and `List-Unsubscribe` headers; with an HTTPS link it also gets `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058). The HTTP-gateway (`http_json`) path can only carry the footer, not headers.
- **Public unsubscribe page.** `GET /unsubscribe/{token}` shows a confirmation page and never changes state (mail scanners prefetch links); `POST /unsubscribe/{token}` records the opt-out and also serves mailbox providers' one-click requests. These routes sit outside `/api` and need no bearer token; tokens are HMAC-signed and contain no readable address. The backend must be reachable at `OUTREACH_UNSUBSCRIBE_BASE_URL` for links to work.
- **Manual management.** `GET/POST /api/email-suppressions` and `DELETE /api/email-suppressions/{id}` (bearer auth) list, add (for example when someone replies "unsubscribe"), and remove entries.
- **Fail closed.** With `OUTREACH_COMPLIANCE_ENFORCED=true` (default), email sends are blocked (`EMAIL_COMPLIANCE_NOT_CONFIGURED`) until `OUTREACH_UNSUBSCRIBE_SECRET`, `OUTREACH_SENDER_POSTAL_ADDRESS` and `OUTREACH_UNSUBSCRIBE_BASE_URL` and/or `OUTREACH_UNSUBSCRIBE_MAILTO` are set. See `.env.example`.

Deliverability itself (a dedicated sending domain or subdomain with SPF, DKIM and DMARC, and gradual warm-up) is configured at your DNS and email provider, not in this application.

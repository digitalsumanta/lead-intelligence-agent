# Lead Intelligence Agent

Lead research, qualification and outreach for a sales team, with every AI output grounded in collected evidence and every outbound message approved by a person.

- **Collect and enrich leads:** CSV import or business research, then website, Google Places and PageSpeed evidence gathered in resumable background jobs.
- **Deterministic intelligence:** opportunities, service recommendations, scores and lead qualification derived from stored evidence, never invented.
- **Evidence-grounded AI:** sales intelligence, copilot answers, action plans, proposals and outreach drafts. Each factual claim must cite stored evidence, and invalid output is rejected rather than saved.
- **Sales workflow:** pipeline stages, activities, tasks, collections and revenue reporting.
- **Human-controlled outreach:** drafts, review, supervisor approval, canary flights, a transactional outbox, delivery webhooks and an e-mail suppression list.

The backend is FastAPI with async SQLAlchemy 2, PostgreSQL 16 and Alembic, plus separate worker processes. The dashboard is Next.js 15. Ollama is the default AI provider; Gemini and OpenAI-compatible endpoints are optional.

## Quick start (Windows / PowerShell)

Requires Python 3.12+, Node 20+, PostgreSQL 16 and, for local AI, [Ollama](https://ollama.com) with `ollama pull qwen2.5-coder:latest`.

1. **Configure.** Copy `.env.example` to `.env` and set at least:
   - `POSTGRES_PASSWORD` and `DATABASE_URL`
   - `API_AUTH_TOKEN`: a long random value, used only by scripts
   - `CORS_ORIGINS`: the dashboard URL
   - `CREDENTIAL_ENCRYPTION_KEY`: generate it after step 2 with `cd backend; python -m app.cli keys generate`
2. **Install.**
   ```powershell
   python -m pip install -r backend/requirements-dev.txt
   cd frontend; npm install; cd ..
   ```
3. **Migrate.** The app never creates tables itself and refuses to start unless the database is at the latest migration.
   ```powershell
   $env:PYTHONPATH = "backend"; python -m alembic -c backend\alembic.ini upgrade head
   ```
4. **Create the first account.** There is no self-registration.
   ```powershell
   cd backend; python -m app.cli users create --email you@example.com --role admin --name "Your Name"; cd ..
   ```
5. **Run.** `./scripts/start-dev.ps1` starts the API on port 8000, the lead-processing worker, and the dashboard on port 3000. To also run the outreach outbox worker, start both workers with `cd backend; python -m app.worker` instead. Alternatively, `docker compose up --build` runs everything, applies migrations automatically, and serves the dashboard on port 3001.

Then open the dashboard and sign in. `GET /health` is a liveness check; `GET /ready` also checks the database, the AI provider and the worker heartbeat.

## Architecture

```
Browser (Next.js dashboard)  --cookie session-->  FastAPI API  --->  PostgreSQL
                                                     |   ^
                              queue jobs / outbox    v   |  claim with FOR UPDATE SKIP LOCKED
                                                  Workers: lead processing + outreach outbox
                                                     |
                     AI providers (Ollama/Gemini/OpenAI-compatible), Google Places, PageSpeed, Resend/SendGrid
```

- **The API does not run long work inline.** It validates requests, authenticates them and queues durable jobs, which the worker claims atomically. A unique index allows at most one active job per lead, and jobs retry with backoff.
- **Outreach is sent only by the outbox worker**, after human approval. A send must pass pre-flight guards immediately before the provider call: kill switch, lease fencing, canary allow-list and flight, and suppression list.
- **AI output is validated before it is stored.** Each claim must reference evidence or opportunities from the lead's current data, and raw provider responses and credentials are never persisted.
- **Alembic owns the schema.** Startup checks that the database is at `app.database.SCHEMA_REVISION`, and a test fails if that constant falls behind the latest migration.

### Code map

| Path | Contents |
| --- | --- |
| `backend/app/main.py` | App setup: auth/CSRF middleware, request IDs and metrics, error envelope, `/health`, `/ready`, `/metrics`, router registration |
| `backend/app/api/routes/` | Endpoints by area: `leads`, `jobs`, `sales_crm`, `outreach`, `ai_agents`, `seo`, `company_intelligence`, `business_research`, `contact_*`, `sales_execution`, `sales_feedback`, `intelligence_orchestrator`, `supervisor_*` (approvals, dispatch, delivery webhooks, canary), `email_compliance`, `auth` |
| `backend/app/services/` | Business logic: enrichment pipeline, deterministic intelligence, AI services, outreach, supervisor dispatch, e-mail compliance, auth, credential encryption |
| `backend/app/domain/`, `schemas/`, `models/` | Pure rules, Pydantic contracts, SQLAlchemy models |
| `backend/app/providers/` | AI, Places, PageSpeed and e-mail provider adapters |
| `backend/app/workers/` | Lead-processing worker (`runner.py`) and outreach outbox worker (`outbox_worker.py`); `app/worker.py` runs both |
| `backend/app/cli.py` | Admin commands for users and stored-credential encryption |
| `backend/migrations/` | Alembic revisions `0001`–`0045` |
| `frontend/` | Next.js dashboard (`app/`), API client (`lib/api.ts`) |

## Configuration

`.env.example` documents every setting. The most important:

| Setting | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection (`postgresql+asyncpg://…`) |
| `APP_ENV` | `production`/`staging` disables development-only approval shortcuts and makes cookies Secure |
| `API_AUTH_TOKEN` | Bearer token for scripts and automation, never for browsers |
| `AI_DEFAULT_PROVIDER`, `OLLAMA_*`, `AI_GEMINI_*`, `AI_OPENAI_COMPATIBLE_*` | AI provider selection and credentials |
| `GOOGLE_PLACES_*`, `PAGESPEED_*` | Optional enrichment sources (off by default) |
| `CREDENTIAL_ENCRYPTION_KEY` | Encrypts provider keys stored in the database (required when any are configured) |
| `OUTREACH_DISPATCH_ENABLED`, `OUTREACH_CANARY_MODE`, `OUTREACH_CANARY_ALLOWED_DOMAINS` | Master send switch and canary restrictions; sending is off by default |
| `OUTREACH_UNSUBSCRIBE_*`, `OUTREACH_SENDER_*` | Unsubscribe links and the postal address required on every e-mail; sends are blocked until set |
| `METRICS_TOKEN`, `SENTRY_DSN` | Prometheus metrics and Sentry error tracking (both optional) |

## Security model

- **Users:** individual accounts with scrypt password hashes, HttpOnly session cookies, roles (`viewer`, `sales_rep`, `sales_manager`, `admin`), CSRF protection, account lockout and login rate limiting.
- **Outreach:** human approval with role checks, a canary mode, a kill switch, a global suppression list, and unsubscribe links plus one-click headers on every e-mail.
- **Secrets:** provider keys are encrypted at rest; tokens, keys and raw provider errors are never logged or returned to the browser.
- **Outbound fetches:** website requests reject private and local addresses and re-check every redirect.

Details: [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Development

```powershell
ruff check backend                          # lint gate (rules in ruff.toml)
$env:PYTHONPATH = "backend"; python -m pytest backend/tests
cd frontend; npx tsc --noEmit; npm run build
```

- The default test suite uses in-memory SQLite. The migration tests (`test_migration_00xx`) also need a local PostgreSQL at `postgres:postgres@localhost:5432`; each creates and drops its own temporary database.
- Tests never read your `.env`; `backend/tests/conftest.py` supplies safe settings.
- For the PostgreSQL integration tests, migrate a dedicated database to head, then run with `TEST_POSTGRES_URL=postgresql+asyncpg://…` and `-m postgres_integration`.
- `.github/workflows/ci.yml` runs all of the above, plus `pip-audit` and `npm audit`, on every push and pull request.

When adding a migration, bump `SCHEMA_REVISION` in `backend/app/database.py`; `test_schema_revision.py` enforces this.

## Further documentation

- [docs/OPERATIONS.md](docs/OPERATIONS.md): users and sign-in, credential encryption, e-mail compliance and the suppression list, CI, request IDs, metrics and Sentry.
- [docs/FEATURES.md](docs/FEATURES.md): the detailed reference for each feature area and its API contract (jobs, CSV, outreach preparation and review, sales workflow, revenue, SEO, AI sales intelligence, copilot, agent policy, supervisor, autonomous tasks, approved e-mail execution, delivery events).
- `reports/`: Phase 12 reliability audits and closure reports.

## Current limitations

- **Settings and reporting:** there are no settings or report pages in the dashboard yet; configuration is through `.env`.
- **Leads list:** it has no full-text search or total counts.
- **Provider support:** AI providers are limited to Ollama, Gemini and OpenAI-compatible chat completions; e-mail providers to Resend and SendGrid (supervisor outbox), plus SMTP and an HTTP gateway (older send paths).
- **Unsubscribe links:** recipients must be able to reach this backend at `OUTREACH_UNSUBSCRIBE_BASE_URL`. Alternatively, use the mailto option and record opt-outs through `/api/email-suppressions`.
- **Sending domain:** the domain itself (SPF, DKIM, DMARC, warm-up) is configured with your DNS and e-mail provider.

# Lead Intelligence Agent

Local-first lead ingestion and enrichment. The backend uses FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic, and a separate durable-job worker. The frontend is Next.js. Ollama is the supported AI provider in this stabilization phase; cloud clients and Google integrations are not implemented yet.

## Architecture

- `backend/app/main.py` exposes REST endpoints. It validates requests, authenticates `/api/*`, and queues work; it does not run long lead analyses inline.
- `backend/app/models/entities.py` defines leads, import records, batches, processing jobs, events, provider usage, keys, and agent runs.
- `backend/app/services/` contains CSV validation, job state transitions, website inspection, scoring, and key-pool utilities.
- `backend/app/workers/runner.py` polls durable jobs. PostgreSQL workers claim rows with `FOR UPDATE SKIP LOCKED`; a conditional unique index prevents more than one active job per lead. A separate heartbeat task supports worker readiness checks while a long AI request is running.
- `backend/migrations/` contains explicit Alembic schema revisions. Alembic is the sole schema authority. API and worker startup require an already migrated database and never create tables.
- The dashboard queues work and polls job status; it does not process leads itself.

## Local development

Use Python 3.12+, Node 20+, PostgreSQL 16, and Ollama. Copy `.env.example` to `.env`, set a strong URL-safe PostgreSQL password and a long random `API_AUTH_TOKEN`, and set `NEXT_PUBLIC_API_TOKEN` to the same local development token so the browser dashboard can call the protected API. These `NEXT_PUBLIC_*` values are compiled into browser JavaScript; use this only for a trusted local development setup, not as production user authentication. Docker Compose refuses to start PostgreSQL with a missing password.

Install backend requirements with `python -m pip install -r backend/requirements.txt` and frontend dependencies with `cd frontend; npm install`. Start PostgreSQL and Ollama, then run `./scripts/start-dev.ps1` from the repository root (or `docker compose up --build` after setting `.env`). The script passes the local bearer token to the API and Next.js dashboard. Ollama defaults to `qwen2.5-coder:latest`; install that model with `ollama pull qwen2.5-coder:latest`. The default AI request timeout is 300 seconds. In Docker Compose the Ollama address defaults to `host.docker.internal`.

## Database migrations

The application never creates or upgrades schema at startup. From the repository root, with PostgreSQL reachable and `DATABASE_URL` configured:

```powershell
$env:PYTHONPATH = "backend"
python -m alembic -c backend\alembic.ini upgrade head
```

Then start the API and worker. Docker Compose runs `alembic upgrade head` in the backend container before starting Uvicorn; the worker waits for the API health check. Migration `0001_initial` explicitly defines the schema; `0002_active_job_guard` adds the partial unique index for active jobs; `0003_worker_heartbeat` adds worker liveness tracking. UUID, named PostgreSQL enums, JSON, timezone-aware timestamps, foreign keys, and indexes are declared explicitly. Unit tests use SQLite for service behavior and do **not** verify PostgreSQL migration compatibility.

For an older database created by the previous runtime `create_all()` startup path and lacking `alembic_version`, inspect and back up that database first. If its schema matches the original `0001_initial` model schema, baseline it with `python -m alembic -c backend\alembic.ini stamp 0001_initial`, then run `upgrade head`. Do not stamp a database whose schema has not been checked; a normal `upgrade head` against unversioned pre-existing tables will attempt to create them again.

## Job lifecycle and recovery

API processing requests persist `QUEUED` jobs and return job IDs. The worker claims queued or due retry jobs atomically, runs bounded concurrent work, and records `AgentRun`, `LeadEvent`, and provider usage history. Job attempts are limited by `max_attempts` (four by default) with 5, 15, and 60 second retry delays. A stale processing lease is moved to retrying only while attempts remain; exhausted jobs become failed. Batch state is updated to `QUEUED`, `PROCESSING`, `COMPLETED`, `PARTIALLY_FAILED`, or `FAILED` as jobs progress.

Checkpoints advance monotonically through validation, website analysis, AI analysis, scoring, result save, and completion. A known saved result is reused after interruption. External model execution has at-least-once semantics: a process crash after the provider returns but before the result transaction commits can require that AI request to run again. Unknown checkpoints fail the job in a controlled manner.

## API and CSV

`/health` is unauthenticated process liveness. `/ready` checks database schema, configured provider health, and whether a worker heartbeat is recent (30 second window). All `/api/*` routes require `Authorization: Bearer <API_AUTH_TOKEN>`. Endpoints include lead listing/details/import, single and batch job creation, job listing/details/retry, dashboard statistics, and provider status. CSV requires `company_name`; optional columns are website, phone, email, address, city, state, country, and industry. Upload size defaults to 5 MB.

The website analyzer only accepts HTTP(S), rejects localhost and non-public addresses, checks DNS destinations, manually follows a bounded number of redirects while revalidating them, applies a timeout, and caps response size. It does not crawl subpages.

## Tests

Run the default unit suite (SQLite, no PostgreSQL required):

```powershell
$env:PYTHONPATH = "backend"
python -m pytest
```

An opt-in PostgreSQL integration check is excluded from the default suite. Provision and migrate a dedicated PostgreSQL test database first, then set `TEST_POSTGRES_URL` to its `postgresql+asyncpg://` URL and run:

```powershell
$env:PYTHONPATH = "backend"
python -m pytest -m postgres_integration
```

That check requires the database to already be migrated to head. It inspects the real PostgreSQL `alembic_version` and table catalog; SQLite is not used as a PostgreSQL substitute. PostgreSQL integration is not claimed as verified until this opt-in check has run successfully against PostgreSQL.

## Current limitations

Only Ollama is executable; Gemini and OpenAI-compatible providers, Google Places, and PageSpeed remain future work. API bearer-token auth is a lightweight local gate, not multi-user or production-grade authentication. DNS destinations are revalidated before each request/redirect; deployments requiring protection against DNS rebinding should add resolver pinning at the network transport layer. Reports and settings pages are not implemented.

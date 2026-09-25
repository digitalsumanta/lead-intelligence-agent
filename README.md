# Lead Intelligence Agent

Local first platform to import business CSVs, validate and deduplicate records, inspect public websites, generate structured sales intelligence with a local model, and explain lead scores. Backend: FastAPI, Pydantic 2, SQLAlchemy 2 async, PostgreSQL and Alembic. Frontend: Next.js, TypeScript and Tailwind.

## Architecture

FastAPI routes live in `main.py`; SQLAlchemy entities live in `models`, payload contracts in `schemas`, CSV, website, scoring and job orchestration in `services`, and model integrations behind `providers.AIProvider`. PostgreSQL stores leads, import jobs, batches, processing jobs, events, provider usage, keys and agent runs. The Docker worker polls due retries and reclaims stale processing jobs. Jobs persist checkpoints and use retry delays of 5, 15 and 60 seconds. Website and AI analysis are skipped when resuming beyond their checkpoints. Scoring uses transparent weighted observations, never just the model result.

## Requirements and Windows setup

Install Python 3.12+, Node 20+, Docker Desktop (or PostgreSQL 16), and Ollama. In PowerShell:

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
cd frontend
npm.cmd install
cd ..
```

The examples use `npm.cmd` so they work when PowerShell's script execution policy blocks `npm.ps1`.

## PostgreSQL

For a complete stack, set a non-default `POSTGRES_PASSWORD` in `.env`, then run `docker compose up --build`. Compose waits for Postgres, applies Alembic migrations, runs the retry worker, then serves API at `http://localhost:8000` and dashboard at `http://localhost:3000`.

For a separately managed PostgreSQL installation, create an application role and database (substitute your own password):

```powershell
psql -U postgres -c "CREATE ROLE lead_agent LOGIN PASSWORD 'change-this-password';"
psql -U postgres -c "CREATE DATABASE lead_agent OWNER lead_agent;"
```

Set `DATABASE_URL=postgresql+asyncpg://lead_agent:change-this-password@localhost:5432/lead_agent` in `.env`. Then run migrations from the repository root:

```powershell
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe -m alembic -c backend\alembic.ini upgrade head
```

## Running with Ollama locally

Ollama is optional for imports and website inspection, but required for AI processing. Install/start Ollama, then:

```powershell
ollama pull qwen2.5-coder:latest
ollama serve
```

Set `AI_PROVIDER=ollama`, `OLLAMA_MODEL=qwen2.5-coder:latest`, and `OLLAMA_BASE_URL=http://localhost:11434`. In Docker Desktop, the compose file uses `host.docker.internal` as the host Ollama address. OpenAI, Gemini, Google Places and PageSpeed keys can all remain blank when using Ollama. Multiple future cloud credentials can be configured as comma-separated `OPENAI_API_KEYS` or `GEMINI_API_KEYS`; they are registered in the database key pool and never returned in API responses. Cloud provider classes are extension points and currently return a clear not-implemented error if explicitly selected.

## Run backend/frontend

With Postgres running, start API from the repository root:

```powershell
$env:PYTHONPATH = "backend"
python -m uvicorn app.main:app --reload --app-dir backend --port 8000
```

In a second terminal: `cd frontend; npm.cmd run dev`. Or use `./scripts/start-dev.ps1` to start both (Docker mode: `./scripts/start-dev.ps1 -Docker`).

## CSV import and processing

CSV requires `company_name`; other supported headers are website, phone, email, address, city, state, country, industry. UTF-8 and BOM are supported. Rows are isolated, invalid values reported, duplicates recognized by normalized company and city. Upload is capped at 5 MB by default.

```powershell
./scripts/import-csv.ps1
./scripts/process-leads.ps1 -BatchSize 10 -Concurrency 2
```

Or:

```powershell
curl.exe -X POST http://localhost:8000/api/leads/import -F "file=@data/sample_leads.csv"
curl.exe -X POST http://localhost:8000/api/leads/process-batch -H "Content-Type: application/json" -d '{"batch_size":10,"max_concurrency":2}'
curl.exe -X POST http://localhost:8000/api/leads/{lead_id}/process
```

## API

- `GET /health`
- `GET /api/leads?page=1&page_size=25`
- `GET /api/leads/{id}`
- `POST /api/leads/import` (multipart `file`)
- `POST /api/leads/{id}/process`
- `POST /api/leads/process-batch` (`batch_size`, `max_concurrency`, optional provider/model)
- `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/retry`
- `GET /api/dashboard/stats`, `GET /api/providers/status`

Import response example: `{"job_id":"…","total_rows":10,"accepted_rows":10,"duplicate_rows":0,"invalid_rows":0,"invalid_details":[]}`. Health: `{"success":true,"status":"healthy","service":"Lead Intelligence Agent"}`. Errors use `{ "success": false, "error": { "code": "…", "message": "…", "details": {} } }` for unexpected errors.

## Tests

`python -m pip install -r backend/requirements.txt` then `python -m pytest backend/tests -v`. Tests use in-memory SQLite, TestClient, and mocked website/provider calls; they need no external services.

## Troubleshooting

- `connection refused` for Postgres: check `docker compose ps` or the local service and verify `DATABASE_URL`.
- Ollama status is unhealthy: start `ollama serve`, check the configured URL and model with `ollama list`.
- From Docker, use `host.docker.internal` for host Ollama, not `localhost`.
- CSV returns 422: check exact supported header names and UTF-8 encoding.
- CORS error: add the browser origin to comma-separated `CORS_ORIGINS`.
- First model generation may take a few minutes while Ollama loads or evaluates a local model; the default `REQUEST_TIMEOUT_SECONDS` is 300 seconds. Increase it for slower hardware or larger models.

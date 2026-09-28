# Lead Intelligence Agent

Local-first lead ingestion and enrichment. The backend uses FastAPI, SQLAlchemy 2 async, PostgreSQL, Alembic, and a separate durable-job worker. The frontend is Next.js. Ollama is the default AI provider; optional Gemini and generic OpenAI-compatible adapters use the shared provider execution/key-pool service. Google Places and PageSpeed collect source evidence through resumable enrichment stages.

## Architecture

- `backend/app/main.py` exposes REST endpoints. It validates requests, authenticates `/api/*`, and queues work; it does not run long lead analyses inline.
- `backend/app/models/entities.py` defines leads, import records, batches, processing jobs, events, provider usage, keys, and agent runs.
- `backend/app/services/` contains CSV validation, job state transitions, website inspection, scoring, and key-pool utilities.
- `backend/app/services/enrichment_pipeline.py` collects website, Google Places, and PageSpeed evidence in durable resumable stages. `backend/app/services/intelligence.py` deterministically derives opportunities, ranked service recommendations, and versioned scores from persisted evidence.
- `backend/app/workers/runner.py` polls durable jobs. PostgreSQL workers claim rows with `FOR UPDATE SKIP LOCKED`; a conditional unique index prevents more than one active job per lead. A separate heartbeat task supports worker readiness checks while a long AI request is running.
- `backend/migrations/` contains explicit Alembic schema revisions. Alembic is the sole schema authority. API and worker startup require an already migrated database and never create tables.
- The dashboard queues work and polls job status; it does not process leads itself.
- The Lead Intelligence review dashboard is available at `/leads`, with detail pages at `/leads/{lead_id}`. It uses `NEXT_PUBLIC_API_URL` (default `http://localhost:8000`) and the existing `NEXT_PUBLIC_API_TOKEN` browser bearer-token setting. Keep this token for trusted local development only because `NEXT_PUBLIC_*` values are bundled into browser code. Run the frontend with `cd frontend; npm run dev`; its list filters and pagination use the existing API. Search, sorting, and total result counts are not currently exposed by the backend and are therefore not simulated in the browser.

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

`/health` is unauthenticated process liveness. `/ready` checks database schema, configured provider health, and whether a worker heartbeat is recent (30 second window). All `/api/*` routes require `Authorization: Bearer <API_AUTH_TOKEN>`. Endpoints include lead listing/details/import, single and batch job creation, job listing/details/retry, dashboard statistics, and provider status. `GET /api/leads/{lead_id}/intelligence` returns the versioned `lead-intelligence-v1` projection of the newest completed enrichment run with a persisted score and valid run-owned deterministic intelligence. It includes run metadata, score breakdown, deterministic opportunities and recommendations, run-scoped evidence, validated personalization and claims when available, and processing metadata. GET is read-only and never invokes an AI provider. A missing lead returns `LEAD_NOT_FOUND` (404); a lead without a valid completed intelligence run returns `INTELLIGENCE_NOT_AVAILABLE` (409). Invalid UUIDs and query parameters use the standard validation error envelope (422); unexpected errors return a generic `INTERNAL_ERROR` (500).

`GET /api/leads` keeps its existing array response and page-based pagination (`page` starts at 1; `page_size` defaults to 25 and is capped at 100). Results are ordered by `created_at` descending then ID descending for stable pages. Lead rows now include compact intelligence status, score, opportunity/recommendation counts, personalization availability, and latest enrichment time, without evidence or personalization bodies. Optional filters are `min_score`/`max_score` (0–100), `intelligence_status` (`available` or `unavailable`), `personalization_available`, and `opportunity_present`. `/api/leads/{lead_id}` retains its existing fields and adds the same compact summary. Neither summary nor detail responses combine data across enrichment runs.

CSV requires `company_name`; optional columns are website, phone, email, address, city, state, country, and industry. Upload size defaults to 5 MB.

The website analyzer only accepts HTTP(S), rejects localhost and non-public addresses, checks DNS destinations, manually follows a bounded number of redirects while revalidating them, applies a timeout, and caps response size. It does not crawl subpages.

## AI providers

`AI_DEFAULT_PROVIDER` selects `ollama` (default), `gemini`, or `openai_compatible`. Ollama needs no subscription or API key; configure `OLLAMA_BASE_URL` and `OLLAMA_MODEL`. Gemini and OpenAI-compatible providers are disabled by default. Enable the chosen provider with `AI_GEMINI_ENABLED=true` or `AI_OPENAI_COMPATIBLE_ENABLED=true`, then configure its base URL, model, and timeout (`AI_GEMINI_*` or `AI_OPENAI_COMPATIBLE_*`). Gemini uses Google's `generateContent` HTTP API; OpenAI-compatible endpoints use `POST /v1/chat/completions`. The Gemini base URL defaults to Google's v1beta API; the compatible endpoint defaults to a local LM Studio style server.

Credentials are supplied as `AI_GEMINI_API_KEY` / `AI_GEMINI_API_KEYS` or `AI_OPENAI_COMPATIBLE_API_KEY` / `AI_OPENAI_COMPATIBLE_API_KEYS`; multiple keys are comma-separated and leased through the existing provider key pool. Calls share its retry, cooldown, rate-limit, and usage telemetry behavior. Cloud credentials are only required if that cloud provider is enabled and selected. Provider tests use mocked HTTP and do not call cloud services. The credential-at-rest limitation described below still applies.

Structured output accepts Pydantic models or the locally enforced JSON Schema subset: local `$defs`/`definitions` references, primitive types, object/array constraints, enums/constants, string patterns, formats (`date-time`, `date`, `time`, `email`, `hostname`, `ipv4`, `ipv6`, `uri`, `uuid`), numeric bounds, nullable schemas, and `anyOf`/`oneOf`/`allOf`. Unsupported keywords, formats, dialects, or external references return an unsupported-capability error before a provider request. Gemini further rejects schema unions it cannot map to its API schema.

`OPENAI_API_KEY` and `OPENAI_API_KEYS` are legacy key-pool settings and are not used by the `openai_compatible` adapter; configure the `AI_OPENAI_COMPATIBLE_*` variables instead. `ProviderUsage` contains operation-scoped request-attempt rows from the execution service and operation-null job summary rows; count request traffic using rows with an operation set.

Google Places collection is an optional enrichment adapter and is disabled by default. Set `GOOGLE_PLACES_ENABLED=true` and configure `GOOGLE_PLACES_API_KEY`; the application registers the key with the provider key pool. The Google Cloud project must enable Places API (New) and have billing configured. The adapter uses Text Search (New) with a restricted field mask and mocked HTTP in normal tests; live Google access is optional. See Google's [Text Search (New) documentation](https://developers.google.com/maps/documentation/places/web-service/text-search) and [usage and billing documentation](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing).

PageSpeed Insights collection is optional and disabled by default. Set `PAGESPEED_ENABLED=true` to opt in; `PAGESPEED_API_KEY` is recommended for automated collection and is leased through the provider key pool when supplied. Configure `PAGESPEED_STRATEGY` as `mobile` or `desktop`, along with timeout, operation, and categories as needed. The adapter validates public HTTP(S) website URLs before requesting Google analysis, and normal tests use mocked responses. Each strategy run is an external Lighthouse analysis request and consumes the API project's quota; see the [PageSpeed Insights API method](https://developers.google.com/speed/docs/insights/rest/v5/pagespeedapi/runpagespeed) for current request parameters.

## Deterministic intelligence

Opportunities and service recommendations are derived from persisted WebsiteAnalyzer, Google Places, and PageSpeed evidence. Current rule thresholds are versioned in `backend/app/services/intelligence.py`; unsupported signals do not create opportunities, and unknown lead fit or buying intent remains unavailable. The service catalog is persisted and can be configured by activating, deactivating, or revising catalog entries. `leads.lead_score` remains the current denormalized score for API compatibility; its meaning is now an evidence-backed digital opportunity score. `leads.score_factors` includes the score version and evidence-linked components, while historical versions are stored per enrichment run. Existing AI-generated copy fields remain separate and do not drive deterministic scoring or recommendations.

## Evidence-grounded personalization

AI personalization runs against only the current enrichment run's persisted evidence, deterministic opportunities, ranked service recommendations, and current versioned score. The versioned contracts are `personalization-v2` and `evidence-grounded-prompt-v2`. Claims are categorized as `verified_fact`, `supported_inference`, or `suggested_language`; verified facts must exactly match a canonical rendering of one cited persisted observation, and supported inferences must exactly match a cautious rendering of one cited deterministic opportunity and its evidence. Suggested language is restricted to approved non-factual phrases. Opportunity and evidence IDs are checked against lead/run ownership before use. Package claims carry service IDs, and their ordered set must exactly equal the deterministic recommendation set. This conservative contract trades free-form factual copy for validation that can be enforced without semantic model judging.

Externally collected evidence is passed as untrusted data. The system prompt directs providers not to follow instructions embedded in website or business content, and no tools are exposed to personalization generation. Validated personalization is persisted per enrichment run with contract/prompt versions and normalized evidence links in `personalizations` and `personalization_evidence_links`; a composite foreign key prevents pairing a lead with another lead's run. Existing lead/API response fields remain present and project the validated summary/script/opening/package plus deterministic services/opportunities. This changes `website_problems`, `seo_opportunities`, `digital_marketing_opportunities`, and `pain_points` to deterministic opportunity projections; `recommended_services` mirrors deterministic recommendations. Legacy AI-only keyword and competitor suggestions are empty, and `decision_maker` is cleared because the new flow does not regenerate it. Ollama, Gemini, and OpenAI-compatible adapters use the shared provider registry, structured request contract, execution controls, and telemetry.

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

Provider adapters currently support Ollama, Gemini, and generic OpenAI-compatible chat completions. Provider request controls support API-key leases and typed retries, but `api_keys.key` is still stored as plaintext in PostgreSQL; use only trusted credentials until encrypted storage or a secret-manager reference is implemented. API bearer-token auth is a lightweight local gate, not multi-user or production-grade authentication. DNS destinations are revalidated before website requests and redirects; deployments requiring protection against DNS rebinding should add resolver pinning at the network transport layer. Reports and settings pages are not implemented.

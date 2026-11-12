# Phase 12 Final Reliability & Failure-Mode Audit: Remediation Backlog
## Lead Intelligence Agent — Prioritized Engineering Action Plan

**Document Identifier:** `PHASE_12_REMEDIATION_BACKLOG.md`  
**Audit Scope:** Prioritized Remediation Tasks, Work Packages, and Acceptance Criteria  
**Target Repository:** `G:\AI-Projects\lead-intelligence-agent`  
**Execution Date:** November 4, 2026  
**Auditor Roles:** Software Architect, Senior QA Engineer, Site Reliability Engineer  
**Safety Invariant:** `OUTREACH_DISPATCH_ENABLED=false` Strictly Enforced  

---

## 1. Remediation Backlog Overview

This backlog outlines the prioritized engineering work packages required to resolve the defects, test regressions, and operational risks identified during the Phase 12 Final Reliability & Failure-Mode Audit.

```mermaid
flowchart LR
    P0["P0: Critical Blocker (1 Task)<br>Webhook Auth Exemption"] --> P1["P1: High Priority (4 Tasks)<br>Mock Isolation & Test Regressions"]
    P1 --> P2["P2: Medium Priority (3 Tasks)<br>Ingress Rate Limits & Sync SRE"]
    P2 --> P3["P3: Low Priority (3 Tasks)<br>Postgres CI & Dependency Locks"]
```

---

## 2. Priority 0 (P0) — Critical Operational Blockers

### REM-01: Webhook Middleware Authentication Exemption & Signature Verification
- **Task ID:** `REM-01`
- **Priority:** **P0** (Must fix prior to any production email dispatch)
- **Component:** `backend/app/main.py::local_api_auth` & `backend/app/api/routes/supervisor_delivery_webhooks.py`
- **Related Defects:** `SEC-01`, `FM-05`
- **Problem Statement:** The global `local_api_auth` middleware checks for an `Authorization: Bearer <token>` header across all `/api/*` paths. Third-party webhook providers (SendGrid, Resend) authenticate via HTTP headers carrying cryptographic signatures (`svix-signature`, `x-twilio-email-event-webhook-signature`), not bearer tokens. Consequently, incoming delivery telemetry is rejected with HTTP 401 Unauthorized.
- **Proposed Technical Solution:**
  1. Update `local_api_auth` middleware to exempt paths matching `/api/sales-supervisor/webhooks/*` from Bearer token checking.
  2. Implement an explicit signature verification dependency inside `supervisor_delivery_webhooks.py` that verifies the HMAC/Svix signature using `settings.outreach_resend_webhook_secret` or `settings.outreach_sendgrid_webhook_public_key`.
  3. Ensure unauthenticated requests lacking a valid signature are rejected with HTTP 403 Forbidden.
- **Acceptance Criteria:**
  - [ ] Requests to `/api/sales-supervisor/webhooks/delivery/*` without Bearer tokens bypass `local_api_auth`.
  - [ ] Requests with invalid or missing provider signatures return HTTP 403.
  - [ ] Valid SendGrid and Resend webhook payloads are ingested successfully into `email_delivery_events`.
  - [ ] All other `/api/*` routes continue to require valid Bearer token authentication.

---

## 3. Priority 1 (P1) — High Priority & Core Test Regressions

### REM-02: Isolate `BusinessResearchEngine` from Live Network Calls in Unit Tests
- **Task ID:** `REM-02`
- **Priority:** **P1** (Must fix before pilot launch)
- **Component:** `backend/app/services/business_research/engine.py` & `backend/tests/test_business_research.py`
- **Related Defects:** `REL-01`, `FM-02`
- **Problem Statement:** `BusinessResearchEngine._research_business` initiates real socket/DNS resolution (`httpx.get`) against candidate domains discovered by the provider. In unit tests, candidate domains are mock entities (`apexsolutionskolkata.com`), causing `[Errno 11001] getaddrinfo failed` and dropping candidates from `job.results`, failing test assertion `assert len(job.results) == 4`.
- **Proposed Technical Solution:**
  1. Refactor `BusinessResearchEngine` to accept an optional `website_analyzer` dependency in its constructor (defaulting to the production `WebsiteAnalyzerService`).
  2. In `test_business_research.py`, pass an isolated mock analyzer that returns pre-determined website analysis dictionaries without attempting socket connections.
  3. In production logic, differentiate between a non-existent domain and transient DNS network failures, recording an explicit `DOMAIN_UNRESOLVABLE` status rather than silently dropping candidate leads.
- **Acceptance Criteria:**
  - [ ] `pytest backend/tests/test_business_research.py` passes 8/8 tests without internet access.
  - [ ] `assert len(job.results) == 4` succeeds in `test_business_discovery_and_research_workflow`.
  - [ ] Zero `[Errno 11001] getaddrinfo failed` warnings are logged during test runs.

---

### REM-03: Modernize or Retire Legacy `test_digital_marketing_intelligence.py`
- **Task ID:** `REM-03`
- **Priority:** **P1** (Must fix to unblock global CI test collection)
- **Component:** `backend/tests/test_digital_marketing_intelligence.py`
- **Related Defects:** `MAINT-01`, `FM-01`
- **Problem Statement:** Standard `pytest` collection fails with `ImportError: cannot import name 'normalize_signal_source' from 'app.domain.digital_marketing_intelligence'` because this Phase 10 test file was not updated when Phase 12 introduced the 7-canonical-category domain engine.
- **Proposed Technical Solution:**
  1. Evaluate test coverage in `backend/tests/test_seven_category_digital_marketing.py`.
  2. Either update `test_digital_marketing_intelligence.py` to test the new canonical functions (`normalize_marketing_signal`, `calculate_canonical_completeness`) or safely delete/archive the legacy file if its test assertions are fully subsumed by `test_seven_category_digital_marketing.py`.
- **Acceptance Criteria:**
  - [ ] `pytest --collect-only` executes across the entire repository with **0 collection errors**.
  - [ ] All digital marketing intelligence tests pass with 100% compliance.

---

### REM-04: Align `test_services.py` with Modernized Google Places Enrichment Stage
- **Task ID:** `REM-04`
- **Priority:** **P1** (Must fix to restore full service test pass rate)
- **Component:** `backend/tests/test_services.py` & `backend/app/services/jobs.py`
- **Related Defects:** `REL-02`, `FM-03`
- **Problem Statement:** Four tests in `test_services.py` fail because `run_job` invokes `run_enrichment_pipeline`, which now marks the Google Places stage as mandatory. Without a mocked Google Places provider in test fixtures, the stage raises `EnrichmentStageRetryable`, leaving the job in `JobStatus.RETRYING` instead of `JobStatus.COMPLETED`.
- **Proposed Technical Solution:**
  1. Update `test_services.py` test harness to inject a mock `google_places` provider into the enrichment pipeline runner.
  2. Verify that mock leads progress cleanly to `JobStatus.COMPLETED`.
- **Acceptance Criteria:**
  - [ ] `pytest backend/tests/test_services.py` passes 32/32 tests.
  - [ ] Jobs transition cleanly to `JobStatus.COMPLETED` when mock providers are supplied.

---

### REM-05: Formalize Outreach Dispatch Verification in Canary Deployment Gates
- **Task ID:** `REM-05`
- **Priority:** **P1**
- **Component:** `backend/app/services/supervisor_dispatch_service.py` & CI Deployment Pipeline
- **Related Defects:** `FM-09`, `FM-12`
- **Problem Statement:** When transitioning to production, activating `OUTREACH_DISPATCH_ENABLED=true` must be strictly gate-checked to prevent accidental blast outreach.
- **Proposed Technical Solution:**
  1. Ensure deployment pipelines enforce canary flight authorizations (`CanaryAuthorizationService`).
  2. Implement an automated pre-flight assertion script that checks recipient allowlists before activating live credentials.
- **Acceptance Criteria:**
  - [ ] Canary mode rejects any non-allowlisted email recipient even if dispatch is globally enabled.
  - [ ] Maximum flight caps (`max_messages`) are strictly enforced.

---

## 4. Priority 2 (P2) — Medium Priority & Operational Hardening

### REM-06: Implement Application-Layer Rate Limiting on Ingestion Endpoints
- **Task ID:** `REM-06`
- **Priority:** **P2**
- **Component:** `backend/app/main.py` & REST Ingestion Endpoints
- **Related Defects:** `SEC-02`, `FM-07`
- **Problem Statement:** While authenticated, endpoints `/api/leads`, `/api/leads/import`, and `/api/jobs/process-batch` lack client-level rate limits. A runaway client script could flood the backend with thousands of duplicate leads.
- **Proposed Technical Solution:**
  1. Add `slowapi` or Redis-backed leaky bucket rate limiting middleware to `/api/leads/import` (e.g. 10 requests per minute per authenticated client ID) and `/api/leads` (e.g. 120 requests per minute).
  2. Return standard HTTP 429 Too Many Requests with `Retry-After` header.
- **Acceptance Criteria:**
  - [ ] Exceeding rate threshold returns HTTP 429 JSON envelope.
  - [ ] Standard authenticated workflows are unaffected.

---

### REM-07: Harden Transactional Sync Boundaries in `unified_intelligence.py`
- **Task ID:** `REM-07`
- **Priority:** **P2**
- **Component:** `backend/app/services/unified_intelligence.py`
- **Related Defects:** `FM-08`
- **Problem Statement:** Synchronizing digital marketing gaps, buying signals, and opportunities involves writes to multiple database entities.
- **Proposed Technical Solution:**
  1. Ensure all multi-entity synchronization steps execute within an explicit `session.begin_nested()` transaction savepoint.
  2. Add structured rollback logging to record the specific opportunity title that caused any sync failure.
- **Acceptance Criteria:**
  - [ ] Partial failures trigger clean transactional rollback with zero orphaned opportunities.

---

### REM-08: Dedicated Background Worker Pool for Heavy Website Crawling
- **Task ID:** `REM-08`
- **Priority:** **P2**
- **Component:** `backend/app/services/website.py` & Worker Architecture
- **Related Defects:** `FM-06`
- **Problem Statement:** Slow third-party lead websites can occupy shared async worker threads during batch crawls.
- **Proposed Technical Solution:**
  1. Isolate website HTTP crawling into a dedicated bounded thread pool or asynchronous semaphore (e.g., `asyncio.Semaphore(10)`).
  2. Ensure lead database operations and API request handling remain unblocked during high-latency network crawls.
- **Acceptance Criteria:**
  - [ ] API responsiveness on `/api/leads` remains under 50ms while 20 concurrent website crawls are active.

---

## 5. Priority 3 (P3) — Low Priority & Technical Debt

### REM-09: Provision Automated Containerized PostgreSQL Service in CI Pipeline
- **Task ID:** `REM-09`
- **Priority:** **P3**
- **Component:** CI/CD Pipeline & `backend/tests/test_postgres_integration.py`
- **Related Defects:** `FM-04`
- **Problem Statement:** 7 PostgreSQL integration tests are deselected by default because development environments rely on SQLite.
- **Proposed Technical Solution:**
  1. Configure GitHub Actions / GitLab CI with a `services: postgres:16-alpine` service container.
  2. Export `TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/test_db` in the CI job and run `pytest -m postgres_integration`.
- **Acceptance Criteria:**
  - [ ] All 7 PostgreSQL integration tests pass cleanly in automated CI runs.

---

### REM-10: Pre-Compute Qualification Scores During Batch Enrichment
- **Task ID:** `REM-10`
- **Priority:** **P3**
- **Component:** `backend/app/services/lead_qualification.py` & Batch Pipeline
- **Related Defects:** `FM-11`
- **Problem Statement:** Cold retrieval of un-cached lead qualification scores computes all 7 dimensions on the fly, adding 200–400ms latency to the initial lead detail page view.
- **Proposed Technical Solution:**
  1. Trigger `qualify_lead` at the conclusion of the unified intelligence pipeline.
  2. Ensure the lead record is committed with pre-computed qualification data already populated in `lead.score_factors["qualification"]`.
- **Acceptance Criteria:**
  - [ ] `GET /api/leads/{id}/qualification` responds from pre-computed cache in under 15ms.

---

### REM-11: Harden Unpinned Transitive Dependencies in Build Manifest
- **Task ID:** `REM-11`
- **Priority:** **P3**
- **Component:** `backend/requirements.txt` & Docker Build
- **Related Defects:** `SEC-04`
- **Problem Statement:** Several secondary packages in `requirements.txt` lack pinned patch versions, risking build drift over time.
- **Proposed Technical Solution:**
  1. Generate fully pinned `requirements.lock` using `pip-compile` or `poetry.lock`.
  2. Reference the locked file in production Dockerfiles.
- **Acceptance Criteria:**
  - [ ] Production Docker builds are 100% reproducible and immune to upstream patch version drift.

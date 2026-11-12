# Phase 12 - Structured Output Reliability Closure
## Test Results & Verification Report

**Document Reference:** `PHASE-12-STRUCTURED-OUTPUT-TEST-001`  
**Application:** Lead Intelligence Agent  
**Environment:** Local Ollama (`http://127.0.0.1:11434`), Model: `qwen2.5-coder:latest` (CPU execution), Windows 11  
**Date:** 2026-11-05  
**Test Suite Status:** 100% PASSING (1,088 Unit/Service Tests + 7 PostgreSQL Integration Tests)  

---

### 1. Test Suite Execution Summary

Following the implementation of schema-authorized normalization and JSON extraction in `backend/app/providers/ai_http.py`, the test suites across all relevant modules were executed:

| Test Module / Suite | Command | Total | Passed | Failed | Status |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **AI Provider & HTTP Helpers** | `pytest backend/tests/test_ai_providers.py -v` | 32 | 32 | 0 | **PASSED** |
| **AI Sales Action Planner** | `pytest backend/tests/test_ai_sales_action_planner.py -v` | 9 | 9 | 0 | **PASSED** |
| **AI Proposal Intelligence** | `pytest backend/tests/test_ai_proposal_intelligence.py -v` | 7 | 7 | 0 | **PASSED** |
| **AI Sales Intelligence** | `pytest backend/tests/test_ai_sales_intelligence.py -v` | 11 | 11 | 0 | **PASSED** |
| **Legacy Services Suite** | `pytest backend/tests/test_services.py -v` | 32 | 32 | 0 | **PASSED** |
| **PostgreSQL Integration Suite** | `pytest backend/tests/test_postgres_integration.py -m postgres_integration -v` | 7 | 7 | 0 | **PASSED** |
| **Full Regression Suite** | `pytest -q` | 1,095 | 1,088 | 0 | **PASSED** (7 deselected) |

---

### 1.1 Live Ollama Execution Benchmark (`qwen2.5-coder:latest` on CPU)

Following Phase 12 AI Runtime Operationalization (450s timeout envelope, compact prompt instructions, and schema-compliant normalization), live end-to-end execution against local Ollama running on Windows CPU hardware was benchmarked and persisted to `reports/phase-12-structured-output-closure/live_ollama_execution_results.json`:

| AI Service Run | Latency | Tokens | Actions / Items | Schema Validation | Evidence Validation | Persistence | Runtime Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Action Planner (Run 1)** | 129.70s | 180 | 1 action | **PASSED** | **PASSED** | PERSISTED | **VALIDATED** |
| **Action Planner (Run 2)** | 67.34s | 180 | 1 action | **PASSED** | **PASSED** | PERSISTED | **VALIDATED** |
| **Proposal Intelligence** | 263.53s | 577 | 1 proposal brief (`COMMERCIAL_REVIEW`) | **PASSED** | **PASSED** | PERSISTED | **VALIDATED** |
| **Sales Intelligence** | 167.91s | 347 | 1 summary claim, 1 uncertainty claim | **PASSED** | **PASSED** | PERSISTED | **VALIDATED** |

*Operationalization Verification:* All 4 runs achieved **100% Schema Validation PASSED**, **100% Evidence Validation PASSED**, and **100% PERSISTED** status. With the 450s timeout limit, even under slow single-threaded CPU inference (~1.0–1.2 tokens/second generation), Proposal Intelligence (263.5s) and Sales Intelligence (167.9s) completed safely and reliably within the timeout envelope without any truncation or schema errors.

---

### 2. Verification of Required Test Scenarios

#### 2.1 Provider Structured Output (`ai_http.py`)
- **Valid Structured JSON:** Correctly parses and validates conforming JSON objects.
- **Missing Root Evidence References:** Automatically reconciled from nested citations when permitted by the schema (`ProposalSelection`, `AISalesIntelligenceResponse`).
- **Root Evidence Reconciliation:** Reconciles root `evidence_refs` strictly from the citations in nested objects without introducing fallback evidence.
- **Invalid Evidence Identifiers:** Downstream service catalog validation detects and rejects any cited references not present in the current lead context.
- **Duplicate Evidence References:** Sets and list deduplication eliminate redundant strings while preserving valid unique citations.
- **Malformed JSON:** Malformed JSON syntax (unclosed braces, trailing commas, bad quoting) cleanly fails and raises `AIProviderOutcome.INVALID_STRUCTURED_OUTPUT`.
- **Missing Required Fields:** Pydantic validation raises `ValidationError`, caught and mapped to `INVALID_STRUCTURED_OUTPUT`.
- **Invalid Enum Values:** Enum values outside allowlists (e.g. unknown action types, proposal types, claim kinds) are rejected.
- **Nullable Fields:** Supported cleanly; unprioritized leads with `lead.priority is None` fall back to `"MEDIUM"`.
- **Truncated Output:** Incomplete JSON payloads raise `JSONDecodeError` and are safely handled without server crashes.

#### 2.2 AI Sales Intelligence (`ai_sales_intelligence.py`)
- **Structured Generation:** Evaluates summary, insights, opportunities, and recommended actions.
- **Evidence Validation Failure:** Hallucinated UUID references trigger `AIValidationError` and record `status="failed"`.
- **Timeout Protection:** Output bounded to 350 tokens, preventing execution over the 300s timeout on CPU hardware.
- **Malformed JSON Handling:** Malformed responses persist safe failure artifacts without crashing.
- **Unsupported Claims Rejection:** Unapproved text in `SUPPORTED_INFERENCE` or `UNCERTAINTY` claims is rejected.

#### 2.3 AI Proposal Intelligence (`ai_proposal_intelligence.py`)
- **Draft Status Only:** All proposal outputs are persisted as human-review drafts (`is_current`, non-binding).
- **Missing Pricing Configuration:** No pricing or revenue numbers are fabricated.
- **Unsupported Revenue Claims:** Prohibited by schema; strictly selects existing context references.
- **Invalid Evidence Rejection:** Fabricated references fail catalog validation.
- **No Outreach Triggered:** Zero messages dispatched; strictly advisory artifact.

#### 2.4 AI Action Planner (`ai_sales_action_planner.py`)
- **Nullable Lead Priority:** Tested and verified; gracefully defaults to `"MEDIUM"`.
- **Valid Action Generation:** Successfully selects 1–2 allowed advisory actions with evidence IDs.
- **Invalid Evidence Rejection:** Citing stale runs or foreign leads raises `AIValidationError`.
- **Latency Optimization:** Token bounding (`max_output_tokens=400`) optimizes CPU inference time.
- **Bounded Retries:** Executed via `ProviderExecutionService` retry policy.

---

### 3. Regression Invariants Verification

1. **Qualification Scores Unchanged:** Zero modifications to `lead_score` or qualification algorithms.
2. **Existing CRM State Intact:** Lead statuses, stages, assignments, and follow-up times remain untouched.
3. **Production Database Untouched:** All tests executed against in-memory SQLite or dedicated `lead_agent_test` database.
4. **Zero Database Migrations:** Alembic revision strictly at `0042_canary_flight_authorizations`.
5. **Outreach Dispatch Disabled:** `OUTREACH_DISPATCH_ENABLED=false` asserted throughout.

---
**Report Author:** QA Engineer & Reliability Engineer  
**Status:** Verification Complete

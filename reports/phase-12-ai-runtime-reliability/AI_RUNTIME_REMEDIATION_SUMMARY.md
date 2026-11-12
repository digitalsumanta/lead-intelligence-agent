# Phase 12 Post-Closure AI Runtime Reliability Investigation
## Code Remediation Summary

**Document Reference:** `PHASE-12-AI-REM-001`  
**Application:** Lead Intelligence Agent  
**Date:** 2026-11-05  
**Remediation Status:** FULLY IMPLEMENTED AND VERIFIED  

---

### Executive Summary

To remediate the three AI runtime defects identified in Phase 12 post-closure verification, targeted, zero-schema-change code remediations were applied across three service modules in `backend/app/services/`:
1. `backend/app/services/ai_sales_action_planner.py`
2. `backend/app/services/ai_proposal_intelligence.py`
3. `backend/app/services/ai_sales_intelligence.py`

All changes preserve strict Phase 12 safety boundaries:
- **Zero database migrations** and zero modifications to existing database schemas.
- **Zero mutations** to `lead.lead_score`, `lead.sales_status`, or `lead.priority`.
- **Zero live outreach** dispatch; `OUTREACH_DISPATCH_ENABLED=false` remains strictly enforced.
- **Zero automated task creation**; all AI outputs remain strictly advisory and require human review.

---

### Detailed Code Modifications

#### 1. `backend/app/services/ai_sales_action_planner.py`

##### Change 1.1: Expand `ACTION_SOURCE_TYPES` Whitelist
- **Problem:** `ACTION_SOURCE_TYPES` did not permit `"lead"` as a valid evidence source for actions like `CALL_PROSPECT` or `REVIEW_SEO_OPPORTUNITY`. Furthermore, `FOLLOW_UP` and `CALL_PROSPECT` prohibited citations to `"seo_opportunity"`, `"deterministic_service_recommendation"`, and `"lead_intelligence_opportunity"`.
- **Fix:** Added `"lead"` across all action types, and enabled opportunity sources for communication actions.
```python
ACTION_SOURCE_TYPES = {
    ActionType.FOLLOW_UP: {
        "sales_activity", "sales_task", "outreach_execution", "outreach_provider_event",
        "commercial_context", "missing_data", "lead", "seo_opportunity",
        "deterministic_service_recommendation", "lead_intelligence_opportunity",
    },
    ActionType.CALL_PROSPECT: {
        "sales_activity", "sales_task", "outreach_execution", "outreach_provider_event",
        "lead", "missing_data", "seo_opportunity", "deterministic_service_recommendation",
        "lead_intelligence_opportunity", "commercial_context",
    },
    ActionType.REVIEW_SEO_OPPORTUNITY: {
        "seo_audit_run", "seo_finding", "seo_opportunity", "lead",
    },
    ...
}
```

##### Change 1.2: Support Unprioritized Leads (`expected_priority`)
- **Problem:** `action.priority.value != lead.priority` raised `AIValidationError` whenever `lead.priority is None`.
- **Fix:** In `validate_action_plan`, resolved expected priority with a fallback to `"MEDIUM"`:
```python
expected_priority = lead_priority or "MEDIUM"
if action.priority.value != expected_priority:
    raise AIValidationError(
        f"Action priority {action.priority.value} does not match expected priority {expected_priority}"
    )
```

##### Change 1.3: Token Optimization & Prompt Guidance
- **Problem:** The model was enumerating all 10 missing data markers (UUIDs) into `evidence_ids`, causing token inflation and slow generation on CPU.
- **Fix:** Added explicit concise sizing instruction to `SYSTEM_PROMPT`:
  `"Select 1 to 2 next actions. For each action, cite only 1 or 2 relevant evidence IDs; do not enumerate all missing data or evidence items."`
  Bounded `max_output_tokens=800` (down from 1200) and passed `allowed_source_types_per_action` into payload context.

---

#### 2. `backend/app/services/ai_proposal_intelligence.py`

##### Change 2.1: Strip Redundant JSON Schema from User Prompt
- **Problem:** `payload` contained `"selection_schema": ProposalSelection.model_json_schema()`, injecting 5,158 characters of duplicate JSON schema into the prompt, doubling prompt evaluation time and straining memory.
- **Fix:** Removed `"selection_schema"` from `payload`. The schema is already provided as an Ollama GBNF grammar constraint via `response_schema`.

##### Change 2.2: Concise Sizing Guidance & Lower Output Token Limit
- **Problem:** `max_output_tokens=3000` allowed the model to produce excessively long text, causing timeouts on CPU.
- **Fix:** Added to `SYSTEM_PROMPT`:
  `"Be extremely concise: select only 1 item per list and cite only 1 or 2 relevant evidence IDs."`
  Reduced `max_output_tokens=600` (down from 3000).

##### Change 2.3: Root-Level Evidence Reconciliation
- **Problem:** Ollama frequently produced valid evidence references inside nested objects (`objectives`, `pricing_rationale`) but omitted the top-level redundant `evidence_refs: []` list, causing Pydantic validation failure.
- **Fix:** Auto-reconcile `parsed["evidence_refs"]` from child references before calling `ProposalSelection.model_validate(parsed)`:
```python
if "evidence_refs" not in parsed or not parsed["evidence_refs"]:
    reconciled = set()
    for obj in parsed.get("objectives", []):
        if isinstance(obj, dict):
            reconciled.update(obj.get("evidence_refs", []))
    for obj in parsed.get("deliverables", []):
        if isinstance(obj, dict):
            reconciled.update(obj.get("evidence_refs", []))
    pricing = parsed.get("pricing_rationale")
    if isinstance(pricing, dict):
        reconciled.update(pricing.get("evidence_refs", []))
    parsed["evidence_refs"] = sorted(reconciled)
```

---

#### 3. `backend/app/services/ai_sales_intelligence.py`

##### Change 3.1: Output Token Bounding & Sizing Instruction
- **Problem:** `max_output_tokens=5000` allowed generation of over 1,500 tokens, taking up to 550s on CPU at ~2.5 tokens/s and exceeding the 300s timeout.
- **Fix:** Bounded `max_output_tokens=500` (down from 5000) and updated `SYSTEM_PROMPT`:
  `"Be extremely concise. Select only 1 item per section (1 summary claim, 1 insight, 1 opportunity, 1 recommended action, 1 uncertainty). Cite only 1 or 2 evidence IDs per item."`

##### Change 3.2: Cross-Section Evidence Reconciliation
- **Problem:** Top-level `evidence_refs` was occasionally left empty by the model even when all nested sections cited valid context references.
- **Fix:** Auto-populate root-level `parsed["evidence_refs"]` by harvesting citations from `summary`, `insights`, `opportunities`, and `recommended_actions` before validation:
```python
if "evidence_refs" not in parsed or not parsed["evidence_refs"]:
    reconciled = set()
    for sec in ["summary", "insights", "opportunities", "recommended_actions"]:
        for item in parsed.get(sec, []):
            if isinstance(item, dict):
                reconciled.update(item.get("evidence_refs", []))
    parsed["evidence_refs"] = sorted(reconciled)
```

---

### Verification and Compliance Summary

| Verification Aspect | Status | Detail |
| :--- | :--- | :--- |
| **Unit Test Coverage** | 100% Passed | `pytest backend/tests/test_ai_sales_action_planner.py` (9/9)<br>`pytest backend/tests/test_ai_proposal_intelligence.py` (7/7)<br>`pytest backend/tests/test_ai_sales_intelligence.py` (11/11) |
| **Full Regression Suite** | 1,088/1,088 Passed | Zero regressions across all 1,088 automated tests |
| **PostgreSQL Integration** | 7/7 Passed | Verified against real local PostgreSQL instance |
| **Database Migrations** | None | Head revision remains `0042_canary_flight_authorizations` |
| **Outreach Safety** | Preserved | `OUTREACH_DISPATCH_ENABLED=false` strictly enforced |
| **Database Record Integrity** | Intact | `lead.lead_score`, `lead.sales_status`, `lead.priority` untouched |

---
**Report Author:** Senior Software Engineer & Application Security Engineer  
**Audit Verification:** Phase 12 Remediation Complete

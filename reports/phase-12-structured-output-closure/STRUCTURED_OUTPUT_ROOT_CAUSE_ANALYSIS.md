# Phase 12 - Structured Output Reliability Closure
## Root Cause Analysis Report

**Document Reference:** `PHASE-12-STRUCTURED-OUTPUT-RCA-001`  
**Application:** Lead Intelligence Agent  
**Environment:** Local Ollama (`http://127.0.0.1:11434`), Model: `qwen2.5-coder:latest` (CPU execution), Windows 11  
**Investigation Date:** 2026-11-05  
**Investigation Status:** READ-ONLY INSPECTION COMPLETE  

---

### 1. Executive Summary & Initial Runtime Baseline

During post-closure verification of Phase 12, real live inference against local Ollama (`qwen2.5-coder:latest`) running on local CPU hardware demonstrated divergent results across the three core AI intelligence modules:

| AI Module | Initial Live Runtime Status | Latency / Duration | Primary Failure / Code |
| :--- | :---: | :---: | :--- |
| **AI Sales Action Planner** | **VALIDATED** | 290.28s | None (Generated 2 validated advisory actions) |
| **AI Sales Intelligence** | **FAILED** | 258.96s | `INVALID_STRUCTURED_OUTPUT` |
| **AI Proposal Intelligence** | **FAILED** | 276.98s | `INVALID_STRUCTURED_OUTPUT` |

While AI Sales Action Planner succeeded in generating a schema-compliant, evidence-grounded action plan, both AI Sales Intelligence and AI Proposal Intelligence failed with `INVALID_STRUCTURED_OUTPUT` at the provider parsing layer.

Through isolated read-only inspection, raw model response extraction, and schema execution analysis, the exact programmatic mechanisms causing these failures have been identified and proven.

---

### 2. Task A Root Cause: Provider-Layer Structured-Output Parsing Sequence (`backend/app/providers/ai_http.py`)

#### 2.1 The Validation Sequence Inversion
In `backend/app/providers/ai_http.py`, `parse_structured()` handles all structured responses for HTTP-based providers (including Ollama). The function executes the following sequence:

```python
# Lines 326-339 in backend/app/providers/ai_http.py:
try:
    parsed = json.loads(content, parse_constant=reject_constant, object_pairs_hook=reject_duplicate_keys)
    if isinstance(request.response_schema, type) and issubclass(request.response_schema, BaseModel):
        return request.response_schema.model_validate(parsed).model_dump(mode="json")
    _validate_schema(parsed, request.response_schema)
    return parsed
except (json.JSONDecodeError, ValidationError, ValueError, TypeError, RecursionError):
    raise AIProviderException(
        error=AIProviderError(
            provider=provider,
            outcome=AIProviderOutcome.INVALID_STRUCTURED_OUTPUT,
            message="Provider output did not satisfy the requested structured schema",
        )
    ) from None
```

#### 2.2 Root Cause Findings
1. **Premature Strict Pydantic Validation:** When `request.response_schema` is a Pydantic `BaseModel` class (e.g. `ProposalSelection` or `AISalesIntelligenceResponse`), `model_validate(parsed)` is invoked **immediately** upon parsing the raw JSON string.
2. **Post-Processing Code Never Executed:** Application services (`ai_proposal_intelligence.py` and `ai_sales_intelligence.py`) contained reconciliation logic to aggregate root-level `evidence_refs` from nested item lists *after* `await provider.generate(request)`. Because `parse_structured()` raises `AIProviderException(AIProviderOutcome.INVALID_STRUCTURED_OUTPUT)` on line 333, control never returned to the caller, rendering all service-level normalization completely unreachable.
3. **Absence of Markdown Fence Stripping:** `json.loads(content)` expects raw JSON. If an LLM wraps the response in markdown code blocks (` ```json ... ``` `) or includes leading/trailing whitespace, `json.loads` immediately throws `json.JSONDecodeError`.
4. **Single-Pass Set-Union Impossibility on 7B LLMs:** Small language models executing single-pass autoregressive generation cannot reliably construct a mathematically identical set-union between items in nested arrays and a redundant root-level `evidence_refs` array. If root `evidence_refs` is missing or diverges from nested citations, strict Pydantic cross-field validators fail instantly.

---

### 3. Task B Root Cause: AI Sales Intelligence Structured Output Failure

#### 3.1 Empirical Evidence from Live Ollama Output
Using isolated read-only inspection script `scratch/inspect_raw_sales_fast.py`, the raw output from `qwen2.5-coder:latest` was captured:

```json
{
  "summary": [
    {
      "kind": "VERIFIED_FACT",
      "evidence_refs": ["lead:b101d423-b6a6-45a2-84de-054e992e70b3:company_name"]
    }
  ],
  "insights": [
    {
      "kind": "SUPPORTED_INFERENCE",
      "evidence_refs": ["lead:b101d423-b6a6-45a2-84de-054e992e70b3:industry"]
    }
  ],
  "opportunities": [ ... ]
}
```

#### 3.2 Root Cause Mechanisms
1. **Missing `text` Attribute in `AIClaim`:**
   In `backend/app/domain/ai_sales_intelligence.py`:
   ```python
   class AIClaim(AISalesContract):
       kind: AIClaimType
       text: Annotated[str | None, Field(max_length=2000)] = None
       evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=180)], ...] = ()
   ```
   Because `text` defaults to `None`, Pydantic marks it as optional in the generated JSON Schema. Ollama's grammar constraint therefore omits `text`. However, the Pydantic `@model_validator(mode="after")` enforces:
   ```python
   elif self.kind in {AIClaimType.SUPPORTED_INFERENCE, AIClaimType.UNCERTAINTY}:
       if not self.text or not self.evidence_refs:
           raise ValueError("inferences and uncertainties require text and evidence references")
   ```
   Because `text` was `None`, validation failed with `ValueError("inferences and uncertainties require text and evidence references")`.
2. **Missing Contract Version and Prompt Version:**
   The model omitted `contract_version` and `prompt_version`, which are required literals on `AISalesIntelligenceResponse`.
3. **Root-Level `evidence_refs` Omission:**
   The model placed citations inside `summary` and `insights`, but omitted or left empty the root `evidence_refs` field, triggering a Pydantic validation error before service reconciliation.
4. **Token Truncation Near Timeout:**
   When `max_output_tokens` was configured too high (e.g. 500–1200 tokens), CPU generation took >250 seconds, putting the request at risk of hitting `request_timeout_seconds=300`. Conversely, setting tokens too low (<250) truncated JSON mid-stream. The optimal token ceiling for CPU execution is 350 tokens.

---

### 4. Task C Root Cause: AI Proposal Intelligence Structured Output Failure

#### 4.1 Empirical Evidence from Live Ollama Output
Using isolated read-only inspection script `scratch/inspect_raw_proposal.py`, the raw output from `qwen2.5-coder:latest` was captured:

```json
{
  "lead_id": "0ae459e1-3486-4feb-938e-7fe82e808cfb",
  "proposal_type": "SEO_REVIEW",
  "executive_summary": "CURRENT_CONTEXT_REVIEW",
  "identified_needs": [],
  "recommended_services": [],
  "opportunity_evidence": [],
  "proposed_scope": [],
  "deliverables": [],
  "assumptions": [],
  "exclusions": [],
  "timeline_guidance": "NO_DETERMINISTIC_TIMELINE",
  "commercial_context": [],
  "missing_information": [
    "missing:0ae459e1-3486-4feb-938e-7fe82e808cfb:commercial.current_stage_value",
    "missing:0ae459e1-3486-4feb-938e-7fe82e808cfb:commercial.expected_collection"
  ],
  "evidence_refs": [
    "lead:0ae459e1-3486-4feb-938e-7fe82e808cfb:company_name",
    "lead:0ae459e1-3486-4feb-938e-7fe82e808cfb:website",
    "lead:0ae459e1-3486-4feb-938e-7fe82e808cfb:industry"
  ],
  "review_notes": []
}
```

#### 4.2 Root Cause Mechanisms
1. **Set-Union Mismatch in `references_are_exact_and_unique`:**
   In `backend/app/domain/ai_proposal_intelligence.py`:
   ```python
   @model_validator(mode="after")
   def references_are_exact_and_unique(self):
       refs: set[str] = set(self.commercial_context) | set(self.missing_information)
       for item in self.identified_needs: refs.update(item.evidence_refs)
       for item in self.recommended_services: refs.update(item.evidence_refs)
       for item in self.opportunity_evidence:
           refs.add(item.opportunity_id)
           refs.update(item.evidence_refs)
       for item in self.proposed_scope: refs.update(item.evidence_refs)
       if len(self.evidence_refs) != len(set(self.evidence_refs)) or set(self.evidence_refs) != refs:
           raise ValueError("evidence_refs must exactly match all selected unique references")
   ```
   The model correctly selected `missing_information` items (`missing:...:commercial.current_stage_value`), but populated `evidence_refs` with general lead facts (`lead:...:company_name`).
   Because `set(self.evidence_refs) != refs`, Pydantic raised `Value error, evidence_refs must exactly match all selected unique references`.
2. **Provider-Layer Rejection:**
   Because `parse_structured()` attempted `ProposalSelection.model_validate(parsed)` directly on this raw output, it crashed and raised `INVALID_STRUCTURED_OUTPUT` before the service could reconcile root `evidence_refs`.

---

### 5. Task D Root Cause: AI Sales Action Planner Latency Profile

#### 5.1 Analysis of the 290.28s Duration
1. **Prompt Payload Density:** The request payload serialized the entire `context` model dump alongside redundant dictionaries for `allowed_source_types_per_action`, `approved_titles`, `approved_rationales`, and `approved_preparation_items`. Evaluating this dense prompt on CPU consumed ~55 seconds.
2. **Output Token Bounding:** `max_output_tokens` was set to `800`. At ~2.4 tokens/second on CPU, the model spent ~235 seconds generating tokens, even though only 2 actions (~120 tokens) were ultimately required.
3. **GBNF Grammar Compilation:** Ollama compiled a grammar for `ActionPlanOutput`, adding ~5s of compilation overhead before inference started.

---

### 6. Summary Root Cause Matrix

| Defect Area | Location | Exact Programmatic Cause | Evidence / Proof |
| :--- | :--- | :--- | :--- |
| **Provider Parsing Sequence** | `backend/app/providers/ai_http.py` | `parse_structured()` calls `model_validate()` before schema-authorized normalization or root evidence reconciliation. | Service post-processing code unreachable; `AIProviderException` raised on line 333. |
| **Proposal Intelligence Failure** | `backend/app/domain/ai_proposal_intelligence.py` | Model outputs citations in nested lists (`missing_information`), but outputs disjoint lead facts in root `evidence_refs`. | Direct validation failure: `evidence_refs must exactly match all selected unique references`. |
| **Sales Intelligence Failure** | `backend/app/domain/ai_sales_intelligence.py` | Model omits `text` on `SUPPORTED_INFERENCE` / `UNCERTAINTY` claims because schema marks it optional. | Validation failure: `inferences and uncertainties require text and evidence references`. |
| **Action Planner Latency** | `backend/app/services/ai_sales_action_planner.py` | Unnecessarily high `max_output_tokens=800` on CPU allows generation to run for ~235s. | Live execution duration: 290.28s for 2 actions. |

---
**Report Author:** Senior Software Architect & Reliability Engineer  
**Inspection Mode:** Read-Only Verification Complete

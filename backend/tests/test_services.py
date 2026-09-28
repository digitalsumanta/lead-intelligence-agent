"""Unit and database-backed tests for import, provider, jobs, and scoring."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.models import Lead, ProcessingJob, LeadStatus, JobStatus, Personalization
from app.models.entities import EnrichmentRun
from app.services.csv_import import normalize_row, validate_row, import_csv
from app.services.jobs import retry_delay, create_job, run_job
from app.services.scoring import score_lead
from app.services.key_pool import ApiKeyPool
from app.providers.base import AIProvider
from app.providers import get_provider
from app.schemas.lead import AIAnalysis
from app.models import ApiKey
from app.providers.ollama import OllamaProvider
from app.services.key_config import sync_environment_keys

def personalization_result(request, *, summary="Local company", video="Show page", package="Growth"):
    context=json.loads(request.user_prompt.split("\n", 1)[1])
    del summary, video, package
    def section(text, service_ids=()):
        return {"text":text,"claims":[{"type":"suggested_language","text":text,"evidence_ids":[],"opportunity_ids":[],"service_ids":list(service_ids)}]}
    service_ids=[item["service_id"] for item in context["recommendations"]]
    sales="Would you be open to a brief conversation?"
    package_text="Would you like to explore the recommended services?"
    return SimpleNamespace(parsed_json={
        "contract_version":"personalization-v2","prompt_version":"evidence-grounded-prompt-v2",
        "recommended_service_ids":service_ids,
        "sales_opening":section(sales),"audit_summary":section(sales),
        "audit_video_script":section(sales),"recommended_package_language":section(package_text, service_ids),
        "confidence_score":0.8,
    })

def test_normalizes_and_adds_https():
    assert normalize_row({"company_name":" Acme ","website":" acme.com ","email":" SALES@ACME.COM "}) == {"company_name":"Acme","website":"https://acme.com","email":"sales@acme.com"}

def test_empty_values_normalize_to_none():
    assert normalize_row({"phone":"  ","city":" Austin "}) == {"phone":None,"city":"Austin"}

def test_company_name_required():
    assert "company_name is required" in validate_row({"company_name":None})

def test_valid_email_accepted():
    assert validate_row({"company_name":"Acme","email":"a@acme.test"}) == []

def test_invalid_email_rejected():
    assert "email is invalid" in validate_row({"company_name":"Acme","email":"broken"})

def test_valid_url_accepted():
    assert validate_row({"company_name":"Acme","website":"https://acme.com"}) == []

def test_invalid_url_rejected():
    assert "website URL is invalid" in validate_row({"company_name":"Acme","website":"https://bad"})

@pytest.mark.parametrize("attempt,expected", [(1,5),(2,15),(3,60),(4,60)])
def test_exponential_retry_delays(attempt, expected):
    assert retry_delay(attempt) == expected

@pytest.mark.asyncio
async def test_import_accepts_bom_and_creates_lead(session_factory):
    async with session_factory() as db:
        result = await import_csv(db, b'\xef\xbb\xbfcompany_name,email\nAcme,hello@acme.test\n', "x.csv")
        assert (result["accepted_rows"],result["invalid_rows"]) == (1,0)
        leads = (await db.scalars(select(Lead))).all()
        assert leads[0].company_name == "Acme"

@pytest.mark.asyncio
async def test_import_detects_duplicate_case_insensitively(session_factory):
    async with session_factory() as db:
        csv = b"company_name,city\nAcme,Austin\nACME,AUSTIN\n"
        result = await import_csv(db, csv, "x.csv")
        assert result["accepted_rows"] == 1 and result["duplicate_rows"] == 1

@pytest.mark.asyncio
async def test_import_tracks_invalid_without_aborting(session_factory):
    async with session_factory() as db:
        result = await import_csv(db, b"company_name,email\nGood,ok@good.test\nBad,invalid\n,\n", "x.csv")
        assert result["accepted_rows"] == 1 and result["invalid_rows"] == 2

@pytest.mark.asyncio
async def test_import_rejects_missing_required_header(session_factory):
    async with session_factory() as db:
        with pytest.raises(ValueError, match="company_name"):
            await import_csv(db, b"name\nAcme\n", "x.csv")

def test_provider_selection_uses_local_ollama_without_cloud_keys(monkeypatch):
    monkeypatch.setattr("app.providers.settings.ai_provider", "ollama")
    assert get_provider().name == "ollama"

def test_unconfigured_cloud_provider_fails_clearly(monkeypatch):
    monkeypatch.setattr("app.providers.settings.openai_api_key", "")
    with pytest.raises(ValueError, match="not configured"): get_provider("openai")

def test_ai_schema_rejects_invalid_confidence():
    with pytest.raises(ValueError): AIAnalysis(company_summary="x",business_category="x",personalized_sales_opening="x",audit_video_script="x",proposed_package="x",confidence_score=4)

@pytest.mark.asyncio
async def test_ai_provider_extracts_json_from_fence():
    payload={"company_summary":"Summary","business_category":"Retail","personalized_sales_opening":"Hi","audit_video_script":"Review site","proposed_package":"Starter","confidence_score":0.7}
    class MockProvider(AIProvider):
        name="mock"
        async def generate(self,prompt): return "Here it is: ```json\n"+json.dumps(payload)+"\n```"
    result=await MockProvider().analyze_lead({}, {})
    assert result.business_category == "Retail"

@pytest.mark.asyncio
async def test_ai_provider_retries_malformed_json_once():
    payload={"company_summary":"Summary","business_category":"Retail","personalized_sales_opening":"Hi","audit_video_script":"Review site","proposed_package":"Starter","confidence_score":0.7}
    class MockProvider(AIProvider):
        name="mock"
        def __init__(self): self.calls=0
        async def generate(self,prompt):
            self.calls+=1
            return "not json" if self.calls==1 else json.dumps(payload)
    provider=MockProvider()
    result=await provider.analyze_lead({}, {})
    assert result.business_category == "Retail" and provider.calls == 2

@pytest.mark.asyncio
async def test_create_job_persists_queued_state(session_factory):
    async with session_factory() as db:
        lead=Lead(company_name="Acme");db.add(lead);await db.commit()
        job=await create_job(db,lead)
        assert job.status == JobStatus.QUEUED and lead.status == LeadStatus.QUEUED

@pytest.mark.asyncio
async def test_pipeline_completes_and_calculates_score(session_factory):
    payload={"company_summary":"Local company","business_category":"Retail","website_problems":[],"seo_opportunities":["Improve title"],"digital_marketing_opportunities":["Local ads"],"pain_points":["Low reach"],"recommended_services":["SEO"],"suggested_keywords":["shop"],"competitors":[],"personalized_sales_opening":"Hi","audit_video_script":"Show page","proposed_package":"Growth","confidence_score":0.8}
    class MockProvider:
        name="mock";model="mock"
        async def generate(self, request): return personalization_result(request, summary=payload["company_summary"], video=payload["audit_video_script"], package=payload["proposed_package"])
    class MockAnalyzer:
        async def analyze(self,url): return {"available":True,"status_code":200,"https":True,"title":"Title","meta_description":"Description","issues":[]}
    async with session_factory() as db:
        lead=Lead(company_name="Acme",website="https://acme.test",industry="Retail",email="a@a.test");db.add(lead);await db.commit()
        job=await create_job(db,lead)
        await run_job(db,job,MockAnalyzer(),MockProvider())
        await db.refresh(job);await db.refresh(lead)
        assert job.status == JobStatus.COMPLETED and job.checkpoint == "COMPLETED"
        assert lead.lead_score is not None and 0 <= lead.lead_score <= 100
        assert lead.score_factors["https"] == 10
        assert set(lead.ai_analysis) == set(AIAnalysis.model_fields)
        assert lead.ai_summary == lead.ai_analysis["company_summary"]
        assert lead.sales_opening == lead.ai_analysis["personalized_sales_opening"]
        assert lead.recommended_services == lead.ai_analysis["recommended_services"]
        assert lead.keyword_opportunities == [] and lead.competitors == []
        assert lead.decision_maker is None

@pytest.mark.asyncio
async def test_personalization_failure_does_not_fail_successful_enrichment(session_factory):
    class BadProvider:
        async def generate(self, request): raise RuntimeError("provider unavailable")
    class MockAnalyzer:
        async def analyze(self,url): return {"available":False,"issues":[]}
    async with session_factory() as db:
        lead=Lead(company_name="Acme");db.add(lead);await db.commit()
        job=await create_job(db,lead)
        await run_job(db,job,MockAnalyzer(),BadProvider());await db.refresh(job)
        assert job.status == JobStatus.RETRYING and job.next_retry_at is not None
        artifact = await db.scalar(select(Personalization))
        assert artifact.status == "failed"
        assert artifact.generated is None
        enrichment = await db.scalar(select(EnrichmentRun).where(EnrichmentRun.processing_job_id == job.id))
        assert enrichment.status == "completed"

@pytest.mark.asyncio
async def test_ai_retry_resumes_same_run_and_next_run_clears_old_decision_maker(session_factory):
    class RetryProvider:
        calls=0
        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            return personalization_result(request)
    class GoodProvider:
        async def generate(self, request): return personalization_result(request)
    class NoWebsite:
        async def analyze(self,url): return {"available":False,"issues":[]}

    provider=RetryProvider()
    async with session_factory() as db:
        lead=Lead(company_name="Resume Co", decision_maker="Old AI generated contact")
        db.add(lead); await db.commit()
        job=await create_job(db,lead)
        await run_job(db,job,NoWebsite(),provider)
        await db.refresh(job)
        assert job.status == JobStatus.RETRYING and job.checkpoint == "AI_ANALYSIS_STARTED"
        failed_artifact=await db.scalar(select(Personalization))
        failed_id=failed_artifact.id
        enrichment=await db.scalar(select(EnrichmentRun).where(EnrichmentRun.processing_job_id == job.id))
        run_id=enrichment.id
        assert enrichment.status == "completed"
        await run_job(db,job,NoWebsite(),provider)
        await db.refresh(job)
        assert job.status == JobStatus.COMPLETED
        assert await db.scalar(select(Personalization.id).where(Personalization.enrichment_run_id == run_id)) == failed_id
        lead.decision_maker="Prior run AI contact"
        await db.commit()
        next_job=await create_job(db,lead)
        await run_job(db,next_job,NoWebsite(),GoodProvider())
        await db.refresh(lead)
        assert lead.decision_maker is None
        async with session_factory() as verify_db:
            runs=(await verify_db.scalars(select(EnrichmentRun).where(EnrichmentRun.lead_id == lead.id))).all()
            artifacts=(await verify_db.scalars(select(Personalization).where(Personalization.lead_id == lead.id))).all()
        assert len(runs) == 2 and len(artifacts) == 2
        assert all(row.status == "validated" for row in artifacts), [(row.status, row.validation_result) for row in artifacts]

def test_scoring_factors_are_bounded_and_explainable():
    lead=SimpleNamespace(industry="Retail",city="Austin",state=None,country="US",address=None,email="a@b.test",phone=None)
    score,factors=score_lead(lead,{"available":True,"status_code":200,"https":False,"issues":["title"]},{"seo_opportunities":["title"],"digital_marketing_opportunities":[]})
    assert 0 <= score <= 100 and factors["https"] == 0 and factors["contact_availability"] == 5

@pytest.mark.asyncio
async def test_key_pool_rotates_and_cools_down_failed_key(session_factory):
    pool=ApiKeyPool()
    async with session_factory() as db:
        a=ApiKey(provider="openai",key="secret-a");b=ApiKey(provider="openai",key="secret-b");db.add_all([a,b]);await db.commit()
        first=await pool.select(db,"openai")
        await pool.mark_failure(db,first,"mock failure",cooldown_seconds=600)
        second=await pool.select(db,"openai")
        assert second.id != first.id
        assert await pool.health(db,"openai") == {"configured":2,"healthy":1}

@pytest.mark.asyncio
async def test_key_pool_retries_operation_on_next_key(session_factory):
    pool=ApiKeyPool();attempted=[]
    async with session_factory() as db:
        db.add_all([ApiKey(provider="gemini",key="key-1"),ApiKey(provider="gemini",key="key-2")]);await db.commit()
        async def operation(key):
            attempted.append(key)
            if len(attempted) == 1: raise OSError("temporary provider error")
            return "success"
        assert await pool.execute_with_rotation(db,"gemini",operation) == "success"
        assert len(attempted)==2 and attempted[0] != attempted[1]

@pytest.mark.asyncio
async def test_environment_keys_are_registered_idempotently(session_factory, monkeypatch):
    monkeypatch.setattr("app.services.key_config.settings.openai_api_key", "")
    monkeypatch.setattr("app.services.key_config.settings.openai_api_keys", "one,two,one")
    async with session_factory() as db:
        await sync_environment_keys(db); await sync_environment_keys(db)
        rows=(await db.scalars(select(ApiKey).where(ApiKey.provider=="openai"))).all()
        assert {row.key for row in rows} == {"one","two"} and len(rows)==2

@pytest.mark.asyncio
async def test_checkpoint_recovery_skips_website_and_ai(session_factory):
    class ShouldNotRun:
        async def analyze(self,url): raise AssertionError("website should not repeat")
    async with session_factory() as db:
        lead=Lead(company_name="Acme",website_analysis={"available":False},ai_analysis={"seo_opportunities":[],"digital_marketing_opportunities":[]});db.add(lead);await db.commit()
        job=ProcessingJob(lead_id=lead.id,checkpoint="AI_ANALYSIS_COMPLETED",status=JobStatus.RETRYING);db.add(job);await db.commit()
        await run_job(db,job,analyzer=ShouldNotRun(),provider_instance=None)
        await db.refresh(job)
        assert job.status == JobStatus.COMPLETED and job.checkpoint == "COMPLETED"

@pytest.mark.asyncio
async def test_mock_ollama_http_response(monkeypatch):
    import app.providers.ollama as module
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"response":' {"ok":true} '}
    class Client:
        def __init__(self,*args,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return None
        async def post(self,*args,**kwargs): return Response()
    monkeypatch.setattr(module.httpx,"AsyncClient",Client)
    assert await OllamaProvider(base_url="http://mock",model="local").generate("prompt") == ' {"ok":true} '

@pytest.mark.asyncio
async def test_health_endpoint(client=None):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        response=await client.get("/health")
    assert response.status_code == 200 and response.json()["status"] == "healthy"

@pytest.mark.asyncio
async def test_process_many_uses_bounded_concurrency_and_mock_provider(session_factory):
    import asyncio
    payload={"company_summary":"Summary","business_category":"Retail","personalized_sales_opening":"Hi","audit_video_script":"Review","proposed_package":"Starter","confidence_score":0.8}
    class MockProvider:
        async def analyze_lead(self, lead, website):
            await asyncio.sleep(0.01)
            return AIAnalysis.model_validate(payload)
        async def generate(self, request):
            await asyncio.sleep(0.01)
            return personalization_result(request, summary=payload["company_summary"], video=payload["audit_video_script"], package=payload["proposed_package"])
    class MockAnalyzer:
        async def analyze(self,url): return {"available":False,"issues":[]}
    from app.services.jobs import process_many
    async with session_factory() as db:
        leads=[Lead(company_name=f"Biz {i}") for i in range(3)];db.add_all(leads);await db.commit()
        jobs=[await create_job(db,lead) for lead in leads]
    results=await process_many(session_factory,jobs,2,analyzer=MockAnalyzer(),provider_instance=MockProvider())
    assert len(results)==3 and all(j.status==JobStatus.COMPLETED for j in results)

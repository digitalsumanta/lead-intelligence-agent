"""Mocked durable-job integrations for cloud adapters through the legacy API."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import ApiKey, JobStatus, Lead, ProcessingJob, ProviderUsage
from app.providers import get_provider
from app.schemas.lead import AIAnalysis
from app.services.jobs import run_job


def analysis_payload():
    return AIAnalysis(
        company_summary="Verified test summary",
        business_category="Retail",
        website_problems=[],
        seo_opportunities=[],
        digital_marketing_opportunities=[],
        pain_points=[],
        recommended_services=["SEO"],
        suggested_keywords=[],
        competitors=[],
        decision_maker=None,
        personalized_sales_opening="Hello",
        audit_video_script="Review the site",
        proposed_package="Starter",
        confidence_score=0.8,
    ).model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_name", "operation"),
    [("gemini", "generate_content"), ("openai_compatible", "chat_completions")],
)
async def test_run_job_cloud_provider_selection_and_legacy_analysis_path(
    session_factory, monkeypatch, provider_name, operation
):
    from app.services import jobs

    model = f"mock-{provider_name}"
    key = f"test-{provider_name}-credential"
    async with session_factory() as session:
        lead = Lead(company_name="Mock Business")
        session.add(lead)
        await session.flush()
        job = ProcessingJob(lead_id=lead.id, checkpoint="WEBSITE_ANALYZED", provider=provider_name, model=model)
        session.add(job)
        session.add(ApiKey(provider=provider_name, key=key))
        await session.commit()
        job_id, lead_id = job.id, lead.id

    expected_analysis = analysis_payload()
    content = json.dumps(expected_analysis)
    async def handler(request):
        if provider_name == "gemini":
            assert request.url.path.endswith(f"/models/{model}:generateContent")
            assert request.headers["x-goog-api-key"] == key
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": content}]}}]})
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == f"Bearer {key}"
        return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": content}}]})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    config_values = {"ai_gemini_enabled": True} if provider_name == "gemini" else {"ai_openai_compatible_enabled": True}
    config = Settings(_env_file=None, **config_values)

    def select_provider(name, selected_model):
        assert name == provider_name and selected_model == model
        return get_provider(name, selected_model, session_factory=session_factory, config=config, client_factory=client_factory)

    monkeypatch.setattr(jobs, "get_provider", select_provider)
    async with session_factory() as session:
        job = await session.get(ProcessingJob, job_id)
        completed = await run_job(session, job, session_factory=session_factory)
        assert completed.status == JobStatus.COMPLETED
        lead = await session.get(Lead, lead_id)
        assert lead.ai_analysis["company_summary"] == "Verified test summary"
        assert lead.recommended_services == ["SEO"]
        usages = (await session.scalars(select(ProviderUsage).where(ProviderUsage.provider == provider_name))).all()
        assert any(item.operation == operation and item.outcome == "success" for item in usages)
        assert any(item.job_id == job_id and item.operation is None and item.success for item in usages)

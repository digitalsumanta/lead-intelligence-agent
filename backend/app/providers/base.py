"""Provider contract and resilient JSON decoding."""
from abc import ABC, abstractmethod
from typing import Any
import json, re
from app.schemas.lead import AIAnalysis

class AIProvider(ABC):
    """Provider interface used by lead intelligence services."""
    name = "base"
    model = ""
    @abstractmethod
    async def generate(self, prompt: str) -> str: ...
    async def generate_structured(self, prompt: str, schema: dict) -> str:
        """Generate structured JSON; providers can apply native schema constraints."""
        return await self.generate(prompt)
    async def analyze_lead(self, lead: dict, website: dict) -> AIAnalysis:
        """Ask provider for structured sales intelligence and validate it."""
        prompt = ("Return only strict JSON with keys company_summary,business_category,website_problems,seo_opportunities," 
                  "digital_marketing_opportunities,pain_points,recommended_services,suggested_keywords,competitors,"
                  "personalized_sales_opening,audit_video_script,proposed_package,confidence_score,decision_maker. "
                  "Do not invent facts; use null for a decision maker that is not identifiable.\n" 
                  + json.dumps({"lead": lead, "website": website}, ensure_ascii=False))
        for attempt in range(2):
            attempt_prompt = prompt if attempt == 0 else prompt + "\nYour previous response did not match the schema. Return one valid JSON object only."
            raw = await self.generate_structured(attempt_prompt, AIAnalysis.model_json_schema())
            for candidate in (raw, self._extract(raw)):
                try:
                    data = json.loads(candidate)
                    return AIAnalysis.model_validate(data)
                except (ValueError, TypeError):
                    continue
        raise ValueError("AI provider returned invalid analysis JSON")
    async def health_check(self) -> dict[str, Any]:
        return {"provider": self.name, "healthy": True, "model": self.model}
    @staticmethod
    def _extract(raw: str) -> str:
        match = re.search(r"\{[\s\S]*\}", raw)
        return match.group(0) if match else ""

class OpenAIProvider(AIProvider):
    """Reserved provider; configured only when an OpenAI key is available."""
    name = "openai"
    async def generate(self, prompt: str) -> str:
        raise NotImplementedError("OpenAI provider is an extension point")

class GeminiProvider(AIProvider):
    """Reserved provider; configured only when a Gemini key is available."""
    name = "gemini"
    async def generate(self, prompt: str) -> str:
        raise NotImplementedError("Gemini provider is an extension point")

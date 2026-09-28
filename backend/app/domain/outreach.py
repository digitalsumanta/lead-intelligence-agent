"""Strict contracts and safe reusable language for human-reviewed outreach."""
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator

OUTREACH_CONTRACT_VERSION = "outreach-v1"
OUTREACH_PROMPT_VERSION = "grounded-outreach-prompt-v1"

SalesStage = Literal["NEW", "QUALIFIED", "CONTACTED", "ENGAGED", "PROPOSAL", "NEGOTIATION"]
EmailSubject = Literal[
    "A quick introduction", "A few qualification questions", "Following up",
    "Next steps", "Discussing the proposal", "Next steps on the proposal",
]
DiscoveryQuestion = Literal[
    "Which business outcome matters most to you right now?",
    "How are you currently evaluating this area?",
    "What would a useful next step look like?",
    "Who else should be involved in evaluating next steps?",
]
FollowUpStep = Literal[
    "SEND_FOLLOW_UP_NOTE", "OFFER_DISCOVERY_CALL", "SHARE_AUDIT_FINDING", "CONFIRM_DECISION_PROCESS",
]
FollowUpTiming = Literal["NEXT_BUSINESS_DAY", "WITHIN_THREE_DAYS", "WITHIN_ONE_WEEK"]
FollowUpPurpose = Literal[
    "REOPEN_CONVERSATION", "CLARIFY_PRIORITIES", "DISCUSS_OPPORTUNITY", "ANSWER_OBJECTIONS", "CONFIRM_NEXT_STEP",
]

STAGE_SUBJECTS = {
    "NEW": {"A quick introduction"},
    "QUALIFIED": {"A few qualification questions"},
    "CONTACTED": {"Following up"},
    "ENGAGED": {"Next steps"},
    "PROPOSAL": {"Discussing the proposal"},
    "NEGOTIATION": {"Next steps on the proposal"},
}
STAGE_QUESTIONS = {
    "NEW": {"Which business outcome matters most to you right now?", "What would a useful next step look like?"},
    "QUALIFIED": {"Which business outcome matters most to you right now?", "How are you currently evaluating this area?", "Who else should be involved in evaluating next steps?"},
    "CONTACTED": {"What would a useful next step look like?", "How are you currently evaluating this area?"},
    "ENGAGED": {"Which business outcome matters most to you right now?", "How are you currently evaluating this area?", "What would a useful next step look like?"},
    "PROPOSAL": {"How are you currently evaluating this area?", "Who else should be involved in evaluating next steps?", "What would a useful next step look like?"},
    "NEGOTIATION": {"Who else should be involved in evaluating next steps?", "What would a useful next step look like?"},
}
STAGE_FOLLOW_UPS = {
    "NEW": {"OFFER_DISCOVERY_CALL", "SEND_FOLLOW_UP_NOTE"},
    "QUALIFIED": {"OFFER_DISCOVERY_CALL", "CONFIRM_DECISION_PROCESS"},
    "CONTACTED": {"SEND_FOLLOW_UP_NOTE", "SHARE_AUDIT_FINDING"},
    "ENGAGED": {"OFFER_DISCOVERY_CALL", "CONFIRM_DECISION_PROCESS"},
    "PROPOSAL": {"SHARE_AUDIT_FINDING", "CONFIRM_DECISION_PROCESS", "SEND_FOLLOW_UP_NOTE"},
    "NEGOTIATION": {"CONFIRM_DECISION_PROCESS", "SEND_FOLLOW_UP_NOTE"},
}
SAFE_OUTREACH_PHRASES = frozenset({
    "Would you be open to a brief conversation?",
    "Would you like to discuss the opportunities identified in this audit?",
    "Would you like to explore the recommended services?",
    "Thank you for your time.",
    "One possible next step is a short discovery conversation.",
})


class OutreachContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OutreachClaim(OutreachContract):
    type: Literal["verified_fact", "supported_inference", "suggested_language"]
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_ids: tuple[UUID, ...] = ()
    opportunity_ids: tuple[UUID, ...] = ()
    service_ids: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = ()

    @model_validator(mode="after")
    def citations_follow_claim_type(self):
        if self.type == "verified_fact" and not self.evidence_ids:
            raise ValueError("verified facts require evidence references")
        if self.type == "supported_inference" and (not self.evidence_ids or not self.opportunity_ids):
            raise ValueError("supported inferences require evidence and opportunity references")
        if self.type == "suggested_language" and not self.service_ids and self.text not in SAFE_OUTREACH_PHRASES:
            raise ValueError("suggested language must use approved non-factual wording")
        if self.type == "suggested_language" and self.service_ids and (self.evidence_ids or self.opportunity_ids):
            raise ValueError("service language must be tied only to deterministic service IDs")
        if self.type != "suggested_language" and self.service_ids:
            raise ValueError("service references are only allowed on suggested language")
        return self


class OutreachText(OutreachContract):
    text: Annotated[str, Field(min_length=1, max_length=8000)]
    claims: tuple[OutreachClaim, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def text_matches_claims(self):
        if self.text != "\n".join(claim.text for claim in self.claims):
            raise ValueError("outreach text must consist exactly of its annotated claims")
        return self


class EmailDraft(OutreachContract):
    subject: EmailSubject
    opening: OutreachText
    body: OutreachText
    call_to_action: OutreachText


class WhatsAppDraft(OutreachContract):
    opening: OutreachText
    message: OutreachText
    call_to_action: OutreachText


class CallScript(OutreachContract):
    opening: OutreachText
    key_problem: OutreachText
    recommended_service_ids: tuple[str, ...] = ()
    discovery_questions: tuple[DiscoveryQuestion, ...] = Field(min_length=1, max_length=4)
    objections_and_talking_points: OutreachText
    closing: OutreachText


class FollowUpSuggestion(OutreachContract):
    step: FollowUpStep
    timing: FollowUpTiming
    purpose: FollowUpPurpose


class OutreachContent(OutreachContract):
    email: EmailDraft
    whatsapp: WhatsAppDraft
    call_script: CallScript
    follow_ups: tuple[FollowUpSuggestion, ...] = Field(min_length=1, max_length=3)
    recommended_service_ids: tuple[str, ...]

    @model_validator(mode="after")
    def service_list_matches_call_script(self):
        if self.call_script.recommended_service_ids != self.recommended_service_ids:
            raise ValueError("call script service IDs must match the declared service list")
        if len(set(self.recommended_service_ids)) != len(self.recommended_service_ids):
            raise ValueError("recommended service IDs must be unique")
        return self


class OutreachResponse(OutreachContract):
    contract_version: Literal["outreach-v1"] = OUTREACH_CONTRACT_VERSION
    content: OutreachContent

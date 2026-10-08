import enum
from typing import Optional, Dict, Any, List
from app.models.entities import SalesActivityOutcome


class FeedbackSentiment(str, enum.Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


class AttributionLevel(str, enum.Enum):
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    UNATTRIBUTED = "UNATTRIBUTED"


OUTCOME_SENTIMENT_MAP: Dict[SalesActivityOutcome, FeedbackSentiment] = {
    # POSITIVE outcomes
    SalesActivityOutcome.CONNECTED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.INTERESTED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.CALLBACK_REQUESTED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.MEETING_BOOKED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.PROPOSAL_REQUESTED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.PROPOSAL_SENT: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.NEGOTIATION_STARTED: FeedbackSentiment.POSITIVE,
    SalesActivityOutcome.WON: FeedbackSentiment.POSITIVE,

    # NEGATIVE outcomes
    SalesActivityOutcome.NOT_INTERESTED: FeedbackSentiment.NEGATIVE,
    SalesActivityOutcome.LOST: FeedbackSentiment.NEGATIVE,
    SalesActivityOutcome.INVALID_CONTACT: FeedbackSentiment.NEGATIVE,

    # NEUTRAL outcomes
    SalesActivityOutcome.NO_RESPONSE: FeedbackSentiment.NEUTRAL,
}


def classify_outcome_sentiment(outcome: Optional[SalesActivityOutcome | str]) -> FeedbackSentiment:
    """
    Deterministic classification of sales activity outcome into feedback sentiment.
    """
    if outcome is None:
        return FeedbackSentiment.UNKNOWN
    if isinstance(outcome, str):
        try:
            outcome = SalesActivityOutcome(outcome)
        except ValueError:
            return FeedbackSentiment.UNKNOWN
    return OUTCOME_SENTIMENT_MAP.get(outcome, FeedbackSentiment.UNKNOWN)


def calculate_feedback_rates(
    total_executions: int,
    positive_count: int,
    negative_count: int,
    neutral_count: int,
) -> Dict[str, float]:
    """
    Calculate deterministic rate percentages rounded to 2 decimal places without division by zero.
    """
    if total_executions <= 0:
        return {
            "positive_rate": 0.0,
            "negative_rate": 0.0,
            "neutral_rate": 0.0,
        }
    return {
        "positive_rate": round((positive_count / total_executions) * 100.0, 2),
        "negative_rate": round((negative_count / total_executions) * 100.0, 2),
        "neutral_rate": round((neutral_count / total_executions) * 100.0, 2),
    }

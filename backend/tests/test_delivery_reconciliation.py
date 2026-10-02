from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.delivery_reconciliation import normalize_delivery_event_type, reconcile_delivery_events


def event(kind, recorded_offset=0, provider_offset=None, event_id=None):
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=event_id or uuid4(), event_type=kind,
        recorded_at=origin + timedelta(minutes=recorded_offset),
        provider_occurred_at=origin + timedelta(minutes=provider_offset) if provider_offset is not None else None,
    )


@pytest.mark.parametrize(("raw", "expected"), [
    (" delivery ", "DELIVERED"), ("hard_bounce", "BOUNCED"),
    ("spam complaint", "COMPLAINED"), ("future_event", None),
])
def test_delivery_event_aliases_are_explicit_and_unknown_is_unsupported(raw, expected):
    result = normalize_delivery_event_type(raw)
    assert result.event_type == expected
    assert result.status == ("SUPPORTED" if expected else "UNSUPPORTED")


def test_reconciliation_uses_terminal_precedence_and_detects_conflicting_terminal_states():
    result = reconcile_delivery_events(uuid4(), "smtp", "message", [
        event("BOUNCED", 0, 1), event("UNSUBSCRIBED", 1, 2), event("FAILED", 2, 3),
    ])
    assert result["current_state"] == "UNSUBSCRIBED"
    assert result["terminal_state"] == "UNSUBSCRIBED"
    assert result["has_terminal_event"] is True
    assert result["has_conflict"] is True
    assert result["conflict_types"] == ("MULTIPLE_TERMINAL_STATES",)
    assert result["event_count"] == 3


@pytest.mark.parametrize("kind", ["SENT", "DELIVERED", "BOUNCED", "FAILED", "COMPLAINED", "UNSUBSCRIBED"])
def test_single_canonical_event_derives_its_effective_state(kind):
    result = reconcile_delivery_events(uuid4(), "smtp", "message", [event(kind, 1, 1)])
    assert result["current_state"] == kind
    assert result["event_count"] == 1


def test_reconciliation_marks_provider_timestamp_conflicts_without_synthesized_times():
    result = reconcile_delivery_events(uuid4(), "smtp", "message", [
        event("BOUNCED", 0, 5), event("DELIVERED", 1, 7), event("SENT", 2, None),
    ])
    assert "DELIVERED_AFTER_TERMINAL" in result["conflict_types"]
    assert result["has_out_of_order_events"] is True
    assert result["first_event_at"].minute == 5
    assert result["last_event_at"].minute == 7

    inverted = reconcile_delivery_events(uuid4(), "smtp", "message", [
        event("SENT", 0, 4), event("DELIVERED", 1, 3),
    ])
    assert "EVENT_TIMESTAMP_OUT_OF_ORDER" in inverted["conflict_types"]

    missing_timestamp = reconcile_delivery_events(uuid4(), "smtp", None, [
        event("BOUNCED", 0, None), event("DELIVERED", 1, None),
    ])
    assert missing_timestamp["conflict_types"] == ()
    assert missing_timestamp["first_event_at"] is None
    assert missing_timestamp["last_event_at"] is None


def test_reconciliation_reports_unsupported_defensively_and_unknown_when_empty():
    result = reconcile_delivery_events(uuid4(), "smtp", None, [event("UNRECOGNIZED")])
    assert result["current_state"] == "UNKNOWN"
    assert result["unsupported_event_count"] == 1
    assert result["conflict_types"] == ("UNSUPPORTED_EVENT_TYPE",)
    empty = reconcile_delivery_events(uuid4(), "smtp", None, [])
    assert empty["current_state"] == "UNKNOWN"
    assert empty["event_count"] == 0


def test_sent_after_terminal_is_reported_without_rewriting_current_state():
    result = reconcile_delivery_events(uuid4(), "smtp", None, [
        event("BOUNCED", 0, 1), event("SENT", 1, 2),
    ])
    assert result["current_state"] == "BOUNCED"
    assert "SENT_AFTER_TERMINAL" in result["conflict_types"]

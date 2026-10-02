"""Deterministic normalization and read-only delivery-state reconciliation."""

from dataclasses import dataclass
from datetime import datetime


_ALIASES = {
    "sent": "SENT", "accepted": "SENT", "queued": "SENT",
    "delivered": "DELIVERED", "delivery": "DELIVERED",
    "bounce": "BOUNCED", "bounced": "BOUNCED", "hard_bounce": "BOUNCED", "soft_bounce": "BOUNCED",
    "failed": "FAILED", "failure": "FAILED",
    "complaint": "COMPLAINED", "complained": "COMPLAINED", "spam_complaint": "COMPLAINED",
    "unsubscribe": "UNSUBSCRIBED", "unsubscribed": "UNSUBSCRIBED",
}
_PRECEDENCE = ("UNSUBSCRIBED", "COMPLAINED", "BOUNCED", "FAILED", "DELIVERED", "SENT")
_TERMINAL = frozenset(("UNSUBSCRIBED", "COMPLAINED", "BOUNCED", "FAILED"))
_CONFLICT_ORDER = (
    "MULTIPLE_TERMINAL_STATES", "DELIVERED_AFTER_TERMINAL", "SENT_AFTER_TERMINAL",
    "EVENT_TIMESTAMP_OUT_OF_ORDER", "UNSUPPORTED_EVENT_TYPE",
)


@dataclass(frozen=True)
class EventTypeNormalization:
    normalized_value: str
    event_type: str | None
    status: str


def normalize_delivery_event_type(value: str) -> EventTypeNormalization:
    normalized = "_".join(value.strip().casefold().split())
    event_type = _ALIASES.get(normalized)
    return EventTypeNormalization(normalized, event_type, "SUPPORTED" if event_type else "UNSUPPORTED")


def reconcile_delivery_events(execution_id, provider: str, provider_message_id: str | None, events):
    ordered = sorted(events, key=lambda item: (item.recorded_at, str(item.id)))
    known = []
    unsupported = 0
    conflicts = set()
    for event in ordered:
        normalized = normalize_delivery_event_type(event.event_type)
        if normalized.event_type is None:
            unsupported += 1
            conflicts.add("UNSUPPORTED_EVENT_TYPE")
            continue
        known.append((event, normalized.event_type))

    timestamp_buckets = {}
    for event, _ in known:
        if event.provider_occurred_at is not None:
            timestamp_buckets.setdefault(event.recorded_at, []).append(event.provider_occurred_at)
    ordered_buckets = [timestamp_buckets[key] for key in sorted(timestamp_buckets)]
    if any(min(later) < max(earlier) for earlier, later in zip(ordered_buckets, ordered_buckets[1:])):
        conflicts.add("EVENT_TIMESTAMP_OUT_OF_ORDER")

    terminals = {kind for _, kind in known if kind in _TERMINAL}
    if len(terminals) > 1:
        conflicts.add("MULTIPLE_TERMINAL_STATES")
    terminal_events = [(event, kind) for event, kind in known if kind in _TERMINAL]
    for later_event, kind in known:
        if kind not in {"DELIVERED", "SENT"} or later_event.provider_occurred_at is None:
            continue
        if any(
            terminal.provider_occurred_at is not None
            and later_event.provider_occurred_at > terminal.provider_occurred_at
            for terminal, _ in terminal_events
        ):
            conflicts.add(f"{kind}_AFTER_TERMINAL")

    current_state = next((kind for kind in _PRECEDENCE if any(k == kind for _, k in known)), "UNKNOWN")
    terminal_state = next((kind for kind in _PRECEDENCE[:4] if kind in terminals), None)
    timestamps = [event.provider_occurred_at for event, _ in known if event.provider_occurred_at is not None]
    has_order_conflict = bool(conflicts & {
        "DELIVERED_AFTER_TERMINAL", "SENT_AFTER_TERMINAL", "EVENT_TIMESTAMP_OUT_OF_ORDER",
    })
    return {
        "execution_id": execution_id, "provider": provider,
        "provider_message_id": provider_message_id, "current_state": current_state,
        "terminal_state": terminal_state, "has_terminal_event": bool(terminals),
        "event_count": len(events), "has_conflict": bool(conflicts),
        "first_event_at": min(timestamps) if timestamps else None,
        "last_event_at": max(timestamps) if timestamps else None,
        "has_out_of_order_events": has_order_conflict,
        "conflict_types": tuple(code for code in _CONFLICT_ORDER if code in conflicts),
        "unsupported_event_count": unsupported,
    }

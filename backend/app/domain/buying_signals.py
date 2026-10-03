from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid
from app.models.entities import ConfidenceLevel, SourceType, SignalStrength, BuyingSignal


def normalize_signal_type(raw_type: str) -> str:
    """Normalize a raw string into a standard deterministic signal type."""
    return raw_type.strip().upper().replace(" ", "_")


def normalize_signal_category(raw_category: str) -> str:
    """Normalize a broad category."""
    return raw_category.strip().lower().replace(" ", "_")


def normalize_signal_source(raw_source: str) -> SourceType:
    """Ensure source fits exactly into SourceType enum."""
    try:
        return SourceType(raw_source)
    except ValueError:
        return SourceType.SYSTEM_DERIVED


def calculate_signal_confidence(evidence: Dict[str, Any]) -> ConfidenceLevel:
    """
    Determine confidence purely based on the presence of evidence properties.
    """
    if not evidence:
        return ConfidenceLevel.UNKNOWN
    
    # If the evidence contains an explicit confidence, inherit it.
    if "original_confidence" in evidence:
        try:
            return ConfidenceLevel(evidence["original_confidence"])
        except ValueError:
            pass

    # Basic rules based on the volume of evidence keys
    # Just a deterministic proxy for how much data supports the signal.
    if len(evidence) > 3:
        return ConfidenceLevel.HIGH
    elif len(evidence) > 1:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def calculate_signal_strength(evidence: Dict[str, Any]) -> SignalStrength:
    """
    Determine strength based on explicit proof criteria (e.g. multi-channel vs single).
    """
    if not evidence:
        return SignalStrength.LOW
    
    channels_involved = evidence.get("channels_involved", 1)
    if isinstance(channels_involved, int):
        if channels_involved >= 3:
            return SignalStrength.HIGH
        elif channels_involved == 2:
            return SignalStrength.MEDIUM
    return SignalStrength.LOW


def build_buying_signal(
    company_id: uuid.UUID,
    raw_type: str,
    raw_category: str,
    raw_source: str,
    evidence: Dict[str, Any],
    detected_at: datetime
) -> BuyingSignal:
    """
    Purely construct the ORM instance (not attached to session).
    """
    signal_type = normalize_signal_type(raw_type)
    signal_category = normalize_signal_category(raw_category)
    source_type = normalize_signal_source(raw_source)
    confidence = calculate_signal_confidence(evidence)
    strength = calculate_signal_strength(evidence)
    
    return BuyingSignal(
        company_id=company_id,
        signal_type=signal_type,
        signal_category=signal_category,
        source_type=source_type,
        confidence=confidence,
        strength=strength,
        detected_at=detected_at,
        evidence=evidence,
        status="ACTIVE"
    )


def deduplicate_signals(signals: List[BuyingSignal]) -> List[BuyingSignal]:
    """
    Remove duplicates that share the same signal_type and detected_at time exactly.
    """
    seen = set()
    unique = []
    for sig in signals:
        key = (sig.signal_type, sig.detected_at)
        if key not in seen:
            seen.add(key)
            unique.append(sig)
    return unique


def sort_signals_by_recency(signals: List[BuyingSignal]) -> List[BuyingSignal]:
    """
    Sort signals so the newest detected_at is first.
    """
    return sorted(signals, key=lambda s: s.detected_at, reverse=True)

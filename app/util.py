"""Petits utilitaires partagés."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("hm.util")


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def iso(moment: datetime | None = None) -> str:
    """ISO-8601 en UTC, suffixe `Z` (format attendu par Better Stack)."""
    moment = moment or utcnow()
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(value: str | None) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return (utcnow() - parsed).total_seconds()


def incident_id(moment: datetime | None = None) -> str:
    return (moment or utcnow()).astimezone(timezone.utc).strftime("inc_%Y%m%d_%H%M")

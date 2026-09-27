"""Finite relative-date vocabulary, resolved in the installation's explicit zone."""
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_RELATIVE = re.compile(
    r"\b(hoje|ontem|today|yesterday)(?:\s+(de manhã|de manha|pela manhã|pela manha|"
    r"à tarde|a tarde|de tarde|à noite|a noite|de noite|de madrugada|morning|afternoon|evening|night))?\s*$",
    re.IGNORECASE,
)


def resolve_history_time(query: str, installation_timezone: str | None, *, now: datetime | None = None):
    match = _RELATIVE.search(query)
    if match is None:
        return query, None
    try:
        zone = ZoneInfo(installation_timezone) if installation_timezone else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    if zone is None:
        return query, {"reason": "installation_timezone_required"}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Relative history queries require an aware reference time")
    day = current.astimezone(zone).date()
    if match[1].lower() in {"ontem", "yesterday"}:
        day -= timedelta(days=1)
    period = (match[2] or "").lower()
    hours = (0, 24)
    if period in {"de manhã", "de manha", "pela manhã", "pela manha", "morning"}:
        hours = (6, 12)
    elif period in {"à tarde", "a tarde", "de tarde", "afternoon"}:
        hours = (12, 18)
    elif period in {"à noite", "a noite", "de noite", "evening", "night"}:
        hours = (18, 24)
    elif period == "de madrugada":
        hours = (0, 6)
    start = datetime.combine(day, time(hour=hours[0]), zone)
    end = datetime.combine(day + timedelta(days=1), time(), zone) if hours[1] == 24 else datetime.combine(day, time(hour=hours[1]), zone)
    # Remove connective words only from a recognized relative query; ordinary literal search is unchanged.
    terms = re.sub(r"\b(no|na|nos|nas|do|da|dos|das|de|com|the|in|at|on|with)\b", " ", query[:match.start()], flags=re.IGNORECASE)
    return " ".join(terms.split()), {"time_range": {
        "timezone": installation_timezone, "created_after": start.timestamp(), "created_before": end.timestamp(),
        "basis": "notification_created_at", "expression": match[0].strip(),
    }}

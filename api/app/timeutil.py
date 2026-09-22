from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings


def local_date(moment: datetime) -> date:
    return moment.astimezone(ZoneInfo(settings.app_timezone)).date()


def today() -> date:
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def friendly_date(d: date | str, ref: Optional[date] = None) -> str:
    """'today', 'yesterday', or 'on Mon 21 Sep' (year added if not this year)."""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    ref = ref or today()
    if d == ref:
        return "today"
    if d == ref - timedelta(days=1):
        return "yesterday"
    label = f"{d:%a} {d.day} {d:%b}"
    if d.year != ref.year:
        label += f" {d.year}"
    return f"on {label}"

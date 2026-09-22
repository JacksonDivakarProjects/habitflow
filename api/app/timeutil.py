from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def local_date(moment: datetime) -> date:
    return moment.astimezone(ZoneInfo(settings.app_timezone)).date()


def today() -> date:
    return datetime.now(ZoneInfo(settings.app_timezone)).date()

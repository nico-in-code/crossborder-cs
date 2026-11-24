from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _enum_value(value):
    return getattr(value, "value", value)


def _datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _datetime_local(value: datetime | str | None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    return value.astimezone().strftime("%Y-%m-%dT%H:%M")


templates.env.filters["enum_value"] = _enum_value
templates.env.filters["dt"] = _datetime
templates.env.filters["dtlocal"] = _datetime_local
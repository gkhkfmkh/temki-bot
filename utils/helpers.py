"""Formatting, validation, safe Telegram edits and export helpers."""

from __future__ import annotations

import csv
import html
import io
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram.exceptions import TelegramBadRequest


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_db(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def safe_html(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def truncate(value: str, limit: int = 3900) -> str:
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def format_money(value: float, currency: str = "₽") -> str:
    amount = f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{amount} {currency}"


def format_datetime(value: str | datetime | None, include_timezone: bool = False) -> str:
    parsed = value if isinstance(value, datetime) else parse_datetime(value)
    if not parsed:
        return "—"
    suffix = " UTC" if include_timezone else ""
    return parsed.strftime("%d.%m.%Y %H:%M") + suffix


def remaining_time(value: str | datetime | None) -> str:
    parsed = value if isinstance(value, datetime) else parse_datetime(value)
    if not parsed:
        return "—"
    seconds = max(0, int((parsed - utcnow()).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days} дн. {hours} ч."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def progress_bar(current: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = max(0, min(width, round(width * current / total)))
    return "█" * filled + "░" * (width - filled)


def generate_promo_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def valid_url(value: str) -> bool:
    value = value.strip().lower()
    return value.startswith("https://") or value.startswith("http://") or value.startswith("tg://")


async def safe_edit_text(message: Any, text: str, **kwargs: Any) -> None:
    try:
        await message.edit_text(truncate(text), **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def csv_bytes(headers: list[str], rows: list[tuple[Any, ...]]) -> io.BytesIO:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    data = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return data


def xlsx_bytes(headers: list[str], rows: list[tuple[Any, ...]], sheet_name: str = "Отчёт") -> bytes:
    """Build a formatted XLSX report in memory."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name[:31]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="263445")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        sheet.append(list(row))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column_cells in sheet.columns:
        values = [str(cell.value or "") for cell in column_cells]
        width = min(max(max((len(value) for value in values), default=10) + 2, 12), 42)
        sheet.column_dimensions[column_cells[0].column_letter].width = width
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()

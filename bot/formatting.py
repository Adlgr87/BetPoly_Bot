"""Text, number and date formatting helpers shared by every handler.

Telegram messages are built with MarkdownV2, which means *every* dynamic value
coming from a user (prediction titles, option labels, usernames…) must be
escaped.  Doing this in one place removes a whole class of bugs (400 errors
and formatting injection).
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Optional

import config

NANO = 1_000_000_000  # nanoTON per TON


def esc(value: object) -> str:
    """Escape a dynamic value for Telegram's HTML parse mode.

    HTML only requires three entities, which makes user supplied titles, names
    and option labels safe by construction (no more broken messages, no more
    formatting injection).
    """
    return html.escape("" if value is None else str(value), quote=False)


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def link(text: object, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def points(value: int) -> str:
    """Format an integer amount of points with thousands separators."""
    return f"{int(value):,}".replace(",", ".")


def ton(nano: int) -> str:
    """Format nanoTON as a human friendly TON amount."""
    whole, frac = divmod(int(nano), NANO)
    frac_str = f"{frac:09d}".rstrip("0")
    if frac_str:
        return f"{whole}.{frac_str}"
    return str(whole)


def ton_with_unit(nano: int) -> str:
    return f"{ton(nano)} TON"


def tx_short(tx_hash: Optional[str], size: int = 8) -> str:
    if not tx_hash:
        return "—"
    return tx_hash[:size] + "…"


def tz() -> timezone:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(config.get_settings().timezone)
    except Exception:  # pragma: no cover - validated by config
        return timezone.utc


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz())


def parse_local_datetime(raw: str, fmt: str = "%d/%m/%Y %H:%M") -> datetime:
    """Parse a user supplied local date into an aware UTC datetime."""
    naive = datetime.strptime(raw.strip(), fmt)
    return naive.replace(tzinfo=tz()).astimezone(timezone.utc)


def fmt_local(value: datetime, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return to_local(value).strftime(fmt)


def fmt_ts(epoch_seconds: int, fmt: str = "%d/%m/%Y %H:%M") -> str:
    return fmt_local(datetime.fromtimestamp(int(epoch_seconds), tz=timezone.utc), fmt)


def iso_utc(value: datetime) -> str:
    """Canonical, lexicographically sortable timestamp used in SQLite."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def iso_now(offset_seconds: int = 0) -> str:
    """Current UTC timestamp; ``offset_seconds`` moves it into the future."""
    return iso_utc(now_utc() + timedelta(seconds=int(offset_seconds)))


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relative(epoch_seconds: int, now: Optional[datetime] = None) -> str:
    """Human friendly 'en 3 h' / 'hace 12 min' label."""
    now = now or now_utc()
    delta = int(epoch_seconds - now.timestamp())
    future = delta >= 0
    delta = abs(delta)
    if delta < 60:
        amount, unit = delta, "s"
    elif delta < 3600:
        amount, unit = delta // 60, "min"
    elif delta < 86400:
        amount, unit = delta // 3600, "h"
    else:
        amount, unit = delta // 86400, "d"
    return f"en {amount} {unit}" if future else f"hace {amount} {unit}"

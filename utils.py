"""NeonTiers Tournament Bot - Segédfüggvények és utilitások."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone


_CODE_ALPHABET = "".join(
    c for c in (string.ascii_uppercase + string.digits)
    if c not in {"0", "O", "1", "I", "L"}
)

_TS_PATTERNS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def generate_link_code(length: int = 6) -> str:
    """Biztonságos, félreolvasható karakterektől mentes kód generálása."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def parse_iso_datetime(ts_str: str) -> datetime | None:
    """ISO formátumú dátum beolvasása UTC időzónával."""
    if not ts_str:
        return None
    for pattern in _TS_PATTERNS:
        try:
            dt = datetime.strptime(ts_str, pattern)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def format_discord_timestamp(dt: datetime, style: str = "R") -> str:
    """Discord dinamikus időbélyeg formázása (pl. <t:12345678:R>)."""
    return f"<t:{int(dt.timestamp())}:{style}>"

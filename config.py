"""Configuration loader for NeonTiers Tournament Discord Bot.

Loads environment variables into a frozen :class:`Config` dataclass and fails
fast (raising ``RuntimeError``) when a required variable is missing or empty.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:
    # python-dotenv is optional at runtime — if it's installed we load .env
    # so local dev "just works" without polluting the process environment.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv may be absent in prod
    pass


def _required(name: str) -> str:
    """Return a required env var or raise ``RuntimeError`` if missing/empty."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is missing or empty. "
            "Set it in your Railway / .env configuration."
        )
    return value


def _optional_int(name: str, default: int) -> int:
    """Parse an optional integer env var, returning *default* when absent."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - explicit error path
        raise RuntimeError(
            f"Environment variable {name!r} must be an integer, got {raw!r}."
        ) from exc


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration for the bot."""

    # --- Required ---
    supabase_url: str
    supabase_anon_key: str
    discord_token: str
    client_id: int

    # --- Optional (with defaults) ---
    # GUILD_ID: 0 = sync globally & use first guild the bot is in.
    # REGULATOR_ROLE_ID / TICKET_CATEGORY_ID / RESULTS_CHANNEL_ID: 0 = use
    # per-tournament values passed to /tournamentqueue (recommended).
    guild_id: int = 0
    regulator_role_id: int = 0
    ticket_category_id: int = 0
    results_channel_id: int = 0
    pending_code_ttl_minutes: int = 30
    pending_code_length: int = 6
    auto_start_poll_seconds: int = 15

    @classmethod
    def from_env(cls) -> "Config":
        """Build a :class:`Config` from the current process environment."""
        return cls(
            supabase_url=_required("SUPABASE_URL"),
            supabase_anon_key=_required("SUPABASE_ANON_KEY"),
            discord_token=_required("DISCORD_TOKEN"),
            client_id=_optional_int("CLIENT_ID", 0),
            guild_id=_optional_int("GUILD_ID", 0),
            regulator_role_id=_optional_int("REGULATOR_ROLE_ID", 0),
            ticket_category_id=_optional_int("TICKET_CATEGORY_ID", 0),
            results_channel_id=_optional_int("RESULTS_CHANNEL_ID", 0),
            pending_code_ttl_minutes=_optional_int("PENDING_CODE_TTL_MINUTES", 30),
            pending_code_length=_optional_int("PENDING_CODE_LENGTH", 6),
            auto_start_poll_seconds=_optional_int("AUTO_START_POLL_SECONDS", 15),
        )


# Module-level singleton — importing this module triggers env validation.
config: Config = Config.from_env()

"""Supabase persistence layer for NeonTiers.

supabase-py is synchronous (it uses httpx in blocking mode), so every public
``Database`` method here is synchronous. Callers in the Discord layer MUST wrap
each call with ``await arun(db.method, ...)`` so the asyncio event loop is not
blocked.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, TypeVar

from supabase import Client, create_client

from config import config

log = logging.getLogger(__name__)

T = TypeVar("T")


def _utcnow() -> datetime:
    """Inline _utcnow (utils module no longer exists)."""
    return datetime.now(timezone.utc)


async def arun(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking callable in a worker thread and await the result.

    Usage::

        tournament = await arun(db.get_tournament, tournament_id)
    """
    return await asyncio.to_thread(func, *args, **kwargs)


def _to_int(value: Any) -> int:
    """Coerce JSON-decoded discord ids (which may arrive as str/float) to int."""
    if value is None:
        return 0
    return int(value)


class Database:
    """Synchronous Supabase wrapper.

    All methods are blocking; pair with :func:`arun` from async contexts.
    """

    def __init__(self) -> None:
        self._client: Client = create_client(
            config.supabase_url, config.supabase_anon_key
        )

    # ------------------------------------------------------------------
    # linked_accounts
    # ------------------------------------------------------------------

    def get_linked_account(self, discord_id: int) -> Optional[dict]:
        """Return the linked-account row for *discord_id* or ``None``."""
        resp = (
            self._client.table("linked_accounts")
            .select("*")
            .eq("discord_id", discord_id)
            .maybe_single()
            .execute()
        )
        if not resp or not resp.data:
            return None
        return resp.data

    # ------------------------------------------------------------------
    # pending_codes
    # ------------------------------------------------------------------

    def create_pending_code(
        self, discord_id: int, code: str, ttl_minutes: int
    ) -> dict:
        """Insert a fresh pending linking code for *discord_id*.

        The ``pending_codes`` table uses ``timestamptz`` columns (per the
        original spec), so we send ISO-8601 strings — NOT Unix epoch ints.

        Raises ``RuntimeError`` if the insert returned no rows — this usually
        means RLS is blocking anon writes to ``pending_codes``.
        """
        now = _utcnow()
        expires_at = now + timedelta(minutes=ttl_minutes)
        payload = {
            "id": str(uuid.uuid4()),
            "discord_id": discord_id,
            "code": code,
            # ISO-8601 strings — pending_codes columns are timestamptz.
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "used": False,
        }
        resp = (
            self._client.table("pending_codes")
            .insert(payload)
            .execute()
        )
        if not resp.data:
            raise RuntimeError(
                "pending_codes INSERT returned no rows — check RLS policies "
                "(anon must have INSERT access)."
            )
        return resp.data[0]

    def is_pending_code_valid(self, code: str) -> bool:
        """Return True iff *code* exists, is unused, and has not expired."""
        # pending_codes.expires_at is timestamptz — compare against ISO-8601.
        now_iso = _utcnow().isoformat()
        resp = (
            self._client.table("pending_codes")
            .select("id", count="exact")
            .eq("code", code)
            .eq("used", False)
            .gt("expires_at", now_iso)
            .maybe_single()
            .execute()
        )
        return bool(resp and resp.data)

    # ------------------------------------------------------------------
    # tournaments
    # ------------------------------------------------------------------

    def create_tournament(
        self,
        name: str,
        end_time,
        queue_message_id: int,
        guild_id: int,
        tournament_id: Optional[str] = None,
        regulator_role_id: int = 0,
        results_channel_id: int = 0,
        ticket_category_id: int = 0,
        ft: int = 1,
    ) -> dict:
        """Insert a new tournament row in ``queued`` state.

        Raises ``RuntimeError`` if the insert returned no rows (RLS issue).
        """
        tid = tournament_id or str(uuid.uuid4())
        # Always send Unix epoch seconds for end_time — works for both bigint
        # and timestamptz columns. (User's schema uses bigint.)
        if hasattr(end_time, "timestamp"):
            end_value = int(end_time.timestamp())
        elif isinstance(end_time, (int, float)) and not isinstance(end_time, bool):
            end_value = int(end_time)
        else:
            # ISO string or similar — parse it.
            from utils import parse_timestamp as _parse
            end_value = int(_parse(end_time).timestamp())
        payload = {
            "id": tid,
            "name": name,
            "end_time": end_value,
            "queue_message_id": queue_message_id,
            "status": "queued",
            "guild_id": guild_id,
            "current_round": 0,
            "players": [],
            "regulator_role_id": regulator_role_id,
            "results_channel_id": results_channel_id,
            "ticket_category_id": ticket_category_id,
            "ft": ft,
            "posted_at": int(_utcnow().timestamp()),
        }
        log.info("create_tournament payload=%s", payload)
        try:
            resp = (
                self._client.table("tournaments")
                .insert(payload)
                .execute()
            )
        except Exception as exc:
            # supabase-py raises postgrest APIError (or httpx errors) for
            # RLS / schema / network failures. Re-raise with full context.
            log.error(
                "create_tournament INSERT raised %s: %s | payload=%s",
                type(exc).__name__, exc, payload,
            )
            raise RuntimeError(
                f"tournaments INSERT failed: {type(exc).__name__}: {exc}"
            ) from exc
        log.info("create_tournament resp.data=%s", resp.data)
        if not resp.data:
            raise RuntimeError(
                "tournaments INSERT returned no rows — this means RLS is "
                "ENABLED but the anon policy is missing/blocked. "
                "Run supabase_setup.sql in the Supabase SQL editor."
            )
        return resp.data[0]

    def get_tournament(self, tournament_id: str) -> Optional[dict]:
        """Return a tournament row by id, or ``None`` if not found."""
        resp = (
            self._client.table("tournaments")
            .select("*")
            .eq("id", tournament_id)
            .maybe_single()
            .execute()
        )
        if not resp or not resp.data:
            return None
        return resp.data

    def list_pending_tournaments(self) -> list[dict]:
        """Return queued tournaments whose end_time has passed (auto-start feed)."""
        now_unix = int(_utcnow().timestamp())
        resp = (
            self._client.table("tournaments")
            .select("*")
            .eq("status", "queued")
            .lte("end_time", now_unix)
            .execute()
        )
        return list(resp.data or [])

    def list_active_tournaments(self) -> list[dict]:
        """Return queued+running tournaments for view rehydration (newest first)."""
        resp = (
            self._client.table("tournaments")
            .select("*")
            .in_("status", ("queued", "running"))
            .order("end_time", desc=True)
            .execute()
        )
        return list(resp.data or [])

    def update_tournament(self, tournament_id: str, **fields: Any) -> None:
        """Update arbitrary fields on a tournament row by id."""
        if not fields:
            return
        resp = (
            self._client.table("tournaments")
            .update(fields)
            .eq("id", tournament_id)
            .execute()
        )
        if not resp.data:
            log.warning(
                "update_tournament(id=%s, fields=%s) returned no rows.",
                tournament_id,
                list(fields.keys()),
            )

    def claim_for_round(self, tournament_id: str, round_number: int) -> bool:
        """Atomically claim a tournament for *round_number*.

        Two-phase conditional UPDATE prevents double-starts when the auto-start
        background loop and a manual ``/tournamentround`` call race:

        1. If the tournament is still ``queued``, transition it to ``running``
           and set ``current_round``.
        2. Otherwise, if it is already ``running`` but on a *different* round,
           just bump ``current_round`` (admin override of the round number).

        Returns ``True`` iff one of the two updates affected a row.
        """
        # Phase 1: queued → running
        resp1 = (
            self._client.table("tournaments")
            .update({"status": "running", "current_round": round_number})
            .eq("id", tournament_id)
            .eq("status", "queued")
            .execute()
        )
        if resp1.data:
            log.info(
                "claim_for_round: tournament %s queued→running round %d",
                tournament_id,
                round_number,
            )
            return True

        # Phase 2: already running, but on a different round → renumber
        resp2 = (
            self._client.table("tournaments")
            .update({"current_round": round_number})
            .eq("id", tournament_id)
            .eq("status", "running")
            .neq("current_round", round_number)
            .execute()
        )
        if resp2.data:
            log.info(
                "claim_for_round: tournament %s renumbered to round %d",
                tournament_id,
                round_number,
            )
            return True

        log.info(
            "claim_for_round: tournament %s already on round %d — no-op",
            tournament_id,
            round_number,
        )
        return False

    def delete_tournament(self, tournament_id: str) -> None:
        """Delete a tournament row (and its matches, via ON DELETE CASCADE)."""
        resp = (
            self._client.table("tournaments")
            .delete()
            .eq("id", tournament_id)
            .execute()
        )
        if not resp.data:
            log.warning(
                "delete_tournament(id=%s) returned no rows.", tournament_id
            )

    # ------------------------------------------------------------------
    # tournaments.players (JSONB array)
    # ------------------------------------------------------------------

    def get_tournament_players(self, tournament_id: str) -> list[dict]:
        """Return the players JSONB array, with discord_id coerced to int."""
        resp = (
            self._client.table("tournaments")
            .select("players")
            .eq("id", tournament_id)
            .maybe_single()
            .execute()
        )
        if not resp or not resp.data:
            return []
        players = resp.data.get("players") or []
        normalized: list[dict] = []
        for entry in players:
            if not isinstance(entry, dict):
                continue
            normalized.append(
                {
                    "discord_id": _to_int(entry.get("discord_id")),
                    "minecraft_name": str(entry.get("minecraft_name", "")),
                }
            )
        return normalized

    def add_player_to_tournament(
        self, tournament_id: str, discord_id: int, minecraft_name: str
    ) -> bool:
        """Add a player to the roster. Returns ``False`` if already joined.

        Caller MUST hold a per-tournament asyncio.Lock to avoid lost-write races
        when two joins land concurrently.
        """
        players = self.get_tournament_players(tournament_id)
        for p in players:
            if _to_int(p.get("discord_id")) == discord_id:
                return False
        players.append(
            {"discord_id": discord_id, "minecraft_name": minecraft_name}
        )
        self.update_tournament(tournament_id, players=players)
        return True

    def remove_player_from_tournament(
        self, tournament_id: str, discord_id: int
    ) -> bool:
        """Remove a player from the roster. Returns ``False`` if not on roster."""
        players = self.get_tournament_players(tournament_id)
        new_players = [
            p for p in players if _to_int(p.get("discord_id")) != discord_id
        ]
        if len(new_players) == len(players):
            return False
        self.update_tournament(tournament_id, players=new_players)
        return True

    # ------------------------------------------------------------------
    # matches
    # ------------------------------------------------------------------

    def create_match(
        self,
        tournament_id: str,
        round_number: int,
        player1_discord_id: int,
        player2_discord_id: int,
        player1_mc: str,
        player2_mc: str,
        ticket_channel_id: int,
        deadline: Optional[int] = None,
    ) -> dict:
        """Insert a match row. Raises ``RuntimeError`` if RLS blocks the insert."""
        payload = {
            "id": str(uuid.uuid4()),
            "tournament_id": tournament_id,
            "round_number": round_number,
            "player1_discord_id": player1_discord_id,
            "player2_discord_id": player2_discord_id,
            "player1_mc": player1_mc,
            "player2_mc": player2_mc,
            "ticket_channel_id": ticket_channel_id,
            "ticket_message_id": 0,
            "winner_discord_id": None,
            "deadline": deadline,
            "score1": None,
            "score2": None,
            "ff": False,
        }
        resp = self._client.table("matches").insert(payload).execute()
        if not resp.data:
            raise RuntimeError(
                "matches INSERT returned no rows — check RLS policies "
                "(anon must have INSERT access)."
            )
        return resp.data[0]

    def set_match_ticket_message(self, match_id: str, message_id: int) -> None:
        """Record the ticket embed's message id, needed to rehydrate the view."""
        self._client.table("matches").update(
            {"ticket_message_id": message_id}
        ).eq("id", match_id).execute()

    def get_match_by_ticket(self, ticket_channel_id: int) -> Optional[dict]:
        """Return a match joined with its tournament name, or ``None``."""
        resp = (
            self._client.table("matches")
            .select("*, tournaments(name)")
            .eq("ticket_channel_id", ticket_channel_id)
            .maybe_single()
            .execute()
        )
        if not resp or not resp.data:
            return None
        return resp.data

    def get_match(self, match_id: str) -> Optional[dict]:
        """Return a single match row by id, or ``None``."""
        resp = (
            self._client.table("matches")
            .select("*")
            .eq("id", match_id)
            .maybe_single()
            .execute()
        )
        if not resp or not resp.data:
            return None
        return resp.data

    def set_match_winner(
        self,
        match_id: str,
        winner_discord_id: int,
        score1: Optional[int] = None,
        score2: Optional[int] = None,
        ff: bool = False,
    ) -> None:
        """Record the winner (and optional score/FF flag) of a match."""
        fields: dict[str, Any] = {"winner_discord_id": winner_discord_id, "ff": ff}
        if score1 is not None:
            fields["score1"] = score1
        if score2 is not None:
            fields["score2"] = score2
        resp = (
            self._client.table("matches")
            .update(fields)
            .eq("id", match_id)
            .execute()
        )
        if not resp.data:
            log.warning(
                "set_match_winner(id=%s, winner=%s) returned no rows.",
                match_id,
                winner_discord_id,
            )

    def get_unresolved_matches(self, tournament_id: str) -> list[dict]:
        """Return matches for *tournament_id* with no winner (view rehydration)."""
        resp = (
            self._client.table("matches")
            .select("*")
            .eq("tournament_id", tournament_id)
            .is_("winner_discord_id", "null")
            .execute()
        )
        return list(resp.data or [])

    def get_all_unresolved_matches(self) -> list[dict]:
        """Return every open match across all tournaments (restart rehydration)."""
        resp = (
            self._client.table("matches")
            .select("*")
            .is_("winner_discord_id", "null")
            .execute()
        )
        return list(resp.data or [])


# Module singleton.
db = Database()

"""NeonTiers Tournament Bot - Bajnokság Cogs modul."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import config
from database import arun, db
from embeds import build_queue_embed, build_ticket_embed, build_result_broadcast_text
from views import MatchTicketView, TournamentQueueView

log = logging.getLogger("neontiers.tournaments")

MATCH_DEADLINE_HOURS = 24


def _to_int(val) -> int:
    try:
        return int(val) if val is not None else 0
    except (ValueError, TypeError):
        return 0


class TournamentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # per-tournament lock so two rounds can't be auto-started concurrently
        self._round_locks: dict[str, asyncio.Lock] = {}
        self.auto_start_loop.start()
        self.auto_close_inactive_loop.start()

    def cog_unload(self):
        self.auto_start_loop.cancel()
        self.auto_close_inactive_loop.cancel()

    def _lock_for(self, tournament_id: str) -> asyncio.Lock:
        lock = self._round_locks.get(tournament_id)
        if lock is None:
            lock = asyncio.Lock()
            self._round_locks[tournament_id] = lock
        return lock

    # ==========================================
    # ÚJRAINDÍTÁS UTÁNI ÁLLAPOT VISSZAÁLLÍTÁS
    # ==========================================

    async def rehydrate_views(self) -> None:
        """Minden aktív (queued/running) bajnoksághoz és nyitott meccshez
        újraregisztrálja a perzisztens view-kat, hogy bot-restart után is
        működjenek a gombok."""
        try:
            tournaments = await arun(db.list_active_tournaments)
        except Exception as exc:
            log.error("Nem sikerült lekérni az aktív bajnokságokat rehydration közben: %s", exc)
            tournaments = []

        for t in tournaments:
            queue_message_id = _to_int(t.get("queue_message_id"))
            if not queue_message_id:
                continue
            view = TournamentQueueView(t["id"])
            self.bot.add_view(view, message_id=queue_message_id)

        try:
            matches = await arun(db.get_all_unresolved_matches)
        except Exception as exc:
            log.error("Nem sikerült lekérni a nyitott meccseket rehydration közben: %s", exc)
            matches = []

        tourney_cache: dict[str, dict] = {t["id"]: t for t in tournaments}
        for m in matches:
            ticket_message_id = _to_int(m.get("ticket_message_id"))
            if not ticket_message_id:
                continue
            tourney_id = m.get("tournament_id")
            tourney = tourney_cache.get(tourney_id)
            if not tourney:
                tourney = await arun(db.get_tournament, tourney_id)
                if tourney:
                    tourney_cache[tourney_id] = tourney
            if not tourney:
                continue
            view = MatchTicketView(m, tourney)
            self.bot.add_view(view, message_id=ticket_message_id)

        log.info(
            "Rehydration kész: %d bajnokság queue view, %d meccs ticket view regisztrálva.",
            len(tournaments),
            sum(1 for m in matches if _to_int(m.get("ticket_message_id"))),
        )

    # ==========================================
    # HÁTTÉRFELADATOK (LOOPS)
    # ==========================================

    @tasks.loop(seconds=config.auto_start_poll_seconds)
    async def auto_start_loop(self):
        """Háttérfeladat: Automatikus indítás vizsgálata."""
        try:
            pending = await arun(db.list_pending_tournaments)
            for tourney in pending:
                await self._start_round_logic(tourney, round_num=1)
        except Exception as exc:
            log.error("Hiba az auto_start_loop futása közben: %s", exc)

    @auto_start_loop.before_loop
    async def _before_auto_start(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def auto_close_inactive_loop(self):
        """Háttérfeladat: 24 órája inaktív meccsek automatikus 0-0 FF lezárása."""
        try:
            running_tourneys = await arun(
                lambda: db._client.table("tournaments").select("*").eq("status", "running").execute().data or []
            )
            for t in running_tourneys:
                unresolved = await arun(db.get_unresolved_matches, t["id"])
                for match in unresolved:
                    ticket_id = _to_int(match.get("ticket_channel_id"))
                    if not ticket_id:
                        continue

                    channel = self.bot.get_channel(ticket_id)
                    if isinstance(channel, discord.TextChannel):
                        last_human_msg = None
                        async for msg in channel.history(limit=50):
                            if not msg.author.bot:
                                last_human_msg = msg
                                break

                        now = datetime.now(timezone.utc)
                        cutoff = now - timedelta(hours=MATCH_DEADLINE_HOURS)

                        ref_time = last_human_msg.created_at if last_human_msg else channel.created_at

                        if ref_time < cutoff:
                            log.info("Meccs inaktivitás miatt lezárva (24h+): match_id=%s", match["id"])
                            embed = discord.Embed(
                                title="⏰ Automatikus Lezárás (Inaktivitás)",
                                description=(
                                    "Mivel 24 órája nem érkezett emberi üzenet a csatornában, a meccs "
                                    "**0 - 0** eredménnyel zárult, és mindkét játékos **FF (Forfeit)** "
                                    "státuszt kapott."
                                ),
                                color=discord.Color.red(),
                            )
                            await channel.send(embed=embed)
                            await arun(db.set_match_winner, match["id"], 0, 0, 0, True)
                            await asyncio.sleep(5)
                            try:
                                await channel.delete(reason="24 órás inaktivitás miatti automatikus törlés")
                            except discord.HTTPException:
                                pass
                            await self._maybe_advance_round(t)
        except Exception as exc:
            log.error("Hiba az auto_close_inactive_loop futása közben: %s", exc)

    @auto_close_inactive_loop.before_loop
    async def _before_auto_close(self):
        await self.bot.wait_until_ready()

    # ==========================================
    # SEGÉDFÜGGVÉNYEK
    # ==========================================

    async def _get_member_safe(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """Biztonságos tag lekérés (Cache / Discord API fallback)."""
        if not guild or not user_id:
            return None
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        return member

    async def _get_player_info(self, discord_id: int) -> tuple[int, str]:
        """Közvetlenül a linked_accounts táblából kérdezi le a játékos adatait."""
        linked = await arun(db.get_linked_account, discord_id)
        if linked and linked.get("minecraft_name"):
            return discord_id, linked["minecraft_name"]
        return discord_id, f"Player_{discord_id}"

    async def _get_prev_round_winners(self, tournament_id: str, round_num: int) -> list[dict]:
        """Lekéri az előző forduló győzteseinek adatait."""
        def _fetch():
            resp = db._client.table("matches").select("*").eq("tournament_id", tournament_id).eq("round_number", round_num - 1).execute()
            if not resp or not resp.data:
                return []

            winners = []
            for match in resp.data:
                w_id = _to_int(match.get("winner_discord_id"))
                if not w_id or w_id == 0:
                    continue

                p1_id = _to_int(match.get("player1_discord_id"))
                mc_name = match.get("player1_mc") if w_id == p1_id else match.get("player2_mc")

                winners.append({"discord_id": w_id, "minecraft_name": mc_name})
            return winners

        return await arun(_fetch)

    async def _update_queue_message(self, tourney: dict) -> None:
        """A queue embedet frissíti (állapot, forduló, stb.) minden forduló indulásakor."""
        queue_message_id = _to_int(tourney.get("queue_message_id"))
        if not queue_message_id:
            return
        guild = self.bot.get_guild(tourney.get("guild_id") or config.guild_id)
        if not guild:
            return
        # Meg kell találni a csatornát, ahol a queue üzenet van. Mivel nincs
        # elmentve a channel_id, végigmegyünk a szöveges csatornákon, amíg
        # meg nem találjuk (cache-elt fetch_message olcsó a legtöbb esetben).
        message = None
        for channel in guild.text_channels:
            try:
                message = await channel.fetch_message(queue_message_id)
                break
            except (discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException:
                continue
        if not message:
            return
        players = await arun(db.get_tournament_players, tourney["id"])
        embed, _ = build_queue_embed(tourney, players, page=0)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Nem sikerült frissíteni a queue üzenetet: %s", exc)

    async def _maybe_advance_round(self, tourney: dict) -> None:
        """Ha egy bajnokság aktuális fordulójának minden meccse lezárult,
        automatikusan elindítja a következő fordulót (vagy lezárja a
        bajnokságot, ha már csak egy győztes maradt)."""
        tourney_id = tourney.get("id")
        if not tourney_id:
            return

        async with self._lock_for(tourney_id):
            fresh = await arun(db.get_tournament, tourney_id)
            if not fresh or fresh.get("status") != "running":
                return

            current_round = _to_int(fresh.get("current_round")) or 1
            unresolved = await arun(db.get_unresolved_matches, tourney_id)
            unresolved_current = [m for m in unresolved if _to_int(m.get("round_number")) == current_round]
            if unresolved_current:
                return  # még van nyitott meccs ebben a fordulóban

            msg = await self._start_round_logic(fresh, round_num=current_round + 1)
            log.info("Automatikus forduló indítás (%s): %s", tourney_id, msg)

    async def _start_round_logic(self, tourney: dict, round_num: int = 1) -> str:
        tourney_id = tourney["id"]
        tourney_name = tourney.get("name", "Bajnokság")

        if round_num == 1:
            raw_players = await arun(db.get_tournament_players, tourney_id)
            players = []
            for p in raw_players:
                d_id = _to_int(p.get("discord_id"))
                if d_id > 0:
                    _, mc_name = await self._get_player_info(d_id)
                    players.append({"discord_id": d_id, "minecraft_name": mc_name})
        else:
            unresolved = await arun(db.get_unresolved_matches, tourney_id)
            unresolved_prev = [m for m in unresolved if _to_int(m.get("round_number")) == round_num - 1]
            if unresolved_prev:
                return f"⚠️ Még {len(unresolved_prev)} meccs nincs lezárva ebben a fordulóban! Előbb rögzítsétek az eredményeket."

            players = await self._get_prev_round_winners(tourney_id, round_num)

        if len(players) < 2:
            if round_num == 1:
                await arun(db.update_tournament, tourney_id, status="cancelled")
                await self._update_queue_message({**tourney, "status": "cancelled"})
                return "❌ A bajnokság törölve lett, mert nincs elég regisztrált játékos (min. 2 fő)."
            else:
                await arun(db.update_tournament, tourney_id, status="completed")
                updated_tourney = {**tourney, "status": "completed", "current_round": round_num - 1}
                await self._update_queue_message(updated_tourney)
                if len(players) == 1:
                    w = players[0]
                    win_text = f"🏆 **A tournament győztese:** <@{w['discord_id']}> (`{w['minecraft_name']}`)"
                    results_channel_id = tourney.get("results_channel_id") or config.results_channel_id
                    guild = self.bot.get_guild(tourney.get("guild_id") or config.guild_id)
                    if guild and results_channel_id:
                        ch = guild.get_channel(results_channel_id)
                        if isinstance(ch, discord.TextChannel):
                            await ch.send(win_text)
                    return win_text
                return "❌ Nincs elég győztes a következő forduló elindításához."

        shuffled = players.copy()
        random.shuffle(shuffled)

        await arun(db.update_tournament, tourney_id, status="running", current_round=round_num)
        tourney = {**tourney, "status": "running", "current_round": round_num}

        guild = self.bot.get_guild(tourney.get("guild_id") or config.guild_id)
        category_id = tourney.get("ticket_category_id") or config.ticket_category_id
        category = guild.get_channel(category_id) if guild and category_id else None

        deadline_ts = int((datetime.now(timezone.utc) + timedelta(hours=MATCH_DEADLINE_HOURS)).timestamp())

        created_matches = 0
        for i in range(0, len(shuffled) - 1, 2):
            p1 = shuffled[i]
            p2 = shuffled[i + 1]

            p1_id = _to_int(p1["discord_id"])
            p2_id = _to_int(p2["discord_id"])

            u1 = await self._get_member_safe(guild, p1_id)
            u2 = await self._get_member_safe(guild, p2_id)

            p1_mc = p1.get("minecraft_name") or (u1.display_name if u1 else f"Player_{p1_id}")
            p2_mc = p2.get("minecraft_name") or (u2.display_name if u2 else f"Player_{p2_id}")

            ticket_channel = None
            if guild and isinstance(category, discord.CategoryChannel):
                if len(category.channels) < 50:
                    try:
                        overwrites = {
                            guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
                        }

                        if u1:
                            overwrites[u1] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                        if u2:
                            overwrites[u2] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                        clean_p1 = "".join(c for c in p1_mc if c.isalnum() or c in "-_")
                        clean_p2 = "".join(c for c in p2_mc if c.isalnum() or c in "-_")

                        ticket_channel = await category.create_text_channel(
                            name=f"r{round_num}-{clean_p1[:8]}-vs-{clean_p2[:8]}",
                            overwrites=overwrites,
                        )
                    except discord.HTTPException as exc:
                        log.error("Discord API Hiba a csatorna létrehozásakor: %s", exc)

            match_data = await arun(
                db.create_match,
                tournament_id=tourney_id,
                round_number=round_num,
                player1_discord_id=p1_id,
                player2_discord_id=p2_id,
                player1_mc=p1_mc,
                player2_mc=p2_mc,
                ticket_channel_id=ticket_channel.id if ticket_channel else 0,
                deadline=deadline_ts,
            )

            created_matches += 1

            if ticket_channel:
                content_text = f"<@{p1_id}> <@{p2_id}> elindult a meccsetek. Pingeljétek egymást is, hogy minél gyorsabban le tudjátok játszani."
                embed = build_ticket_embed(tourney, match_data)

                sent = await ticket_channel.send(
                    content=content_text,
                    embed=embed,
                    view=MatchTicketView(match_data, tourney),
                )
                await arun(db.set_match_ticket_message, match_data["id"], sent.id)

        await self._update_queue_message(tourney)

        return f"✅ A(z) **{round_num}. forduló** elindult! ({created_matches} meccs/ticket létrehozva)."

    # ==========================================
    # SLASH PARANCSOK
    # ==========================================

    @app_commands.command(name="tournamentqueue", description="Új bajnokság regisztráció nyitása.")
    @app_commands.describe(
        name="A bajnokság neve",
        minutes="Jelentkezési idő percben",
        ft="Hány győzelemig tart egy meccs (Bo szám, alapértelmezett 1)",
    )
    async def tournamentqueue(self, interaction: discord.Interaction, name: str, minutes: int, ft: int = 1):
        try:
            await interaction.response.defer()
            end_time = discord.utils.utcnow() + timedelta(minutes=minutes)

            tourney_data = await arun(
                db.create_tournament,
                name=name,
                end_time=end_time,
                queue_message_id=0,
                guild_id=interaction.guild_id or config.guild_id,
                ticket_category_id=config.ticket_category_id,
                results_channel_id=config.results_channel_id,
                regulator_role_id=config.regulator_role_id,
                ft=ft,
            )
            tourney_id = tourney_data["id"]

            view = TournamentQueueView(tourney_id)
            embed, _ = build_queue_embed(tourney_data, [], page=0)
            msg = await interaction.followup.send(embed=embed, view=view)
            await arun(db.update_tournament, tourney_id, queue_message_id=msg.id)
            self.bot.add_view(view, message_id=msg.id)
        except Exception as exc:
            log.error("Hiba a tournamentqueue parancsnál: %s", exc)
            await interaction.followup.send(f"❌ Hiba történt: `{exc}`", ephemeral=True)

    @app_commands.command(name="tournamentqueuepost", description="Egy bajnokság queue embedjének újraküldése (ha elromlott/eltűnt).")
    @app_commands.describe(tournament_id="A bajnokság UUID-ja")
    async def tournamentqueuepost(self, interaction: discord.Interaction, tournament_id: str):
        try:
            await interaction.response.defer()

            tourney = await arun(db.get_tournament, tournament_id)
            if not tourney:
                await interaction.followup.send("❌ Nem található bajnokság ezzel az ID-val!", ephemeral=True)
                return

            players = await arun(db.get_tournament_players, tournament_id)
            view = TournamentQueueView(tournament_id)
            embed, _ = build_queue_embed(tourney, players, page=0)

            msg = await interaction.followup.send(embed=embed, view=view)
            await arun(db.update_tournament, tournament_id, queue_message_id=msg.id)
            self.bot.add_view(view, message_id=msg.id)

            log.info("Queue embed újraposztolva: tournament_id=%s új message_id=%s", tournament_id, msg.id)
        except Exception as exc:
            log.error("Hiba a tournamentqueuepost parancsnál: %s", exc)
            await interaction.followup.send(f"❌ Hiba történt: `{exc}`", ephemeral=True)

    @app_commands.command(name="tournamentresultpost", description="Egy meccs eredmény-üzenetének újraküldése az eredmény csatornába (nem válaszként).")
    @app_commands.describe(match_id="A meccs UUID-ja")
    async def tournamentresultpost(self, interaction: discord.Interaction, match_id: str):
        try:
            await interaction.response.defer(ephemeral=True)

            match = await arun(db.get_match, match_id)
            if not match:
                await interaction.followup.send("❌ Nem található meccs ezzel az ID-val!", ephemeral=True)
                return

            tourney = await arun(db.get_tournament, match.get("tournament_id"))
            if not tourney:
                await interaction.followup.send("❌ Nem található a meccshez tartozó bajnokság!", ephemeral=True)
                return

            text = build_result_broadcast_text(tourney, match)
            if text is None:
                await interaction.followup.send("❌ Ennek a meccsnek még nincs rögzített eredménye.", ephemeral=True)
                return

            results_channel_id = tourney.get("results_channel_id") or config.results_channel_id
            guild = self.bot.get_guild(tourney.get("guild_id") or config.guild_id)
            channel = guild.get_channel(results_channel_id) if guild and results_channel_id else None
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send("❌ Nem található az eredmény csatorna!", ephemeral=True)
                return

            # Fontos: sima send(), NEM reply/mention — önálló üzenetként megy ki.
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True))

            await interaction.followup.send(f"✅ Eredmény újraposztolva ide: {channel.mention}", ephemeral=True)
        except Exception as exc:
            log.error("Hiba a tournamentresultpost parancsnál: %s", exc)
            await interaction.followup.send(f"❌ Hiba történt: `{exc}`", ephemeral=True)

    @app_commands.command(name="tournamentround", description="Forduló kézi indítása vagy kezelése.")
    @app_commands.choices(action=[
        app_commands.Choice(name="start", value="start"),
        app_commands.Choice(name="close", value="close"),
    ])
    async def tournamentround(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        tournament_id: str,
        round_number: int,
    ):
        try:
            await interaction.response.defer(ephemeral=True)

            tourney = await arun(db.get_tournament, tournament_id)
            if not tourney:
                await interaction.followup.send("❌ Nem található bajnokság ezzel az ID-val!", ephemeral=True)
                return

            if action.value == "start":
                msg = await self._start_round_logic(tourney, round_num=round_number)
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.followup.send("ℹ️ Forduló lezárva.", ephemeral=True)
        except Exception as exc:
            log.error("Hiba a tournamentround parancsnál: %s", exc)
            await interaction.followup.send(f"❌ Hiba történt: `{exc}`", ephemeral=True)


async def maybe_advance_round(bot: commands.Bot, tourney: dict) -> None:
    """Modul-szintű belépési pont, amit a views.py hív meg (kör/körkörös
    import elkerülése végett), miután egy meccs eredménye rögzítésre került."""
    cog = bot.get_cog("TournamentsCog")
    if cog is None:
        log.warning("TournamentsCog nincs betöltve, nem tudom továbbléptetni a fordulót.")
        return
    await cog._maybe_advance_round(tourney)


async def setup(bot: commands.Bot):
    await bot.add_cog(TournamentsCog(bot))

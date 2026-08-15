"""NeonTiers Tournament Bot - Discord Interaktív UI elemek."""

from __future__ import annotations

import asyncio
import logging
import discord
from discord import ui

from config import config
from database import arun, db
from embeds import build_queue_embed, build_ticket_embed
from utils import generate_link_code

log = logging.getLogger("neontiers.views")


# ==========================================
# TOURNAMENT QUEUE (jelentkezési) VIEW
# ==========================================


class TournamentQueueView(ui.View):
    """Perzisztens view a bajnokság jelentkezési embedhez.

    Ugyanaz a view-osztály minden bajnoksághoz újra használatos: a
    tournament_id-t a konstruktorban kapja meg, a bot pedig induláskor
    (rehydration) minden aktív bajnoksághoz külön példányt regisztrál a
    saját queue_message_id-jéhez kötve (lásd cogs/tournaments.py
    ``rehydrate_views``).
    """

    def __init__(self, tournament_id: str, page: int = 0):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        self.page = page

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        tourney = await arun(db.get_tournament, self.tournament_id)
        if not tourney:
            return
        players = await arun(db.get_tournament_players, self.tournament_id)
        embed, clamped_page = build_queue_embed(tourney, players, self.page)
        self.page = clamped_page
        try:
            await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException as exc:
            log.warning("Nem sikerült frissíteni a queue üzenetet: %s", exc)

    @ui.button(label="Belépés", style=discord.ButtonStyle.success, custom_id="tournament_join")
    async def join_tournament(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        tourney = await arun(db.get_tournament, self.tournament_id)
        if not tourney or tourney.get("status") != "queued":
            await interaction.followup.send(
                "❌ Ez a bajnokság már nem fogad jelentkezéseket.", ephemeral=True
            )
            return

        linked = await arun(db.get_linked_account, user_id)
        if not linked:
            code = generate_link_code()
            await arun(db.create_pending_code, user_id, code, config.pending_code_ttl_minutes)
            await interaction.followup.send(
                f"❌ A fiókod nincs összekapcsolva!\n\n"
                f"Lépj fel a Minecraft szerverre (`chaosffa.kinetic.host`), és írd be ezt a parancsot:\n"
                f"```/link {code}```",
                ephemeral=True,
            )
            return

        mc_name = linked.get("minecraft_name", "Ismeretlen")
        added = await arun(db.add_player_to_tournament, self.tournament_id, user_id, mc_name)
        if added:
            await interaction.followup.send("✅ Sikeresen regisztráltál a bajnokságra!", ephemeral=True)
            await self._refresh_message(interaction)
        else:
            await interaction.followup.send("⚠️ Már regisztráltál erre a bajnokságra!", ephemeral=True)

    @ui.button(label="Kilépés", style=discord.ButtonStyle.danger, custom_id="tournament_leave")
    async def leave_tournament(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        removed = await arun(db.remove_player_from_tournament, self.tournament_id, interaction.user.id)
        if removed:
            await interaction.followup.send("✅ Sikeresen kiléptél a bajnokságból.", ephemeral=True)
            await self._refresh_message(interaction)
        else:
            await interaction.followup.send("⚠️ Nem voltál regisztrálva erre a bajnokságra.", ephemeral=True)

    @ui.button(label="Következő oldal", style=discord.ButtonStyle.secondary, custom_id="tournament_next_page")
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        tourney = await arun(db.get_tournament, self.tournament_id)
        if not tourney:
            await interaction.followup.send("❌ Ez a bajnokság már nem létezik.", ephemeral=True)
            return
        players = await arun(db.get_tournament_players, self.tournament_id)

        total = len(players)
        total_pages = max(1, (total + 10 - 1) // 10)
        next_page_num = (self.page + 1) % total_pages

        embed, clamped_page = build_queue_embed(tourney, players, next_page_num)
        pager = QueueEphemeralPagerView(self.tournament_id, clamped_page)
        await interaction.followup.send(embed=embed, view=pager, ephemeral=True)


class QueueEphemeralPagerView(ui.View):
    """Csak a rányomó felhasználó ephemeral üzenetében élő lapozó — Előző/Következő
    oldal gombokkal, a közös (mindenki által látott) queue embedet nem érinti."""

    def __init__(self, tournament_id: str, page: int = 0):
        super().__init__(timeout=180)
        self.tournament_id = tournament_id
        self.page = page

    async def _render(self, interaction: discord.Interaction, delta: int) -> None:
        tourney = await arun(db.get_tournament, self.tournament_id)
        if not tourney:
            await interaction.response.edit_message(content="❌ Ez a bajnokság már nem létezik.", embed=None, view=None)
            return
        players = await arun(db.get_tournament_players, self.tournament_id)

        total = len(players)
        total_pages = max(1, (total + 10 - 1) // 10)
        self.page = (self.page + delta) % total_pages

        embed, clamped_page = build_queue_embed(tourney, players, self.page)
        self.page = clamped_page
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="◀ Előző oldal", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._render(interaction, -1)

    @ui.button(label="Következő oldal ▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._render(interaction, 1)


# ==========================================
# MATCH TICKET VIEW
# ==========================================


class ResultModal(ui.Modal, title="Meccs Eredmény Beírása"):
    def __init__(self, match_data: dict, tournament_data: dict):
        super().__init__()
        self.match_data = match_data
        self.tournament_data = tournament_data

        p1_mc = match_data.get("player1_mc") or "Player1"
        p2_mc = match_data.get("player2_mc") or "Player2"

        self.score1_input = ui.TextInput(
            label=f"{p1_mc} eredménye",
            placeholder="Pl. 2",
            required=True,
            max_length=3,
        )
        self.score2_input = ui.TextInput(
            label=f"{p2_mc} eredménye",
            placeholder="Pl. 1",
            required=True,
            max_length=3,
        )
        self.add_item(self.score1_input)
        self.add_item(self.score2_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        raw1 = self.score1_input.value.strip()
        raw2 = self.score2_input.value.strip()
        if not raw1.isdigit() or not raw2.isdigit():
            await interaction.followup.send("❌ Az eredményeknek számnak kell lenniük!", ephemeral=True)
            return

        score1, score2 = int(raw1), int(raw2)
        if score1 == score2:
            await interaction.followup.send("❌ Döntetlen eredmény nem lehetséges!", ephemeral=True)
            return

        p1_id = self.match_data.get("player1_discord_id")
        p2_id = self.match_data.get("player2_discord_id")
        p1_mc = self.match_data.get("player1_mc") or "?"
        p2_mc = self.match_data.get("player2_mc") or "?"

        winner_id = p1_id if score1 > score2 else p2_id

        await arun(db.set_match_winner, self.match_data["id"], winner_id, score1, score2, False)

        round_num = self.match_data.get("round_number") or 1
        text = (
            f"**{round_num}. kör eredmény**\n\n"
            f"`{p1_mc}` {score1} - {score2} `{p2_mc}` -> továbbjutott: <@{winner_id}>"
        )
        await _broadcast_result(interaction, self.tournament_data, text)
        await _lock_ticket_embed(interaction, self.match_data, self.tournament_data)

        await interaction.followup.send("✅ Eredmény sikeresen rögzítve!", ephemeral=True)

        from cogs.tournaments import maybe_advance_round  # local import: avoid cycle
        await maybe_advance_round(interaction.client, self.tournament_data)


async def _broadcast_result(interaction: discord.Interaction, tournament_data: dict, text: str) -> None:
    results_channel_id = tournament_data.get("results_channel_id") or config.results_channel_id
    if not results_channel_id or not interaction.guild:
        return
    ch = interaction.guild.get_channel(results_channel_id)
    if isinstance(ch, discord.TextChannel):
        await ch.send(text)


async def _lock_ticket_embed(interaction: discord.Interaction, match_data: dict, tournament_data: dict) -> None:
    """Az embedet 'Lezárt' állapotra frissíti, majd egy idő után törli a csatornát."""
    match_data = dict(match_data)
    match_data.setdefault("winner_discord_id", -1)  # non-None just for the embed's "Lezárt" branch
    embed = build_ticket_embed(tournament_data, match_data)
    try:
        if interaction.message:
            await interaction.message.edit(embed=embed, view=None)
    except discord.HTTPException:
        pass
    if interaction.channel:
        try:
            await interaction.channel.send("🔒 A meccs lezárult. A csatorna 10 másodperc múlva törlődik.")
        except discord.HTTPException:
            pass
        await asyncio.sleep(10)
        try:
            await interaction.channel.delete(reason="Meccs lezárva.")
        except discord.HTTPException:
            pass


class FFChoiceView(ui.View):
    """Ephemeral segéd-view a Regulator számára: ki FF-elt (mc név szerint, vagy mindkettő)."""

    def __init__(self, match_data: dict, tournament_data: dict):
        super().__init__(timeout=120)
        self.match_data = match_data
        self.tournament_data = tournament_data

        p1_mc = match_data.get("player1_mc") or "Player1"
        p2_mc = match_data.get("player2_mc") or "Player2"

        self.ff_p1.label = f"{p1_mc} FF-elt"
        self.ff_p2.label = f"{p2_mc} FF-elt"

    async def _finish(self, interaction: discord.Interaction, ff_choice: str):
        p1_mc = self.match_data.get("player1_mc") or "?"
        p2_mc = self.match_data.get("player2_mc") or "?"
        round_num = self.match_data.get("round_number") or 1

        if ff_choice == "both":
            text = (
                f"**{round_num}. kör eredmény**\n\n"
                f"`{p1_mc}` és `{p2_mc}` kézi lezárás miatt kiesett. Végső score: 0-0"
            )
            await interaction.response.send_modal(_NoopScoreCloseModal(self, text))
            return

        # Single FF -> ask for each player's score separately via modal.
        await interaction.response.send_modal(FFScoreModal(self, ff_choice))

    @ui.button(label="1. játékos FF-elt", style=discord.ButtonStyle.secondary)
    async def ff_p1(self, interaction: discord.Interaction, button: ui.Button):
        await self._finish(interaction, "p1")

    @ui.button(label="2. játékos FF-elt", style=discord.ButtonStyle.secondary)
    async def ff_p2(self, interaction: discord.Interaction, button: ui.Button):
        await self._finish(interaction, "p2")

    @ui.button(label="Mindkettő (dupla kiesés)", style=discord.ButtonStyle.danger)
    async def ff_both(self, interaction: discord.Interaction, button: ui.Button):
        await self._finish(interaction, "both")


class _NoopScoreCloseModal(ui.Modal, title="Megerősítés"):
    """Apró modal csak azért, hogy a both-FF ág is interaction.response-t küldjön,
    majd rögtön be is fejezi a lezárást."""

    confirm = ui.TextInput(label="Írj be 'ok' a megerősítéshez", default="ok", required=False)

    def __init__(self, parent_view: FFChoiceView, broadcast_text: str):
        super().__init__()
        self.parent_view = parent_view
        self.broadcast_text = broadcast_text

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await arun(db.set_match_winner, self.parent_view.match_data["id"], 0, 0, 0, True)
        await _broadcast_result(interaction, self.parent_view.tournament_data, self.broadcast_text)
        await _lock_ticket_embed(interaction, self.parent_view.match_data, self.parent_view.tournament_data)
        await interaction.followup.send("✅ Rögzítve (dupla FF).", ephemeral=True)

        from cogs.tournaments import maybe_advance_round
        await maybe_advance_round(interaction.client, self.parent_view.tournament_data)


class FFScoreModal(ui.Modal, title="Végső Eredmény (FF)"):
    def __init__(self, parent_view: FFChoiceView, ff_choice: str):
        super().__init__()
        self.parent_view = parent_view
        self.ff_choice = ff_choice

        match_data = parent_view.match_data
        p1_mc = match_data.get("player1_mc") or "Player1"
        p2_mc = match_data.get("player2_mc") or "Player2"

        self.score1_input = ui.TextInput(
            label=f"{p1_mc} végső eredménye",
            placeholder="Pl. 2",
            required=True,
            max_length=3,
        )
        self.score2_input = ui.TextInput(
            label=f"{p2_mc} végső eredménye",
            placeholder="Pl. 1",
            required=True,
            max_length=3,
        )
        self.add_item(self.score1_input)
        self.add_item(self.score2_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        raw1 = self.score1_input.value.strip()
        raw2 = self.score2_input.value.strip()
        if not raw1.isdigit() or not raw2.isdigit():
            await interaction.followup.send("❌ Az eredményeknek számnak kell lenniük!", ephemeral=True)
            return
        score1, score2 = int(raw1), int(raw2)

        match_data = self.parent_view.match_data
        tournament_data = self.parent_view.tournament_data

        p1_id = match_data.get("player1_discord_id")
        p2_id = match_data.get("player2_discord_id")
        p1_mc = match_data.get("player1_mc") or "?"
        p2_mc = match_data.get("player2_mc") or "?"
        round_num = match_data.get("round_number") or 1

        winner_id = p2_id if self.ff_choice == "p1" else p1_id
        winner_mc = p2_mc if self.ff_choice == "p1" else p1_mc
        loser_mc = p1_mc if self.ff_choice == "p1" else p2_mc
        w_score = score2 if self.ff_choice == "p1" else score1
        l_score = score1 if self.ff_choice == "p1" else score2

        await arun(db.set_match_winner, match_data["id"], winner_id, score1, score2, True)

        text = (
            f"**{round_num}. kör eredmény**\n\n"
            f"`{winner_mc}` ff-fel jutott tovább `{loser_mc}` ellen. Végső score: {w_score}-{l_score}"
        )
        await _broadcast_result(interaction, tournament_data, text)
        await _lock_ticket_embed(interaction, match_data, tournament_data)
        await interaction.followup.send("✅ Rögzítve (FF).", ephemeral=True)

        from cogs.tournaments import maybe_advance_round
        await maybe_advance_round(interaction.client, tournament_data)


class MatchTicketView(ui.View):
    def __init__(self, match_data: dict, tournament_data: dict):
        super().__init__(timeout=None)
        self.match_data = match_data
        self.tournament_data = tournament_data

    def _is_regulator(self, interaction: discord.Interaction) -> bool:
        reg_role_id = self.tournament_data.get("regulator_role_id") or config.regulator_role_id
        if not reg_role_id:
            return True  # nincs beállítva regulator role -> ne blokkoljunk feleslegesen
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return any(r.id == reg_role_id for r in member.roles) or member.guild_permissions.administrator

    async def _refresh_match(self) -> dict:
        fresh = await arun(db.get_match_by_ticket, self.match_data.get("ticket_channel_id"))
        if fresh:
            self.match_data = fresh
        return self.match_data

    @ui.button(label="Eredmény", style=discord.ButtonStyle.success, custom_id="match_submit_result")
    async def submit_result(self, interaction: discord.Interaction, button: ui.Button):
        if not self._is_regulator(interaction):
            await interaction.response.send_message("❌ Csak a Regulator használhatja ezt a gombot.", ephemeral=True)
            return
        await self._refresh_match()
        await interaction.response.send_modal(ResultModal(self.match_data, self.tournament_data))

    @ui.button(label="FF", style=discord.ButtonStyle.secondary, custom_id="match_ff")
    async def ff_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self._is_regulator(interaction):
            await interaction.response.send_message("❌ Csak a Regulator használhatja ezt a gombot.", ephemeral=True)
            return
        await self._refresh_match()
        await interaction.response.send_message(
            "Válaszd ki, ki FF-elt:",
            view=FFChoiceView(self.match_data, self.tournament_data),
            ephemeral=True,
        )

    @ui.button(label="Lezárás", style=discord.ButtonStyle.danger, custom_id="match_close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: ui.Button):
        if not self._is_regulator(interaction):
            await interaction.response.send_message("❌ Csak a Regulator használhatja ezt a gombot.", ephemeral=True)
            return
        await interaction.response.send_message("🔒 Csatorna törlése 5 másodpercen belül...", ephemeral=False)
        await asyncio.sleep(5)
        if interaction.channel:
            try:
                await interaction.channel.delete(reason="Match ticket kézzel lezárva.")
            except discord.HTTPException:
                pass

"""NeonTiers Tournament Discord Bot - Fő belépési pont."""

from __future__ import annotations

import logging
import sys
import discord
from discord.ext import commands

from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("neontiers")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
    """Modulok és parancsok szinkronizálása indításkor."""
    log.info("Modulok betöltése...")
    await bot.load_extension("cogs.tournaments")

    # Kényszerített szinkronizálás az instant elérhetőségért
    if config.guild_id > 0:
        guild = discord.Object(id=config.guild_id)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info("✅ Slash parancsok szinkronizálva a szerverre (%d parancs).", len(synced))
    else:
        synced = await bot.tree.sync()
        log.info("✅ Globális slash parancsok szinkronizálva (%d parancs).", len(synced))


@bot.event
async def on_ready():
    log.info("Sikeres bejelentkezés: %s (ID: %s)", bot.user, bot.user.id)

    # Restart utáni állapot-visszaállítás: az aktív bajnokságok queue
    # gombjai és a nyitott meccs ticketek gombjai újra "élővé" válnak.
    cog = bot.get_cog("TournamentsCog")
    if cog is not None:
        try:
            await cog.rehydrate_views()
        except Exception:
            log.exception("Hiba történt a view-k rehydration-je közben.")


@bot.command(name="sync")
@commands.is_owner()
async def sync_prefix_cmd(ctx: commands.Context):
    """Szöveges (!sync) parancs — ezt nem kell szinkronizálni, tehát ezzel
    lehet első alkalommal (vagy ha a slash /sync eltűnt) élesíteni az összes
    slash parancsot, beleértve magát a /sync-et is."""
    try:
        if ctx.guild:
            guild = discord.Object(id=ctx.guild.id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        await ctx.send(f"✅ {len(synced)} slash parancs szinkronizálva.")
    except Exception as exc:
        log.exception("Hiba a !sync parancs futása közben.")
        await ctx.send(f"❌ Hiba történt: `{exc}`")


@bot.tree.command(name="sync", description="Slash parancsok azonnali szinkronizálása (csak adminoknak).")
@discord.app_commands.default_permissions(administrator=True)
async def sync_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        if interaction.guild_id:
            guild = discord.Object(id=interaction.guild_id)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        await interaction.followup.send(
            f"✅ {len(synced)} slash parancs szinkronizálva.", ephemeral=True
        )
    except Exception as exc:
        log.exception("Hiba a /sync parancs futása közben.")
        await interaction.followup.send(f"❌ Hiba történt: `{exc}`", ephemeral=True)


def main():
    bot.run(config.discord_token)


if __name__ == "__main__":
    main()

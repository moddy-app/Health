"""Groupe `/status` — gestion de crise depuis Discord.

Synchronisé sur la guild uniquement : le sync global met jusqu'à une heure à se
propager, celui d'une guild est instantané.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from ..render import colors, theme
from ..render.layout import build_notice_view
from . import modals
from .views import loading_view, show_details

log = logging.getLogger("hm.bot.commands")


def _notice(text: str, accent: int | None = None):
    return build_notice_view(text, accent=accent)


def staff_only():
    """Réservé au rôle staff, dans la guild du salon de statut."""

    async def predicate(interaction: discord.Interaction) -> bool:
        settings = interaction.client.ctx.settings
        if str(interaction.guild_id or "") != str(settings.discord_guild_id):
            return False
        role_id = settings.discord_staff_role_id
        if not role_id:
            # Pas de rôle configuré : on retombe sur les permissions du salon
            # plutôt que d'ouvrir la commande à tout le serveur.
            perms = getattr(interaction.user, "guild_permissions", None)
            return bool(perms and perms.manage_guild)
        return any(str(role.id) == str(role_id) for role in getattr(interaction.user, "roles", []))

    return app_commands.check(predicate)


class StatusCommands(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(name="status", description="Moddy status page controls")

    @app_commands.command(name="incident", description="Open a new incident")
    @staff_only()
    async def incident(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(modals.IncidentCreateModal(interaction.client.ctx))

    @app_commands.command(name="update", description="Post an update on the active incident")
    @staff_only()
    async def update(self, interaction: discord.Interaction) -> None:
        if not await _require_active(interaction):
            return
        await interaction.response.send_modal(modals.IncidentUpdateModal(interaction.client.ctx))

    @app_commands.command(name="resolve", description="Resolve the active incident")
    @staff_only()
    async def resolve(self, interaction: discord.Interaction) -> None:
        if not await _require_active(interaction):
            return
        await interaction.response.send_modal(modals.IncidentResolveModal(interaction.client.ctx))

    @app_commands.command(name="maintenance", description="Schedule a maintenance window")
    @staff_only()
    async def maintenance(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(modals.MaintenanceModal(interaction.client.ctx))

    @app_commands.command(name="check", description="Detailed status, only visible to you")
    @staff_only()
    async def check(self, interaction: discord.Interaction) -> None:
        # Même panneau que le bouton `Details`, loader compris.
        await interaction.response.send_message(view=loading_view(), ephemeral=True)
        await show_details(interaction, interaction.client.ctx)

    @app_commands.command(name="sticky", description="Repost the sticky status message")
    @staff_only()
    async def sticky(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.client.sticky.force_repost()
        await interaction.followup.send(
            view=_notice(f"{theme.EMOJI_OK} Sticky reposted.", colors.ACCENT_RESOLVED),
            ephemeral=True,
        )


async def _require_active(interaction: discord.Interaction) -> bool:
    """`send_modal` ne s'annule pas : on vérifie *avant* de l'envoyer."""
    if await interaction.client.ctx.incidents.get_active():
        return True
    await interaction.response.send_message(
        view=_notice(f"{theme.EMOJI_ALERT} No active incident.", colors.ACCENT_DEGRADED),
        ephemeral=True,
    )
    return False


async def on_tree_error(interaction: discord.Interaction, error: Exception) -> None:
    """Une check qui lève sans réponse laisse l'interaction en échec visible."""
    if isinstance(error, app_commands.CheckFailure):
        text = f"{theme.EMOJI_ALERT} Staff only."
        accent = colors.ACCENT_DEGRADED
    else:
        log.exception("commande en échec", exc_info=error)
        text = f"{theme.EMOJI_ALERT} The command failed. The monitor logged it."
        accent = colors.ACCENT_MAJOR
    view = _notice(text, accent)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)
    except Exception:  # pragma: no cover - l'interaction a pu expirer
        log.warning("impossible de répondre à l'interaction refusée")

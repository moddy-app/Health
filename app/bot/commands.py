"""Groupe `/status` — gestion de crise depuis Discord.

Synchronisé sur la guild uniquement : le sync global met jusqu'à une heure à se
propager, celui d'une guild est instantané.
"""

from __future__ import annotations

import logging
import re

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

    @app_commands.command(
        name="update", description="Post an update on an active incident or maintenance"
    )
    @staff_only()
    async def update(self, interaction: discord.Interaction) -> None:
        target = await _resolve_target(interaction)
        if target is None:
            return
        single, choices = target
        await interaction.response.send_modal(
            modals.IncidentUpdateModal(
                interaction.client.ctx,
                incident_id=single["id"] if single else None,
                choices=choices,
                maintenance=bool(single) and _is_maintenance(single),
            )
        )

    @app_commands.command(
        name="resolve", description="Resolve an active incident, or close an active maintenance"
    )
    @staff_only()
    async def resolve(self, interaction: discord.Interaction) -> None:
        target = await _resolve_target(interaction)
        if target is None:
            return
        single, choices = target
        await interaction.response.send_modal(
            modals.IncidentResolveModal(
                interaction.client.ctx,
                incident_id=single["id"] if single else None,
                choices=choices,
                maintenance=bool(single) and _is_maintenance(single),
            )
        )

    @app_commands.command(name="maintenance", description="Schedule a maintenance window")
    @staff_only()
    async def maintenance(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(modals.MaintenanceModal(interaction.client.ctx))

    @app_commands.command(name="cancel", description="Cancel an active maintenance")
    @staff_only()
    async def cancel(self, interaction: discord.Interaction) -> None:
        target = await _resolve_target(interaction, only_maintenance=True)
        if target is None:
            return
        single, choices = target
        await interaction.response.send_modal(
            modals.MaintenanceCancelModal(
                interaction.client.ctx,
                incident_id=single["id"] if single else None,
                choices=choices,
            )
        )

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

    @app_commands.command(
        name="reload",
        description="Reload updates from Better Stack, and adopt any incident it missed",
    )
    @staff_only()
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        incidents = interaction.client.ctx.incidents
        synced = await incidents.sync_updates()
        adopted = await incidents.adopt_missing()

        if not synced and not adopted:
            await interaction.followup.send(
                view=_notice(
                    f"{theme.EMOJI_ALERT} Nothing to reload — no Better Stack report yet.",
                    colors.ACCENT_DEGRADED,
                ),
                ephemeral=True,
            )
            return

        lines = []
        if synced:
            count = sum(len(incident.get("updates") or []) for incident in synced)
            lines.append(
                f"Reloaded {count} update{'s' if count != 1 else ''} across "
                f"{len(synced)} incident{'s' if len(synced) != 1 else ''}."
            )
        if adopted:
            titles = ", ".join(f"**{i.get('title')}**" for i in adopted)
            lines.append(
                f"Adopted {len(adopted)} incident{'s' if len(adopted) != 1 else ''} Better Stack "
                f"had but we'd missed: {titles}."
            )
        await interaction.followup.send(
            view=_notice(f"{theme.EMOJI_OK} " + " ".join(lines), colors.ACCENT_RESOLVED),
            ephemeral=True,
        )

    @app_commands.command(
        name="resend",
        description="Rewrite an incident message from Better Stack, as a new message or in place",
    )
    @app_commands.describe(
        report="Link to the incident on the status page (or its Better Stack ID)",
        message="Link to the Discord message to rewrite in place — leave empty to post a new one",
    )
    @staff_only()
    async def resend(
        self, interaction: discord.Interaction, report: str, message: str | None = None
    ) -> None:
        settings = interaction.client.ctx.settings
        report_id = parse_report_id(report)
        if report_id is None:
            await interaction.response.send_message(
                view=_notice(
                    f"{theme.EMOJI_ALERT} That doesn't look like a status page incident link.",
                    colors.ACCENT_DEGRADED,
                ),
                ephemeral=True,
            )
            return

        message_id = None
        if message:
            link = parse_message_link(message)
            if link is None:
                await interaction.response.send_message(
                    view=_notice(
                        f"{theme.EMOJI_ALERT} That doesn't look like a Discord message link.",
                        colors.ACCENT_DEGRADED,
                    ),
                    ephemeral=True,
                )
                return
            channel_id, message_id = link
            if channel_id != str(settings.discord_status_channel_id):
                # Le bot n'écrit que dans le salon de statut : un lien d'ailleurs
                # serait cherché au mauvais endroit.
                await interaction.response.send_message(
                    view=_notice(
                        f"{theme.EMOJI_ALERT} The message must be in the status channel.",
                        colors.ACCENT_DEGRADED,
                    ),
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        incident, written = await interaction.client.ctx.incidents.resend(report_id, message_id)
        if incident is None:
            text = f"{theme.EMOJI_ALERT} Better Stack has no report {report_id} on the status page."
            accent = colors.ACCENT_DEGRADED
        elif written is None:
            text = (
                f"{theme.EMOJI_ALERT} Discord refused the message. A message posted by the "
                "webhook can't be edited by the bot — leave the link empty to post a new one."
            )
            accent = colors.ACCENT_MAJOR
        else:
            done = "rewritten" if message_id else "posted"
            text = f"{theme.EMOJI_OK} **{incident.get('title')}** {done} from Better Stack."
            accent = colors.ACCENT_RESOLVED
        await interaction.followup.send(view=_notice(text, accent), ephemeral=True)

    @app_commands.command(
        name="pause",
        description="Stop automatic incidents — Better Stack updates are still relayed",
    )
    @staff_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        incidents = interaction.client.ctx.incidents
        current = await incidents.auto_paused()
        if current:
            text = f"{theme.EMOJI_ALERT} Automatic updates are already paused (by {current.get('by')})."
            accent = colors.ACCENT_DEGRADED
        else:
            await incidents.pause_auto(str(interaction.user))
            text = (
                f"{theme.EMOJI_OK} Automatic updates paused. Outages are still detected, but "
                "no incident is opened, updated or resolved on its own. Better Stack updates "
                "are still relayed. Use `/status resume` to restart."
            )
            accent = colors.ACCENT_RESOLVED
        await interaction.response.send_message(view=_notice(text, accent), ephemeral=True)

    @app_commands.command(name="resume", description="Restart automatic incidents after a pause")
    @staff_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        incidents = interaction.client.ctx.incidents
        if not await incidents.auto_paused():
            text = f"{theme.EMOJI_ALERT} Automatic updates are not paused."
            accent = colors.ACCENT_DEGRADED
        else:
            await incidents.resume_auto(str(interaction.user))
            text = (
                f"{theme.EMOJI_OK} Automatic updates restarted. An outage still going on will "
                "be reported at the next check."
            )
            accent = colors.ACCENT_RESOLVED
        await interaction.response.send_message(view=_notice(text, accent), ephemeral=True)


_REPORT_LINK = re.compile(r"/(?:incident|incidents|status-reports?)/(\d+)")
_MESSAGE_LINK = re.compile(r"discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)")


def parse_report_id(text: str) -> str | None:
    """L'ID d'un report : lien de la status page (avec ou sans langue), lien du
    tableau de bord Better Stack, ou l'ID nu."""
    text = (text or "").strip()
    if text.isdigit():
        return text
    match = _REPORT_LINK.search(text)
    return match.group(1) if match else None


def parse_message_link(text: str) -> tuple[str, str] | None:
    """(salon, message) d'un lien « Copy Message Link »."""
    match = _MESSAGE_LINK.search((text or "").strip())
    return (match.group(1), match.group(2)) if match else None


def _is_maintenance(incident: dict) -> bool:
    return incident.get("type") == "maintenance"


async def _resolve_target(
    interaction: discord.Interaction, *, only_maintenance: bool = False
) -> tuple[dict | None, list[dict] | None] | None:
    """`send_modal` ne s'annule pas : on vérifie *avant* de l'envoyer.

    Plusieurs incidents pouvant être actifs à la fois, cette étape rend soit
    la cible unique (pas d'ambiguïté), soit la liste des choix à proposer dans
    le modal via un `RadioGroup` — jamais les deux. `None` si rien n'est actif,
    l'interaction ayant déjà répondu.
    """
    actives = await interaction.client.ctx.incidents.get_active_all()
    if only_maintenance:
        actives = [i for i in actives if i.get("type") == "maintenance"]
    if not actives:
        label = "maintenance" if only_maintenance else "incident or maintenance"
        await interaction.response.send_message(
            view=_notice(f"{theme.EMOJI_ALERT} No active {label}.", colors.ACCENT_DEGRADED),
            ephemeral=True,
        )
        return None
    if len(actives) == 1:
        return actives[0], None
    return None, actives


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

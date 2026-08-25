"""Vues persistantes du salon de statut.

Une View persistante doit être réenregistrée à chaque démarrage
(`Bot.add_view`) avec un `custom_id` fixe et `timeout=None`, sinon son bouton
est mort après le premier redéploiement — et Railway redéploie souvent.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from .. import keys
from ..render.layout import BaseView, status_detail, status_summary
from ..render.model import StatusPresentation

log = logging.getLogger("hm.bot.views")

REFRESH_ID = "hm:sticky:refresh"


class RefreshButton(ui.Button):
    """Diagnostic rapide en éphémère, lu directement dans Redis.

    Jamais d'appel HTTP vers `/v1/status` : viser son propre process ajouterait
    un point de panne au moment précis où tout casse.
    """

    def __init__(self) -> None:
        super().__init__(
            label="Refresh", style=discord.ButtonStyle.secondary, custom_id=REFRESH_ID
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = getattr(interaction.client, "ctx", None)
        if ctx is None:  # pragma: no cover - le bot est toujours câblé
            await interaction.response.send_message("Monitor not ready.", ephemeral=True)
            return

        # Sans cooldown, le bouton est un vecteur de spam sur un salon public.
        cooldown = keys.REFRESH_COOLDOWN.format(user=interaction.user.id)
        if not await ctx.store.claim(cooldown, ctx.settings.hm_refresh_cooldown):
            await interaction.response.send_message(
                f"Slow down — one refresh every {ctx.settings.hm_refresh_cooldown}s.",
                ephemeral=True,
            )
            return

        public = await ctx.store.get_json(keys.STATUS_PUBLIC) or {}
        snapshot = StatusPresentation.from_public(public)
        heartbeats = {
            service.id: (await ctx.store.get_json(keys.hb(service.id)) or {})
            for service in snapshot.services
        }
        await interaction.response.send_message(
            view=build_detail_view(snapshot, heartbeats), ephemeral=True
        )


class StickyStatusView(BaseView):
    """Le message permanent en bas du salon.

    Construite sans snapshot au démarrage, uniquement pour réenregistrer le
    bouton : `add_view` n'a besoin que des `custom_id`, pas du contenu.
    """

    def __init__(self, snapshot: StatusPresentation | None = None, status_page_url: str = "") -> None:
        super().__init__()
        container = ui.Container(accent_color=snapshot.accent if snapshot else None)
        if snapshot is not None:
            container.add_item(ui.TextDisplay(status_summary(snapshot)))
            if snapshot.incident_title:
                container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
                title = snapshot.incident_title
                container.add_item(ui.TextDisplay(f"**Ongoing:** {title}"))
        else:
            container.add_item(ui.TextDisplay("### Status"))

        row = ui.ActionRow()
        row.add_item(RefreshButton())
        if status_page_url:
            # Un bouton lien sans URL lève à l'envoi.
            row.add_item(
                ui.Button(
                    label="Status Page", style=discord.ButtonStyle.link, url=status_page_url
                )
            )
        container.add_item(row)
        self.add_item(container)


def build_detail_view(snapshot: StatusPresentation, heartbeats: dict[str, dict]) -> BaseView:
    view = BaseView()
    container = ui.Container(accent_color=snapshot.accent)
    container.add_item(ui.TextDisplay(status_detail(snapshot, heartbeats)))
    view.add_item(container)
    return view

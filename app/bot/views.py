"""Vues persistantes du salon de statut.

Une View persistante doit être réenregistrée à chaque démarrage
(`Bot.add_view`) avec un `custom_id` fixe et `timeout=None`, sinon son bouton
est mort après le premier redéploiement — et Railway redéploie souvent.
"""

from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord import ui

from .. import keys
from ..render import colors
from ..render.layout import (
    BaseView,
    build_notice_view,
    service_detail,
    status_header,
    status_summary,
)
from ..render.model import StatusPresentation

log = logging.getLogger("hm.bot.views")

# Le bouton s'appelle « Details » depuis qu'il ouvre le panneau de diagnostic,
# mais son `custom_id` ne bouge pas : c'est lui qui identifie les stickys déjà
# postés dans le salon (`StickyManager._is_ours`). Le renommer abandonnerait
# tous les stickys d'avant le déploiement.
DETAILS_ID = "hm:sticky:refresh"
DETAILS_REFRESH_ID = "hm:details:refresh"

# Le temps que met le panneau à s'afficher. Rien à calculer là-dedans : lire
# Redis prend quelques millisecondes. C'est le temps de voir le loader.
LOADING_MIN = 0.0
LOADING_MAX = 1.0


async def load_status(ctx) -> tuple[StatusPresentation, dict[str, dict]]:
    """L'état courant, lu directement dans Redis.

    Jamais d'appel HTTP vers `/v1/status` : viser son propre process ajouterait
    un point de panne au moment précis où tout casse.
    """
    public = await ctx.store.get_json(keys.STATUS_PUBLIC) or {}
    snapshot = StatusPresentation.from_public(public)
    heartbeats = {
        service.id: (await ctx.store.get_json(keys.hb(service.id)) or {})
        for service in snapshot.services
    }
    return snapshot, heartbeats


async def _claim_cooldown(interaction: discord.Interaction, ctx) -> bool:
    """Sans cooldown, le bouton est un vecteur de spam sur un salon public."""
    cooldown = keys.REFRESH_COOLDOWN.format(user=interaction.user.id)
    if await ctx.store.claim(cooldown, ctx.settings.hm_refresh_cooldown):
        return True
    await interaction.response.send_message(
        view=build_notice_view(
            f"{colors.EMOJI_ALERT} Slow down — once every {ctx.settings.hm_refresh_cooldown}s.",
            accent=colors.ACCENT_DEGRADED,
        ),
        ephemeral=True,
    )
    return False


class DetailsButton(ui.Button):
    """Ouvre le panneau de diagnostic, en éphémère."""

    def __init__(self) -> None:
        super().__init__(
            label="Details",
            style=discord.ButtonStyle.primary,
            emoji=discord.PartialEmoji.from_str(colors.EMOJI_INFO),
            custom_id=DETAILS_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = getattr(interaction.client, "ctx", None)
        if ctx is None:  # pragma: no cover - le bot est toujours câblé
            await interaction.response.send_message(
                view=build_notice_view(
                    f"{colors.EMOJI_ALERT} Monitor not ready.", accent=colors.ACCENT_MAJOR
                ),
                ephemeral=True,
            )
            return
        if not await _claim_cooldown(interaction, ctx):
            return
        await interaction.response.send_message(view=loading_view(), ephemeral=True)
        await show_details(interaction, ctx)


class DetailRefreshButton(ui.Button):
    """Rejoue le panneau sur place, sans en empiler un second."""

    def __init__(self) -> None:
        super().__init__(
            label="Refresh",
            style=discord.ButtonStyle.success,
            emoji=discord.PartialEmoji.from_str(colors.EMOJI_REFRESH),
            custom_id=DETAILS_REFRESH_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = getattr(interaction.client, "ctx", None)
        if ctx is None:  # pragma: no cover - le bot est toujours câblé
            await interaction.response.defer()
            return
        if not await _claim_cooldown(interaction, ctx):
            return
        await interaction.response.edit_message(view=loading_view())
        await show_details(interaction, ctx)


class StickyStatusView(BaseView):
    """Le message permanent en bas du salon.

    Construite sans snapshot au démarrage, uniquement pour réenregistrer le
    bouton : `add_view` n'a besoin que des `custom_id`, pas du contenu.
    """

    def __init__(
        self, snapshot: StatusPresentation | None = None, status_page_url: str = ""
    ) -> None:
        super().__init__()
        container = ui.Container(accent_color=snapshot.accent if snapshot else None)
        if snapshot is not None:
            container.add_item(ui.TextDisplay(status_summary(snapshot)))
            if snapshot.incident_title:
                container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
                # `OnGoing`/`Resolved` sont réservés à la ligne « Status: » du
                # message d'incident : ici, l'icône de niveau s'applique.
                container.add_item(
                    ui.TextDisplay(f"{snapshot.incident_icon} **{snapshot.incident_title}**")
                )
        else:
            container.add_item(ui.TextDisplay("### Status"))

        row = ui.ActionRow()
        row.add_item(DetailsButton())
        if status_page_url:
            # Un bouton lien sans URL lève à l'envoi.
            row.add_item(
                ui.Button(
                    label="Status Page", style=discord.ButtonStyle.link, url=status_page_url
                )
            )
        container.add_item(row)
        self.add_item(container)


class DetailView(BaseView):
    """Le panneau de diagnostic : un bloc par service, séparés.

    Persistante elle aussi : son bouton `Refresh` doit survivre au
    redéploiement, l'éphémère qui le porte, lui, reste affiché.
    """

    def __init__(
        self,
        snapshot: StatusPresentation | None = None,
        heartbeats: dict[str, dict] | None = None,
    ) -> None:
        super().__init__()
        heartbeats = heartbeats or {}
        container = ui.Container(accent_color=snapshot.accent if snapshot else None)
        if snapshot is not None:
            container.add_item(ui.TextDisplay(status_header(snapshot)))
            for service in snapshot.services:
                container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
                container.add_item(
                    ui.TextDisplay(
                        service_detail(
                            service,
                            heartbeats.get(service.id) or {},
                            icon=snapshot.icon_for(service),
                        )
                    )
                )
        else:
            container.add_item(ui.TextDisplay("### Status"))

        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        row = ui.ActionRow()
        row.add_item(DetailRefreshButton())
        container.add_item(row)
        self.add_item(container)


def build_detail_view(
    snapshot: StatusPresentation, heartbeats: dict[str, dict]
) -> BaseView:
    return DetailView(snapshot, heartbeats)


def loading_view() -> BaseView:
    """Ce qu'on voit le temps que le panneau arrive."""
    return build_notice_view(f"{colors.EMOJI_LOADING} Loading…")


async def show_details(interaction: discord.Interaction, ctx) -> None:
    """Remplace le loader par le panneau.

    Le délai n'attend rien — lire Redis prend quelques millisecondes — il laisse
    seulement le loader s'afficher plutôt que de clignoter.

    Une édition qui échoue (éphémère fermé, token expiré) ne casse rien :
    l'utilisateur peut toujours rappuyer sur le bouton.
    """
    snapshot, heartbeats = await load_status(ctx)
    await asyncio.sleep(random.uniform(LOADING_MIN, LOADING_MAX))
    try:
        await interaction.edit_original_response(view=DetailView(snapshot, heartbeats))
    except Exception as exc:
        log.info("affichage du panneau interrompu: %s", exc)

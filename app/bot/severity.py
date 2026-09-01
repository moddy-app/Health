"""Deuxième temps de `/status incident` : la sévérité, service par service.

Le modal tient dans cinq composants top-level et les cinq sont pris : titre,
message, sévérité globale, services affectés, notification. Impossible d'y
demander en plus l'état de *chaque* service — et un modal ne peut pas en ouvrir
un second, Discord ne l'autorise qu'en réponse à une commande ou à un
composant.

D'où ce panneau éphémère, posté juste après le modal : il reprend les services
cochés et demande lesquels sont franchement down. Les autres sont publiés en
`degraded`. Sans cette étape, tout service affecté partait en `downtime` sur la
status page, y compris ceux qui ne faisaient que ralentir.

Il s'ouvre déjà d'accord avec la sévérité saisie au modal — voir `down_set` —
et se publie tel quel : ce que le panneau affiche est ce qui part.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from .. import keys
from ..render import colors, theme
from ..render.layout import BaseView, build_notice_view

log = logging.getLogger("hm.bot.severity")

DOWN_SELECT_ID = "hm:incident:down"
PUBLISH_ID = "hm:incident:publish"

DOWN = "down"
DEGRADED = "degraded"


def draft_key(user_id: int | str) -> str:
    return keys.INCIDENT_DRAFT.format(user=user_id)


async def save_draft(ctx, user_id: int | str, draft: dict) -> None:
    await ctx.store.set_json(draft_key(user_id), draft, ttl=keys.INCIDENT_DRAFT_TTL)


async def load_draft(ctx, user_id: int | str) -> dict | None:
    draft = await ctx.store.get_json(draft_key(user_id))
    return draft if isinstance(draft, dict) else None


def down_set(draft: dict) -> set[str]:
    """Les services franchement down d'un brouillon.

    Une clé `down` absente vaut « pas encore choisi » : c'est la sévérité
    saisie au modal qui décide alors — un incident annoncé « Degraded
    Performance » ouvre le panneau avec tous ses services en `degraded`, tout
    autre niveau les ouvre en `down`. Sans ça, le staff qui choisit `degraded`
    voyait quand même « Down » partout et devait deviner qu'il fallait
    décocher.

    Une liste vide, elle, est un choix, pas une absence : un incident dont
    aucun service n'est franchement down est légitime. Confondre les deux
    (`down or affected`) republiait tout en `downtime` dès que la dernière
    case était décochée, exactement ce que cette étape existe pour éviter.
    """
    down = draft.get("down")
    if down is not None:
        return set(down)
    if draft.get("level") == colors.DEGRADED:
        return set()
    return set(draft.get("affected") or [])


def statuses_for(draft: dict) -> dict[str, str]:
    """L'état publié de chaque service affecté."""
    down = down_set(draft)
    return {
        service: (DOWN if service in down else DEGRADED)
        for service in draft.get("affected") or []
    }


def _summary(draft: dict, settings) -> str:
    lines = [f"**{draft.get('title') or 'Incident'}**"]
    for service, status in statuses_for(draft).items():
        icon = theme.service_icon(status)
        lines.append(f"{icon} {settings.display_name(service)} · {theme.service_label(status)}")
    lines.append(
        "-# Ticked services are published as fully down, unticked ones as "
        "degraded. Publish when the list above is right."
    )
    return "\n".join(lines)


class DownSelect(ui.Select):
    """Les services franchement down ; les autres sont `degraded`."""

    def __init__(self, draft: dict | None = None, settings=None) -> None:
        options = []
        if draft and settings:
            down = down_set(draft)
            options = [
                discord.SelectOption(
                    label=settings.display_name(service),
                    value=service,
                    default=service in down,
                )
                for service in draft.get("affected") or []
            ]
        super().__init__(
            custom_id=DOWN_SELECT_ID,
            placeholder="Fully down — untick a service to mark it degraded",
            options=options,
            min_values=0,
            max_values=max(len(options), 1),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = interaction.client.ctx
        draft = await load_draft(ctx, interaction.user.id)
        if draft is None:
            await _expired(interaction)
            return
        draft["down"] = list(self.values)
        await save_draft(ctx, interaction.user.id, draft)
        await interaction.response.edit_message(view=SeverityView(draft, ctx.settings))


class PublishButton(ui.Button):
    """Rien n'est publié avant ce clic : le modal ne fait que préparer."""

    def __init__(self) -> None:
        super().__init__(
            label="Publish",
            style=discord.ButtonStyle.success,
            custom_id=PUBLISH_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        ctx = interaction.client.ctx
        draft = await load_draft(ctx, interaction.user.id)
        if draft is None:
            await _expired(interaction)
            return

        # Publier appelle Better Stack *et* Discord : bien au-delà des 3s d'une
        # interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)
        await ctx.store.delete(draft_key(interaction.user.id))

        payload = {
            "title": draft.get("title"),
            "message": draft.get("message"),
            "level": draft.get("level"),
            "affected": draft.get("affected") or [],
            "notify": bool(draft.get("notify")),
            "author": draft.get("author") or interaction.user.display_name,
            "statuses": statuses_for(draft),
        }
        incident = await ctx.incidents.handle_command("incident.create", payload)
        if incident is None:
            await interaction.followup.send(
                view=build_notice_view(
                    f"{theme.EMOJI_ALERT} Nothing was published. Check the monitor logs.",
                    accent=colors.ACCENT_MAJOR,
                ),
                ephemeral=True,
            )
            return

        url = incident.get("url")
        detail = (
            f"[View on the status page]({url})"
            if url
            else "Discord only — Better Stack is unavailable."
        )
        await interaction.followup.send(
            view=build_notice_view(
                f"{theme.EMOJI_OK} **{incident.get('title')}**\n-# {detail}",
                accent=colors.ACCENT_RESOLVED,
            ),
            ephemeral=True,
        )


class SeverityView(BaseView):
    """Panneau persistant : son état vit dans le store, pas dans l'instance.

    C'est ce qui lui permet d'être réenregistrée vide au démarrage — les
    callbacks relisent le brouillon de l'utilisateur qui clique.
    """

    def __init__(self, draft: dict | None = None, settings=None) -> None:
        super().__init__()
        container = ui.Container(accent_color=colors.ACCENT_DEGRADED)
        if draft is not None and settings is not None:
            container.add_item(ui.TextDisplay(_summary(draft, settings)))
        else:
            container.add_item(ui.TextDisplay("**Incident severity**"))

        row = ui.ActionRow()
        row.add_item(DownSelect(draft, settings))
        container.add_item(row)

        buttons = ui.ActionRow()
        buttons.add_item(PublishButton())
        container.add_item(buttons)
        self.add_item(container)


async def _expired(interaction: discord.Interaction) -> None:
    view = build_notice_view(
        f"{theme.EMOJI_ALERT} This draft has expired. Run `/status incident` again.",
        accent=colors.ACCENT_DEGRADED,
    )
    if interaction.response.is_done():  # pragma: no cover - défensif
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(view=view)

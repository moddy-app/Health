"""Rendu du message d'incident en objets discord.py — chemin bot.

Le jumeau de `raw.py`. Les deux modules partent du même `IncidentPresentation`
et doivent produire la même structure : ce qui change ici doit changer là-bas,
et `tests/test_render_parity.py` le vérifie.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from ..util import age_seconds
from . import theme
from .model import IncidentPresentation, StatusPresentation
from .raw import header_body, header_title, update_text, visible_updates

log = logging.getLogger("hm.render")


class BaseView(ui.LayoutView):
    """Socle commun : jamais de timeout, jamais d'interaction sans réponse.

    Une exception dans un callback laisserait l'interaction en échec et
    afficherait « L'application ne répond pas » au staff en pleine crise — le
    pire moment. Le handler central répond toujours quelque chose.
    """

    def __init__(self, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: ui.Item
    ) -> None:
        log.exception("callback %s en échec", type(item).__name__, exc_info=error)
        message = "Something went wrong. The monitor logged it."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:  # pragma: no cover - l'interaction a pu expirer
            log.warning("impossible de répondre à l'interaction en échec")


def build_header_container(p: IncidentPresentation) -> ui.Container:
    container = ui.Container(accent_color=p.accent)
    if p.url:
        container.add_item(
            ui.Section(
                ui.TextDisplay(header_title(p)),
                accessory=ui.Button(
                    label="View Incident", style=discord.ButtonStyle.link, url=p.url
                ),
            )
        )
    else:
        # Un bouton lien sans URL lève à l'envoi, et une Section sans accessory
        # est refusée : sans URL, l'en-tête redevient du texte.
        container.add_item(ui.TextDisplay(header_title(p)))
    container.add_item(ui.TextDisplay(header_body(p)))
    return container


def build_updates_container(p: IncidentPresentation) -> ui.Container | None:
    if not p.updates:
        return None

    container = ui.Container(accent_color=None)
    container.add_item(ui.TextDisplay("### **Updates:**"))

    shown, hidden = visible_updates(p)
    if hidden:
        container.add_item(ui.TextDisplay(f"-# {hidden} earlier update(s) not shown."))

    for index, update in enumerate(shown):
        if index:
            container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(update_text(update)))
    return container


def build_layout_view(p: IncidentPresentation) -> BaseView:
    """La View complète du message d'incident : en-tête + historique."""
    view = BaseView()
    view.add_item(build_header_container(p))
    updates = build_updates_container(p)
    if updates is not None:
        view.add_item(updates)
    return view


# ----------------------------------------------------------------------
# État courant — sticky et vue détaillée
# ----------------------------------------------------------------------
def status_summary(s: StatusPresentation) -> str:
    """En-tête du sticky : bandeau, horodatage relatif, une ligne par service."""
    lines = [f"### {s.emoji} {s.headline}", f"-# Last updated <t:{s.timestamp}:R>", ""]
    width = max((len(service.name) for service in s.services), default=0)
    for service in s.services:
        icon = theme.service_icon(service.status)
        lines.append(f"{icon} ``{service.name.ljust(width)}``  {service.label}")
    return "\n".join(lines)


def status_detail(s: StatusPresentation, heartbeats: dict[str, dict]) -> str:
    """Réponse du bouton « Refresh » : plus détaillée que le sticky.

    C'est l'outil de diagnostic rapide pendant une crise — version, uptime, âge
    du dernier heartbeat et contenu de `checks`, service par service.
    """
    blocks = [f"### {s.emoji} {s.headline}", f"-# Last updated <t:{s.timestamp}:R>"]
    if s.incident_title:
        title = f"[{s.incident_title}]({s.incident_url})" if s.incident_url else s.incident_title
        blocks.append(f"**Ongoing:** {title}")

    for service in s.services:
        hb = heartbeats.get(service.id) or {}
        details = []
        if hb.get("version"):
            details.append(f"`{hb['version']}`")
        uptime = hb.get("uptime_s")
        if uptime is not None:
            details.append(f"up {int(uptime) // 3600}h{(int(uptime) % 3600) // 60:02d}")
        age = age_seconds(hb.get("received_at"))
        details.append(f"hb {int(age)}s ago" if age is not None else "no heartbeat")
        if service.impacted_by:
            details.append("impacted by " + ", ".join(service.impacted_by))

        icon = theme.service_icon(service.status)
        line = f"{icon} **{service.name}** — {service.label}\n-# " + " · ".join(details)
        checks = hb.get("checks") or {}
        if checks:
            # `checks` est un dictionnaire à clés libres : on l'itère, on ne
            # l'interprète jamais.
            line += "\n-# " + " · ".join(f"{key}: {value}" for key, value in checks.items())
        blocks.append(line)

    return "\n".join(blocks)


def build_notice_view(text: str, *, accent: int | None = None) -> BaseView:
    """Réponse courte du bot — toujours en Components V2, jamais en texte nu.

    Le salon de statut ne doit contenir qu'un seul format de message : un
    container, une ligne, pas d'embed et pas de contenu brut.
    """
    view = BaseView()
    container = ui.Container(accent_color=accent)
    container.add_item(ui.TextDisplay(text))
    view.add_item(container)
    return view

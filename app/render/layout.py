"""Rendu du message d'incident en objets discord.py — chemin bot.

Le jumeau de `raw.py`. Les deux modules partent du même `IncidentPresentation`
et doivent produire la même structure : ce qui change ici doit changer là-bas,
et `tests/test_render_parity.py` le vérifie.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from . import theme
from .model import IncidentPresentation, StatusPresentation, unix
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


# Un horodatage Discord est rendu en clair (« 3 seconds ago ») : sa largeur
# finale n'est pas celle de la balise, on la tient pour acquise.
TIMESTAMP_WIDTH = 13


def pending(value: str, *, revealed: bool, width: int | None = None) -> str:
    """La valeur, ou des tirets de sa largeur tant qu'elle n'est pas révélée.

    Le panneau garde ainsi sa forme *et* ses dimensions pendant le chargement :
    les mots fixes restent lisibles, seule l'information à venir est masquée.
    Un message qui grandit sous le curseur déplace ce qu'on était en train de
    lire.
    """
    if revealed:
        return value
    return "-" * max(width or len(value), 1)


def status_header(s: StatusPresentation, *, revealed: bool = True) -> str:
    """En-tête du panneau de détail : le niveau global et rien d'autre.

    Tant que tous les services ne sont pas révélés, l'en-tête ne conclut rien :
    annoncer « All Systems Operational » avant d'avoir affiché le premier
    service serait donner la réponse avant la question.
    """
    icon = s.emoji if revealed else theme.EMOJI_LOADING
    headline = s.headline if revealed else "Loading"
    lines = [
        f"### {icon} {headline}",
        "-# Last updated "
        + pending(f"<t:{s.timestamp}:R>", revealed=revealed, width=TIMESTAMP_WIDTH),
    ]
    if s.incident_title:
        if revealed:
            link = f"[{s.incident_title}]({s.incident_url})" if s.incident_url else s.incident_title
            lines.append(f"{theme.EMOJI_ONGOING} **{link}**")
        else:
            # Le titre masqué, pas le lien : l'URL ne s'affiche jamais, elle ne
            # doit pas compter dans la largeur.
            masked = pending(s.incident_title, revealed=False)
            lines.append(f"{theme.EMOJI_LOADING} **{masked}**")
    return "\n".join(lines)


def _check_ok(value) -> bool:
    """Un check est en échec quand il le dit lui-même, jamais par déduction.

    Les noms de clés restent libres (§6) : seul `ok`, qui fait partie du contrat
    de heartbeat, est lu. Une valeur d'une autre forme est prise pour bonne —
    c'est `status` qui décide de l'état du service, pas ce dictionnaire.
    """
    if isinstance(value, dict):
        return bool(value.get("ok", True))
    if isinstance(value, bool):
        return value
    return True


def check_summary(checks: dict) -> str | None:
    """Une ligne pour tous les checks — le détail seulement quand ça casse.

    Le dump brut des dictionnaires était illisible : en régime normal, un
    compteur suffit ; en panne, ce sont les checks en échec qu'on veut voir, et
    eux seuls.
    """
    if not checks:
        return None
    failing = [name for name, value in checks.items() if not _check_ok(value)]
    if not failing:
        total = len(checks)
        return f"{theme.check_icon(True)} {total} check{'s' if total > 1 else ''} passing"
    names = ", ".join(name.replace("_", " ") for name in failing)
    return f"{theme.check_icon(False)} {names} · {len(checks) - len(failing)}/{len(checks)} passing"


def service_detail(service, hb: dict, *, revealed: bool = True) -> str:
    """Le bloc d'un service : état, puis les faits qui l'expliquent.

    C'est l'outil de diagnostic rapide pendant une crise — version, uptime,
    dernier heartbeat, checks en échec. Tant qu'il n'est pas révélé, le bloc
    garde tout ce qui ne change pas — son nom, les mots `up`, `heartbeat` — et
    remplace par des tirets ce qui va s'afficher, à la largeur que ça prendra.
    """
    def hidden(value: str, width: int | None = None) -> str:
        return pending(value, revealed=revealed, width=width)

    facts = []
    if hb.get("version"):
        facts.append(f"`{hidden(str(hb['version']))}`")
    uptime = hb.get("uptime_s")
    if uptime is not None:
        facts.append(
            "up " + hidden(f"{int(uptime) // 3600}h{(int(uptime) % 3600) // 60:02d}")
        )
    received = hb.get("received_at")
    # Un timestamp Discord plutôt qu'un âge calculé : le panneau reste juste
    # même quand il reste affiché plusieurs minutes, et chacun le lit dans son
    # fuseau.
    if received:
        facts.append("heartbeat " + hidden(f"<t:{unix(received)}:R>", TIMESTAMP_WIDTH))
    else:
        facts.append(hidden("no heartbeat"))
    if service.impacted_by:
        facts.append("impacted by " + hidden(", ".join(service.impacted_by)))

    icon = theme.service_icon(service.status) if revealed else theme.EMOJI_LOADING
    lines = [f"{icon} **{service.name}** · {hidden(service.label)}"]
    lines.append("-# " + " · ".join(facts))
    summary = check_summary(hb.get("checks") or {})
    if summary:
        # L'icône du résumé dit déjà si ça passe ou non : elle attend son tour.
        icon, _, rest = summary.partition(" ")
        lines.append(f"-# {icon if revealed else theme.EMOJI_LOADING} {hidden(rest)}")

    return "\n".join(lines)


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

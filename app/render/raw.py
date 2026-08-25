"""Rendu du message d'incident en JSON brut — chemin webhook.

Le jumeau de `layout.py` : même modèle en entrée, même structure en sortie,
mais construite à la main parce qu'un webhook ne connaît pas discord.py. Le
test de parité de `tests/test_render_parity.py` veille à ce que les deux ne
divergent pas.
"""

from __future__ import annotations

from . import theme
from .model import IncidentPresentation

# Types de composants Discord utilisés ici.
TYPE_BUTTON = 2
TYPE_SECTION = 9
TYPE_TEXT_DISPLAY = 10
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

BUTTON_STYLE_LINK = 5

# Flag message IS_COMPONENTS_V2.
IS_COMPONENTS_V2 = 1 << 15  # 32768

# Un message Discord plafonne à 40 composants ; on garde de la marge en ne
# rendant que les updates les plus récents.
MAX_UPDATES = 15


def quote(message: str) -> str:
    """Blockquote Discord : le `> ` doit être répété sur chaque ligne."""
    lines = (message or "").strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def header_title(p: IncidentPresentation) -> str:
    return f"### {p.emoji} {p.title}"


def header_body(p: IncidentPresentation) -> str:
    affected = ", ".join(f"``{name}``" for name in p.affected) or "``—``"
    body = (
        f"**Created by:** {p.created_by}\n"
        f"**Affected services:** {affected}\n"
        f"**Status:** {p.status_emoji}{p.status_label}"
    )
    if p.mentions:
        # En petit, sous l'en-tête : la mention doit prévenir, pas crier. Elle
        # est portée par le message lui-même plutôt que par un message séparé —
        # Discord ne repingue pas à l'édition, l'alerte ne part donc qu'une fois.
        body += f"\n-# {p.mentions}"
    return body


def update_text(update) -> str:
    return f"**{update.kind_label}** — <t:{update.timestamp}:F> :\n{quote(update.message)}"


def visible_updates(p: IncidentPresentation) -> tuple[list, int]:
    """Les updates rendus, et le nombre de ceux qui ont été coupés."""
    shown = p.updates[-MAX_UPDATES:]
    return shown, len(p.updates) - len(shown)


def build_header_container(p: IncidentPresentation) -> dict:
    title = {"type": TYPE_TEXT_DISPLAY, "content": header_title(p)}

    if p.url:
        head: dict = {
            "type": TYPE_SECTION,
            "accessory": {
                "type": TYPE_BUTTON,
                "style": BUTTON_STYLE_LINK,
                "label": p.link_label,
                "url": p.url,
            },
            "components": [title],
        }
    else:
        # Pas encore de report Better Stack (API down, ou incident degraded) :
        # une Section sans accessory est refusée par l'API, on dégrade en texte.
        head = title

    return {
        "type": TYPE_CONTAINER,
        "accent_color": p.accent,
        "components": [head, {"type": TYPE_TEXT_DISPLAY, "content": header_body(p)}],
    }


def build_updates_container(p: IncidentPresentation) -> dict | None:
    if not p.updates:
        return None

    components: list[dict] = [{"type": TYPE_TEXT_DISPLAY, "content": "### **Updates:**"}]

    shown, hidden = visible_updates(p)
    if hidden:
        components.append(
            {"type": TYPE_TEXT_DISPLAY, "content": f"-# {hidden} earlier update(s) not shown."}
        )

    for index, update in enumerate(shown):
        if index:
            components.append({"type": TYPE_SEPARATOR, "divider": True, "spacing": 1})
        components.append({"type": TYPE_TEXT_DISPLAY, "content": update_text(update)})

    return {"type": TYPE_CONTAINER, "accent_color": None, "components": components}


def build_raw_components(p: IncidentPresentation) -> list[dict]:
    """Les deux containers du message d'incident : en-tête + historique."""
    containers = [build_header_container(p)]
    updates = build_updates_container(p)
    if updates:
        containers.append(updates)
    return containers


def build_raw_embed(p: IncidentPresentation) -> dict:
    """Repli dégradé si le webhook refuse les Components V2."""
    fields = [
        {
            "name": "Affected services",
            "value": ", ".join(f"`{name}`" for name in p.affected) or "`—`",
            "inline": False,
        },
        {"name": "Status", "value": p.status_label, "inline": True},
    ]
    if p.updates:
        last = p.updates[-1]
        fields.append(
            {"name": theme.kind_label(last.kind), "value": last.message[:1024], "inline": False}
        )

    embed: dict = {
        "title": p.title,
        "description": p.message[:4000],
        "color": p.accent,
        "fields": fields,
        "timestamp": p.created_at,
    }
    if p.url:
        embed["url"] = p.url
    return embed

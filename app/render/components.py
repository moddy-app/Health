"""Construction du JSON brut des Components V2.

Une seule fonction de rendu, deux transports : le bot relaie ce JSON tel quel,
le webhook l'envoie directement.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import colors

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

_KIND_LABELS = {
    "created": "Created",
    "updated": "Updated",
    "resolved": "Resolved",
    "scheduled": "Scheduled",
}


def _unix(value: str | None) -> int:
    """ISO-8601 -> timestamp unix, pour `<t:unix:F>`."""
    if not value:
        return int(datetime.now(tz=timezone.utc).timestamp())
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(datetime.now(tz=timezone.utc).timestamp())


def _quote(message: str) -> str:
    """Blockquote Discord : le `> ` doit être répété sur chaque ligne."""
    lines = (message or "").strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _status_label(incident: dict) -> str:
    if incident.get("status") == "resolved":
        return "Resolved"
    if incident.get("type") == "maintenance":
        return "Maintenance"
    return "Ongoing"


def _affected_block(incident: dict, names: dict[str, str]) -> str:
    services = incident.get("affected") or []
    if not services:
        return "``—``"
    return ", ".join(f"``{names.get(s, s)}``" for s in services)


def build_header_container(incident: dict, names: dict[str, str]) -> dict:
    resolved = incident.get("status") == "resolved"
    title = incident.get("title") or "Incident"
    header_text = {
        "type": TYPE_TEXT_DISPLAY,
        "content": f"### {colors.emoji(resolved)} {title}",
    }

    url = incident.get("url")
    if url:
        head: dict = {
            "type": TYPE_SECTION,
            "accessory": {
                "type": TYPE_BUTTON,
                "style": BUTTON_STYLE_LINK,
                "label": "View Incident",
                "url": url,
            },
            "components": [header_text],
        }
    else:
        # Pas encore de report Better Stack (API down, ou incident degraded) :
        # une Section sans accessory est refusée par l'API, on dégrade en texte.
        head = header_text

    body = (
        f"**Created by:** {incident.get('created_by') or 'Moddy Health Monitor'}\n"
        f"**Affected services:** {_affected_block(incident, names)}\n"
        f"**Status:** {colors.emoji(resolved)}{_status_label(incident)}"
    )

    return {
        "type": TYPE_CONTAINER,
        "accent_color": colors.accent(incident.get("level", colors.MAJOR_OUTAGE), resolved),
        "components": [head, {"type": TYPE_TEXT_DISPLAY, "content": body}],
    }


def build_updates_container(incident: dict) -> dict | None:
    updates = incident.get("updates") or []
    if not updates:
        return None

    components: list[dict] = [{"type": TYPE_TEXT_DISPLAY, "content": "### **Updates:**"}]

    shown = updates[-MAX_UPDATES:]
    if len(shown) < len(updates):
        components.append(
            {
                "type": TYPE_TEXT_DISPLAY,
                "content": f"-# {len(updates) - len(shown)} earlier update(s) not shown.",
            }
        )

    for index, update in enumerate(shown):
        if index:
            components.append({"type": TYPE_SEPARATOR, "divider": True, "spacing": 1})
        label = _KIND_LABELS.get(update.get("kind", "updated"), "Updated")
        components.append(
            {
                "type": TYPE_TEXT_DISPLAY,
                "content": (
                    f"**{label}** — <t:{_unix(update.get('at'))}:F> :\n"
                    f"{_quote(update.get('message', ''))}"
                ),
            }
        )

    return {"type": TYPE_CONTAINER, "accent_color": None, "components": components}


def build_incident_components(incident: dict, names: dict[str, str] | None = None) -> list[dict]:
    """Les deux containers du message d'incident : en-tête + historique."""
    names = names or {}
    containers = [build_header_container(incident, names)]
    updates = build_updates_container(incident)
    if updates:
        containers.append(updates)
    return containers


def build_incident_embed(incident: dict, names: dict[str, str] | None = None) -> dict:
    """Repli dégradé si le webhook refuse les Components V2."""
    names = names or {}
    resolved = incident.get("status") == "resolved"
    fields = [
        {
            "name": "Affected services",
            "value": _affected_block(incident, names).replace("``", "`"),
            "inline": False,
        },
        {"name": "Status", "value": _status_label(incident), "inline": True},
    ]
    updates = incident.get("updates") or []
    if updates:
        last = updates[-1]
        fields.append(
            {
                "name": _KIND_LABELS.get(last.get("kind", "updated"), "Update"),
                "value": (last.get("message") or "")[:1024],
                "inline": False,
            }
        )

    embed: dict = {
        "title": incident.get("title") or "Incident",
        "description": (incident.get("message") or "")[:4000],
        "color": colors.accent(incident.get("level", colors.MAJOR_OUTAGE), resolved),
        "fields": fields,
        "timestamp": incident.get("created_at"),
    }
    if incident.get("url"):
        embed["url"] = incident["url"]
    return embed

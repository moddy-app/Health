"""Couleurs, émojis et libellés du rendu Discord.

`colors.py` porte la palette et le vocabulaire des niveaux, partagés avec le
cœur du monitor. Ce module ne s'occupe que de l'apparence : ce qui change ici
ne change rien à la détection.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import colors

# Statuts d'un incident tels que le monitor les écrit.
OPEN = "open"
UPDATING = "updating"
RESOLVED = "resolved"

# Types d'incident.
MAINTENANCE = "maintenance"


@dataclass(frozen=True)
class Style:
    color: int
    emoji: str
    label: str


# Trois icônes pour tout le rendu : en cours, résolu, en attente. Rien d'autre
# n'est autorisé — le message d'incident doit rester lisible d'un coup d'œil.
EMOJI_ONGOING = colors.EMOJI_ONGOING
EMOJI_RESOLVED = colors.EMOJI_RESOLVED
EMOJI_PENDING = colors.EMOJI_PENDING

STATUS_STYLES: dict[str, Style] = {
    OPEN: Style(colors.ACCENT_MAJOR, EMOJI_ONGOING, "Ongoing"),
    UPDATING: Style(colors.ACCENT_MAJOR, EMOJI_ONGOING, "Ongoing"),
    RESOLVED: Style(colors.ACCENT_RESOLVED, EMOJI_RESOLVED, "Resolved"),
    MAINTENANCE: Style(colors.ACCENT_MAINTENANCE, EMOJI_PENDING, "Maintenance"),
}

KIND_LABELS: dict[str, str] = {
    "created": "Created",
    "updated": "Updated",
    "resolved": "Resolved",
    "scheduled": "Scheduled",
}

# Libellés publics des états de service, pour le sticky et la vue détaillée.
SERVICE_ICONS: dict[str, str] = {
    "operational": EMOJI_RESOLVED,
    "degraded": EMOJI_PENDING,
    "down": EMOJI_ONGOING,
    "unknown": EMOJI_PENDING,
    "maintenance": EMOJI_PENDING,
}

LEVEL_ICONS: dict[str, str] = {
    colors.OPERATIONAL: EMOJI_RESOLVED,
    colors.DEGRADED: EMOJI_PENDING,
    colors.MAINTENANCE: EMOJI_PENDING,
    colors.PARTIAL_OUTAGE: EMOJI_ONGOING,
    colors.MAJOR_OUTAGE: EMOJI_ONGOING,
}

SERVICE_LABELS: dict[str, str] = {
    "operational": "Operational",
    "degraded": "Degraded",
    "down": "Down",
    "unknown": "Unknown",
    "maintenance": "Maintenance",
}

# Bandeau du sticky selon le niveau global.
LEVEL_HEADLINES: dict[str, str] = {
    colors.OPERATIONAL: "All Systems Operational",
    colors.DEGRADED: "Degraded Performance",
    colors.PARTIAL_OUTAGE: "Partial Outage",
    colors.MAJOR_OUTAGE: "Major Outage",
    colors.MAINTENANCE: "Scheduled Maintenance",
}


def accent(level: str, resolved: bool = False) -> int:
    return colors.accent(level, resolved)


def emoji(resolved: bool) -> str:
    return colors.emoji(resolved)


def status_style(status: str, type_: str = "") -> Style:
    """Style de la ligne « Status » de l'en-tête.

    Un incident de maintenance porte son propre libellé tant qu'il n'est pas
    résolu ; une fois résolu, il redevient un incident clos comme un autre.
    """
    if status == RESOLVED:
        return STATUS_STYLES[RESOLVED]
    if type_ == MAINTENANCE:
        return STATUS_STYLES[MAINTENANCE]
    return STATUS_STYLES.get(status, STATUS_STYLES[OPEN])


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, "Updated")


def service_label(status: str) -> str:
    return SERVICE_LABELS.get(status, status.replace("_", " ").title())


def service_icon(status: str) -> str:
    return SERVICE_ICONS.get(status, EMOJI_PENDING)


def level_icon(level: str) -> str:
    return LEVEL_ICONS.get(level, EMOJI_ONGOING)


def headline(level: str) -> str:
    return LEVEL_HEADLINES.get(level, "Service Disruption")

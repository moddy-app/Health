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


# Les deux icônes de la ligne « Status: », et personne d'autre ne les emploie.
EMOJI_ONGOING = colors.EMOJI_ONGOING
EMOJI_RESOLVED = colors.EMOJI_RESOLVED

# Icônes des réponses éphémères du bot — un accusé de réception n'est pas un
# état public, il ne prend donc jamais une icône de service.
EMOJI_OK = colors.EMOJI_OK
EMOJI_ALERT = colors.EMOJI_ALERT
EMOJI_LOADING = colors.EMOJI_LOADING

STATUS_STYLES: dict[str, Style] = {
    OPEN: Style(colors.ACCENT_MAJOR, EMOJI_ONGOING, "On Going"),
    UPDATING: Style(colors.ACCENT_MAJOR, EMOJI_ONGOING, "On Going"),
    RESOLVED: Style(colors.ACCENT_RESOLVED, EMOJI_RESOLVED, "Resolved"),
    MAINTENANCE: Style(colors.ACCENT_MAINTENANCE, EMOJI_ONGOING, "Maintenance"),
}

KIND_LABELS: dict[str, str] = {
    "created": "Created",
    "updated": "Updated",
    "resolved": "Resolved",
    "scheduled": "Scheduled",
}

# Libellés publics des états de service, pour le sticky et le panneau de détail.
SERVICE_ICONS: dict[str, str] = {
    "operational": colors.EMOJI_OPERATIONAL,
    "degraded": colors.EMOJI_DEGRADED,
    "down": colors.EMOJI_DOWN,
    "maintenance": colors.EMOJI_MAINTENANCE,
    # Un service dont on n'a jamais rien reçu n'est pas en panne : il est muet.
    "unknown": colors.EMOJI_ALERT,
}

LEVEL_ICONS: dict[str, str] = dict(colors.EMOJI_BY_LEVEL)

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


def level_emoji(level: str, resolved: bool = False) -> str:
    return colors.level_emoji(level, resolved)


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
    return SERVICE_ICONS.get(status, colors.EMOJI_ALERT)


def level_icon(level: str) -> str:
    return LEVEL_ICONS.get(level, colors.EMOJI_DOWN)


def check_icon(ok: bool) -> str:
    """Icône d'un check individuel, dans le panneau de détail (éphémère)."""
    return EMOJI_OK if ok else EMOJI_ALERT


def headline(level: str, *, any_down: bool = False) -> str:
    """Bandeau du sticky et du panneau de détail.

    `DEGRADED` recouvre deux réalités distinctes : un service réellement lent,
    et un service non-critique tombé (`aggregate()` ne réserve les niveaux
    « outage » qu'aux services critiques). Dire « Degraded Performance »
    quand un service est en fait `down` — constaté en production sur une sonde
    de site vitrine — annonce moins grave que la réalité.
    """
    if level == colors.DEGRADED and any_down:
        return "Some Services Are Down"
    return LEVEL_HEADLINES.get(level, "Service Disruption")

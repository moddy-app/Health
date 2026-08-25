"""Palette, emojis et mapping des niveaux (§7 « Table de rendu »)."""

from __future__ import annotations

# Niveaux de sévérité agrégée
OPERATIONAL = "operational"
DEGRADED = "degraded"
PARTIAL_OUTAGE = "partial_outage"
MAJOR_OUTAGE = "major_outage"
MAINTENANCE = "maintenance"

# Ordre croissant de gravité, sert aux comparaisons d'escalade.
SEVERITY_ORDER: dict[str, int] = {
    OPERATIONAL: 0,
    MAINTENANCE: 1,
    DEGRADED: 2,
    PARTIAL_OUTAGE: 3,
    MAJOR_OUTAGE: 4,
}

# accent_color Discord (entiers décimaux)
ACCENT_MAJOR = 15_280_939  # #E93A3A
ACCENT_DEGRADED = 15_774_258  # #F0B232
ACCENT_MAINTENANCE = 5_793_266  # #5865F2
ACCENT_RESOLVED = 5_763_719  # #57F287

ACCENT_BY_LEVEL: dict[str, int] = {
    MAJOR_OUTAGE: ACCENT_MAJOR,
    PARTIAL_OUTAGE: ACCENT_MAJOR,
    DEGRADED: ACCENT_DEGRADED,
    MAINTENANCE: ACCENT_MAINTENANCE,
    OPERATIONAL: ACCENT_RESOLVED,
}

# Équivalents hex, utilisés pour l'embed de repli.
HEX_BY_LEVEL: dict[str, str] = {
    MAJOR_OUTAGE: "#E93A3A",
    PARTIAL_OUTAGE: "#E93A3A",
    DEGRADED: "#F0B232",
    MAINTENANCE: "#5865F2",
    OPERATIONAL: "#57F287",
}

# Trois icônes, pas une de plus : le rendu doit rester sobre, et ces émojis
# doivent être uploadés en *application emojis* sur l'application Health
# Monitor — sinon le chemin webhook les rend cassés.
EMOJI_ONGOING = "<:error:1541616427197530203>"
EMOJI_RESOLVED = "<:check_circle:1541616428657016926>"
EMOJI_PENDING = "<a:spinner:1541617132104843264>"

# États de ressource Better Stack
BS_RESOLVED = "resolved"
BS_DEGRADED = "degraded"
BS_DOWNTIME = "downtime"
BS_MAINTENANCE = "maintenance"


def accent(level: str, resolved: bool = False) -> int:
    """Couleur du container d'en-tête. Un incident résolu passe au vert."""
    if resolved:
        return ACCENT_RESOLVED
    return ACCENT_BY_LEVEL.get(level, ACCENT_MAJOR)


def emoji(resolved: bool) -> str:
    return EMOJI_RESOLVED if resolved else EMOJI_ONGOING


def bs_status_for(level: str, service_status: str) -> str:
    """État Better Stack d'une ressource affectée.

    Le niveau `maintenance` prime : la doc Better Stack impose
    `status: "maintenance"` quand `report_type: "maintenance"`.
    """
    if level == MAINTENANCE:
        return BS_MAINTENANCE
    if service_status == "down":
        return BS_DOWNTIME
    if service_status == "degraded":
        return BS_DEGRADED
    return BS_RESOLVED

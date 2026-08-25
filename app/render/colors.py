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

# Liseré des containers (accent_color Discord, entiers décimaux).
ACCENT_MAJOR = 15_280_939  # #E92B2B
ACCENT_DEGRADED = 16_747_520  # #FF8C00
ACCENT_MAINTENANCE = 5_866_977  # #5985E1
ACCENT_RESOLVED = 3_641_431  # #379057

ACCENT_BY_LEVEL: dict[str, int] = {
    MAJOR_OUTAGE: ACCENT_MAJOR,
    PARTIAL_OUTAGE: ACCENT_MAJOR,
    DEGRADED: ACCENT_DEGRADED,
    MAINTENANCE: ACCENT_MAINTENANCE,
    OPERATIONAL: ACCENT_RESOLVED,
}

# Équivalents hex, utilisés pour l'embed de repli.
HEX_BY_LEVEL: dict[str, str] = {
    MAJOR_OUTAGE: "#E92B2B",
    PARTIAL_OUTAGE: "#E92B2B",
    DEGRADED: "#FF8C00",
    MAINTENANCE: "#5985E1",
    OPERATIONAL: "#379057",
}

# Le jeu d'icônes complet du rendu. Aucune autre n'est autorisée, et toutes
# doivent être uploadées en *application emojis* sur l'application Health
# Monitor — sinon le chemin webhook les rend cassées.
#
# Trois familles, qui ne se mélangent jamais :
#   1. l'état d'un service ou d'un niveau — titre d'incident, sticky, panneau ;
#   2. la ligne « Status: » d'un incident, et elle seule ;
#   3. les réponses éphémères du bot, en blanc, qui ne sont pas un état public.
EMOJI_DOWN = "<:down:1541799254807543808>"
EMOJI_DEGRADED = "<:degraded:1541799158938083430>"
EMOJI_MAINTENANCE = "<:maintenance:1541798162833080320>"
EMOJI_OPERATIONAL = "<:check_circle:1541801328584433664>"

# Réservées à « Status: … » — jamais dans un titre, jamais dans une liste de
# services : elles disent où en est l'incident, pas comment va un service.
EMOJI_ONGOING = "<:OnGoing:1541798161599828038>"
EMOJI_RESOLVED = "<:Resolved:1541798160278749244>"

# Réponses éphémères et détail des checks : variantes blanches, neutres.
EMOJI_OK = "<:check_circle_white:1541799862859989052>"
EMOJI_ALERT = "<:exclamation:1541799657829568582>"

# Chargement, et rien d'autre.
EMOJI_LOADING = "<a:spinner:1541617132104843264>"

# Icônes de boutons — elles n'apparaissent jamais dans du texte.
EMOJI_INFO = "<:info:1541808220610363423>"
EMOJI_REFRESH = "<:refresh:1541808218760544376>"

# État d'un service ou d'un niveau agrégé -> icône.
EMOJI_BY_LEVEL: dict[str, str] = {
    MAJOR_OUTAGE: EMOJI_DOWN,
    PARTIAL_OUTAGE: EMOJI_DOWN,
    DEGRADED: EMOJI_DEGRADED,
    MAINTENANCE: EMOJI_MAINTENANCE,
    OPERATIONAL: EMOJI_OPERATIONAL,
}

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


def level_emoji(level: str, resolved: bool = False) -> str:
    """Icône d'un niveau — titre d'incident, bandeau du sticky, ligne de service.

    Un incident résolu porte l'icône « opérationnel », quelle qu'ait été sa
    gravité : c'est son état actuel qui compte.
    """
    if resolved:
        return EMOJI_OPERATIONAL
    return EMOJI_BY_LEVEL.get(level, EMOJI_DOWN)


def bs_status_for(level: str, service_status: str, report_type: str | None = None) -> str:
    """État Better Stack d'une ressource affectée.

    C'est le **type du report** qui décide, pas le niveau de l'incident : Better
    Stack n'accepte `maintenance` que sur un report `maintenance`, et n'y accepte
    que celui-là. Mélanger les deux vaut un `422 affected_resources is invalid`
    sur chaque update — constaté en production, en boucle, pendant une
    maintenance. Le niveau ne sert que quand le type est inconnu (report pas
    encore créé).
    """
    if report_type == MAINTENANCE:
        return BS_MAINTENANCE
    if report_type is None and level == MAINTENANCE:
        return BS_MAINTENANCE
    if service_status == "down":
        return BS_DOWNTIME
    if service_status == "degraded":
        return BS_DEGRADED
    return BS_RESOLVED

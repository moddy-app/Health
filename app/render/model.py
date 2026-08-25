"""Modèle intermédiaire du rendu Discord.

Le bot et le webhook envoient le même message par deux APIs incompatibles :
discord.py veut des objets `ui.*`, le webhook veut du JSON brut. Écrire deux
fois la mise en forme est la garantie qu'elles divergeront — d'où ce modèle,
seul point d'entrée des deux renderers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import colors, theme


def unix(value: str | None) -> int:
    """ISO-8601 -> timestamp unix, pour `<t:unix:F>`.

    Toujours un timestamp Discord, jamais une date formatée : chaque lecteur la
    voit alors dans son propre fuseau.
    """
    if not value:
        return int(datetime.now(tz=timezone.utc).timestamp())
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(datetime.now(tz=timezone.utc).timestamp())


@dataclass(frozen=True)
class IncidentUpdate:
    kind: str
    at: str | None
    message: str

    @property
    def kind_label(self) -> str:
        return theme.kind_label(self.kind)

    @property
    def timestamp(self) -> int:
        return unix(self.at)


@dataclass(frozen=True)
class IncidentPresentation:
    title: str
    level: str
    status: str
    type: str = "incident"
    affected: list[str] = field(default_factory=list)  # noms publics, déjà résolus
    url: str | None = None
    created_by: str = "Moddy Health Monitor"
    message: str = ""
    created_at: str | None = None
    updates: list[IncidentUpdate] = field(default_factory=list)
    # Déjà résolue par la configuration : le rendu ne sait pas qui prévenir.
    mentions: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == theme.RESOLVED

    @property
    def accent(self) -> int:
        return theme.accent(self.level, self.resolved)

    @property
    def emoji(self) -> str:
        """Icône du titre : l'état des services, pas l'avancement de l'incident.

        `On Going` et `Resolved` sont réservés à la ligne « Status: ». Le titre,
        lui, dit ce qui se passe : down, dégradé, maintenance, ou rétabli.
        """
        return theme.level_emoji(self.level, self.resolved)

    @property
    def status_emoji(self) -> str:
        return theme.status_style(self.status, self.type).emoji

    @property
    def status_label(self) -> str:
        return theme.status_style(self.status, self.type).label

    @classmethod
    def from_incident(
        cls,
        incident: dict,
        names: dict[str, str] | None = None,
        mentions: str = "",
    ) -> IncidentPresentation:
        names = names or {}
        return cls(
            mentions=mentions,
            title=incident.get("title") or "Incident",
            level=incident.get("level") or colors.MAJOR_OUTAGE,
            status=incident.get("status") or theme.OPEN,
            type=incident.get("type") or "incident",
            affected=[names.get(s, s) for s in incident.get("affected") or []],
            url=incident.get("url"),
            created_by=incident.get("created_by") or "Moddy Health Monitor",
            message=incident.get("message") or "",
            created_at=incident.get("created_at"),
            updates=[
                IncidentUpdate(
                    kind=update.get("kind") or "updated",
                    at=update.get("at"),
                    message=update.get("message") or "",
                )
                for update in incident.get("updates") or []
            ],
        )


@dataclass(frozen=True)
class ServiceLine:
    id: str
    name: str
    status: str
    since: str | None = None
    reported: str | None = None
    impacted_by: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return theme.service_label(self.status)


@dataclass(frozen=True)
class StatusPresentation:
    """État courant tel que l'affichent le sticky et la vue « Refresh »."""

    level: str
    updated_at: str | None
    services: list[ServiceLine] = field(default_factory=list)
    incident_title: str | None = None
    incident_url: str | None = None

    @property
    def headline(self) -> str:
        any_down = any(service.status == "down" for service in self.services)
        return theme.headline(self.level, any_down=any_down)

    @property
    def emoji(self) -> str:
        return theme.level_icon(self.level)

    @property
    def accent(self) -> int:
        return theme.accent(self.level, self.level == colors.OPERATIONAL)

    @property
    def timestamp(self) -> int:
        return unix(self.updated_at)

    @classmethod
    def from_public(cls, public: dict | None) -> StatusPresentation:
        """Construit la présentation depuis le payload de `/v1/status`.

        Le sticky lit ce payload dans Redis, jamais par un appel HTTP au propre
        process du monitor : ce serait un point de panne de plus, pour rien.
        """
        public = public or {}
        current = public.get("incident") or public.get("maintenance") or {}
        return cls(
            level=public.get("status") or colors.OPERATIONAL,
            updated_at=public.get("updated_at"),
            services=[
                ServiceLine(
                    id=service.get("id") or "",
                    name=service.get("name") or service.get("id") or "",
                    status=service.get("status") or "unknown",
                    since=service.get("since"),
                    reported=service.get("reported"),
                    impacted_by=list(service.get("impacted_by") or []),
                )
                for service in public.get("services") or []
            ],
            incident_title=current.get("title"),
            incident_url=current.get("url"),
        )

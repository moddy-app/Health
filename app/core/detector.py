"""Machine à états par service, seuils anti faux-positifs, sévérité agrégée."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .. import keys
from ..config import Settings
from ..render import colors
from ..state import Store
from ..util import age_seconds, iso, utcnow
from .impact import ImpactGraph

log = logging.getLogger("hm.detector")

OPERATIONAL = "operational"
DEGRADED = "degraded"
DOWN = "down"
UNKNOWN = "unknown"

# `status` déclaré par le service -> état cible.
_REPORTED = {"ok": OPERATIONAL, "degraded": DEGRADED, "down": DOWN}


@dataclass
class ServiceState:
    service: str
    status: str = UNKNOWN
    since: str = field(default_factory=iso)
    consecutive_failures: int = 0
    consecutive_ok: int = 0
    last_heartbeat_at: str | None = None
    # Dernier heartbeat reçu, servi tel quel au bouton « Refresh ».
    version: str | None = None
    uptime_s: int | None = None
    checks: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "status": self.status,
            "since": self.since,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_ok": self.consecutive_ok,
            "last_heartbeat_at": self.last_heartbeat_at,
            "version": self.version,
            "uptime_s": self.uptime_s,
            "checks": self.checks,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ServiceState:
        return cls(
            service=data["service"],
            status=data.get("status", UNKNOWN),
            since=data.get("since") or iso(),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            consecutive_ok=int(data.get("consecutive_ok", 0)),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            version=data.get("version"),
            uptime_s=data.get("uptime_s"),
            checks=data.get("checks") or {},
            meta=data.get("meta") or {},
        )


@dataclass
class Snapshot:
    level: str
    updated_at: str
    services: dict[str, ServiceState]
    # État après propagation d'impact, sur tous les services connus.
    effective: dict[str, str] = field(default_factory=dict)
    impacted_by: dict[str, list[str]] = field(default_factory=dict)
    transitions: list[tuple[str, str, str]] = field(default_factory=list)
    in_grace: bool = False

    @property
    def failing(self) -> list[str]:
        """Causes racines : ce que les heartbeats déclarent eux-mêmes."""
        return [s for s, st in self.services.items() if st.status in (DEGRADED, DOWN)]

    @property
    def affected(self) -> list[str]:
        """Causes racines *et* services dégradés par ricochet."""
        return [s for s, status in self.effective.items() if status in (DEGRADED, DOWN)]

    @property
    def collateral(self) -> list[str]:
        """Uniquement les dégradés par ricochet."""
        return [s for s in self.affected if s in self.impacted_by]

    def statuses(self) -> dict[str, str]:
        """États effectifs — c'est ce qu'on publie sur la status page."""
        return dict(self.effective)


class Detector:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self.states: dict[str, ServiceState] = {}
        self.impact = ImpactGraph(
            settings.hm_impact_map, settings.known_services, monitored=settings.services
        )
        self._started_monotonic = time.monotonic()
        self._started_at = utcnow()

    # ------------------------------------------------------------------
    @property
    def in_grace(self) -> bool:
        return (time.monotonic() - self._started_monotonic) < self._s.hm_startup_grace

    async def load(self) -> None:
        """Recharge l'état persisté. Un redéploiement ne repart pas de zéro."""
        for service in self._s.services:
            data = await self._store.get_json(keys.state(service))
            if isinstance(data, dict) and data.get("service"):
                self.states[service] = ServiceState.from_dict(data)
            else:
                self.states[service] = ServiceState(service=service)
        log.info(
            "détecteur prêt sur %d service(s), grace period de %ds",
            len(self.states),
            self._s.hm_startup_grace,
        )

    # ------------------------------------------------------------------
    async def _observe(self, service: str) -> tuple[str, dict | None]:
        """État brut observé + dernier heartbeat, si la clé n'a pas expiré."""
        payload = await self._store.get_json(keys.hb(service))
        if not isinstance(payload, dict):
            # Clé expirée, ou service qui n'a jamais émis.
            state = self.states.get(service)
            return (DOWN if state and state.last_heartbeat_at else UNKNOWN), None
        return _REPORTED.get(str(payload.get("status", "")).lower(), DOWN), payload

    def _silence_ok(self, state: ServiceState, has_heartbeat: bool) -> bool:
        """« minimum 60s de silence » — ne s'applique qu'au silence réel.

        Quand le service déclare lui-même `down` ou `degraded`, ses heartbeats
        arrivent : exiger 60s de silence n'alerterait jamais. Dans ce cas seul
        le seuil de cycles compte.
        """
        if has_heartbeat:
            return True
        reference = state.last_heartbeat_at
        silence = age_seconds(reference) if reference else (utcnow() - self._started_at).total_seconds()
        return (silence or 0) >= self._s.hm_min_silence

    async def run_cycle(self) -> Snapshot:
        transitions: list[tuple[str, str, str]] = []

        for service in self._s.services:
            state = self.states.setdefault(service, ServiceState(service=service))
            observed, payload = await self._observe(service)

            if payload is not None:
                state.last_heartbeat_at = payload.get("received_at") or iso()
                state.version = payload.get("version")
                state.uptime_s = payload.get("uptime_s")
                state.checks = payload.get("checks") or {}
                state.meta = payload.get("meta") or {}

            healthy = observed == OPERATIONAL
            if healthy:
                state.consecutive_ok += 1
                state.consecutive_failures = 0
            else:
                state.consecutive_failures += 1
                state.consecutive_ok = 0

            previous = state.status
            target = previous

            if healthy:
                if previous != OPERATIONAL and state.consecutive_ok >= self._s.hm_recovery_threshold:
                    target = OPERATIONAL
            else:
                # Un service jamais vu finit par compter comme `down` : sans ça,
                # un service qui n'a jamais démarré n'est que du silence.
                candidate = DOWN if observed == UNKNOWN else observed
                if (
                    candidate != previous
                    and state.consecutive_failures >= self._s.hm_failure_threshold
                    and self._silence_ok(state, payload is not None)
                ):
                    target = candidate

            if target != previous:
                state.status = target
                state.since = iso()
                transitions.append((service, previous, target))
                log.info("%s: %s -> %s", service, previous, target)

            await self._store.set_json(keys.state(service), state.to_dict())

        return self.current_snapshot(transitions=transitions)

    def current_snapshot(
        self, transitions: list[tuple[str, str, str]] | None = None
    ) -> Snapshot:
        """Vue immédiate, sans I/O — sert aussi de secours à `/v1/status`."""
        services = {s: self.states[s] for s in self._s.services if s in self.states}
        effective, impacted_by = self.impact.apply({s: st.status for s, st in services.items()})
        return Snapshot(
            level=self.aggregate(),
            updated_at=iso(),
            services=services,
            effective=effective,
            impacted_by=impacted_by,
            transitions=transitions or [],
            in_grace=self.in_grace,
        )

    # ------------------------------------------------------------------
    def aggregate(self) -> str:
        """Sévérité agrégée (§4).

        Calculée sur les états **observés**, pas sur les états propagés : « un
        service critique down » doit rester une affirmation exacte. Un état
        dérivé n'existe jamais sans qu'un `down` observé l'ait déclenché : la
        sévérité est donc déjà au moins `degraded` quand la propagation parle.

        `unknown` n'entre pas dans le calcul : tant que les seuils ne sont pas
        atteints, un service jamais vu n'est pas encore une panne.
        """
        critical = set(self._s.critical_services)
        down = {s for s, st in self.states.items() if st.status == DOWN}
        degraded = {s for s, st in self.states.items() if st.status == DEGRADED}
        critical_down = down & critical

        if critical and critical_down == critical:
            return colors.MAJOR_OUTAGE
        if critical_down:
            return colors.PARTIAL_OUTAGE
        if degraded or down:
            return colors.DEGRADED
        return colors.OPERATIONAL

    # ------------------------------------------------------------------
    def public_payload(self, snapshot: Snapshot, incident: dict | None) -> dict:
        """Réponse pré-calculée de `/v1/status`.

        Calculée dans la boucle de check, pas à la requête : l'endpoint ne fait
        que servir une clé Redis.
        """
        maintenance = incident if incident and incident.get("type") == "maintenance" else None
        active = None if maintenance else incident

        # L'ordre d'affichage vient de la configuration : le payload est la
        # seule liste ordonnée que lisent le sticky, le panneau de détail et le
        # dashboard.
        ordered = self._s.display_order(list(snapshot.services))

        return {
            "status": _public_status(snapshot.level, active),
            "updated_at": snapshot.updated_at,
            "services": [
                {
                    "id": service,
                    "name": self._s.display_name(service),
                    # Ce que vit l'utilisateur, propagation d'impact comprise.
                    "status": snapshot.effective.get(service, snapshot.services[service].status),
                    # Ce que le service dit de lui-même.
                    "reported": snapshot.services[service].status,
                    "impacted_by": snapshot.impacted_by.get(service, []),
                    "since": snapshot.services[service].since,
                }
                for service in ordered
            ],
            "incident": _public_incident(active),
            "maintenance": _public_incident(maintenance),
        }


def _public_status(observed: str, active: dict | None) -> str:
    """Le niveau affiché ne peut pas être moins sévère que l'incident actif.

    `aggregate()` ne connaît que les heartbeats : un incident ouvert à la main
    (`/status incident`) peut annoncer `degraded` alors que le service se
    déclare toujours `operational` de lui-même. Sans ce plancher, le bandeau
    du sticky dirait « All systems operational » juste au-dessus du titre de
    l'incident en cours — les deux lignes se contrediraient.
    """
    if not active:
        return observed
    level = active.get("level")
    if level and colors.SEVERITY_ORDER.get(level, 0) > colors.SEVERITY_ORDER.get(observed, 0):
        return level
    return observed


def _public_incident(incident: dict | None) -> dict | None:
    if not incident or incident.get("status") == "resolved":
        return None
    updates = incident.get("updates") or []
    last = updates[-1] if updates else None
    return {
        "id": incident.get("id"),
        "type": incident.get("type", "incident"),
        "level": incident.get("level"),
        "title": incident.get("title"),
        "message": incident.get("message"),
        "affected": incident.get("affected") or [],
        "started_at": incident.get("created_at"),
        "resolved_at": incident.get("resolved_at"),
        # La fenêtre planifiée d'une maintenance — absente d'un incident normal.
        "starts_at": incident.get("starts_at"),
        "ends_at": incident.get("ends_at"),
        "url": incident.get("url"),
        "updates_count": len(updates),
        "last_update": (
            {"at": last.get("at"), "message": last.get("message")} if last else None
        ),
    }

"""Cycle de vie des incidents : ouverture, updates, résolution, historique.

Plusieurs incidents peuvent être actifs à la fois (`hm:incident:active`, un
dictionnaire `{id: incident}`). Un nouveau service qui tombe n'enrichit un
incident existant que s'il lui est *relié* par le graphe d'impact
(`HM_IMPACT_MAP`) ou explicitement cité dans ses services affectés : un
service qui n'a rien à voir avec un incident en cours en ouvre un second,
géré indépendamment.
"""

from __future__ import annotations

import json
import logging

from .. import keys
from ..config import Settings
from ..integrations.betterstack import BetterStack, process_report
from ..render import colors
from ..state import Store
from ..util import age_seconds, incident_id, iso
from .detector import DEGRADED, DOWN, OPERATIONAL, Snapshot
from .impact import ImpactGraph
from .notifier import Notifier

log = logging.getLogger("hm.incident")

MONITOR = "Moddy Health Monitor"

TYPE_INCIDENT = "incident"
TYPE_MAINTENANCE = "maintenance"
TYPE_DEGRADED = "degraded_performance"


def _type_for(level: str) -> str:
    return TYPE_DEGRADED if level == colors.DEGRADED else TYPE_INCIDENT


def _bs_report_type(incident: dict) -> str:
    """Type du report Better Stack qui porte cet incident.

    Retenu à la création (`bs_report_type`) ; déduit du type de l'incident tant
    que le report n'existe pas. C'est lui, et pas le niveau, qui décide des
    états acceptés sur les ressources affectées.
    """
    stored = incident.get("bs_report_type")
    if stored:
        return str(stored)
    return TYPE_MAINTENANCE if incident.get("type") == TYPE_MAINTENANCE else "manual"


class IncidentManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        betterstack: BetterStack,
        notifier: Notifier,
        impact: ImpactGraph,
    ) -> None:
        self._s = settings
        self._store = store
        self._bs = betterstack
        self._notifier = notifier
        self._impact = impact

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------
    async def get_active_all(self) -> list[dict]:
        """Tous les incidents actifs, dans l'ordre où ils ont été ouverts."""
        data = await self._store.get_json(keys.INCIDENT_ACTIVE)
        if not isinstance(data, dict):
            return []
        return sorted(
            (v for v in data.values() if isinstance(v, dict)),
            key=lambda i: i.get("created_at") or "",
        )

    async def get_active(self, incident_id_: str) -> dict | None:
        data = await self._store.get_json(keys.INCIDENT_ACTIVE)
        if not isinstance(data, dict):
            return None
        incident = data.get(incident_id_)
        return incident if isinstance(incident, dict) else None

    async def _save(self, incident: dict) -> None:
        data = await self._store.get_json(keys.INCIDENT_ACTIVE)
        if not isinstance(data, dict):
            data = {}
        data[incident["id"]] = incident
        await self._store.set_json(keys.INCIDENT_ACTIVE, data)

    async def _archive(self, incident: dict) -> None:
        await self._store.rpush(
            keys.INCIDENT_HISTORY, json.dumps(incident, separators=(",", ":"))
        )
        await self._store.ltrim(keys.INCIDENT_HISTORY, -keys.INCIDENT_HISTORY_MAX, -1)
        data = await self._store.get_json(keys.INCIDENT_ACTIVE)
        if isinstance(data, dict):
            data.pop(incident["id"], None)
            await self._store.set_json(keys.INCIDENT_ACTIVE, data)

    async def history(self, limit: int = 20) -> list[dict]:
        raw = await self._store.lrange(keys.INCIDENT_HISTORY, -limit, -1)
        out: list[dict] = []
        for item in reversed(raw):
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out

    def _url_for(self, report_id: str | None) -> str | None:
        if not report_id:
            return None
        return f"{self._s.discord_status_page_url.rstrip('/')}/incident/{report_id}"

    # ------------------------------------------------------------------
    # Better Stack
    # ------------------------------------------------------------------
    async def _publish_betterstack(
        self,
        incident: dict,
        message: str,
        *,
        statuses: dict[str, str],
        notify: bool,
    ) -> None:
        """Crée le report s'il n'existe pas encore, sinon poste un update.

        Publié quel que soit le niveau — un service non-critique en `degraded`
        mérite la même visibilité qu'une panne majeure : la status page ne doit
        pas cacher un « petit truc » sous prétexte qu'il n'affecte rien de
        critique.
        """
        if not self._bs.enabled:
            return
        level = incident.get("level", colors.MAJOR_OUTAGE)
        report_type = _bs_report_type(incident)

        affected = incident.get("affected") or []
        resources = self._bs.resources_for(affected, statuses, level, report_type=report_type)
        if not resources:
            log.warning("aucune ressource Better Stack mappée pour %s", affected)
            return

        report_id = incident.get("bs_report_id")
        if report_id:
            await self._bs.post_update(
                report_id,
                message=message,
                affected_resources=resources,
                notify_subscribers=notify,
            )
            return

        report_id = await self._bs.create_report(
            title=incident.get("title") or "Incident",
            message=message,
            affected_resources=resources,
            report_type=report_type,
            notify_subscribers=notify,
            published_at=incident.get("created_at"),
            starts_at=incident.get("starts_at"),
            ends_at=incident.get("ends_at"),
        )
        if report_id:
            incident["bs_report_id"] = report_id
            # Le type du report conditionne l'état autorisé sur *tous* ses
            # updates : on le retient plutôt que de le redéduire à chaque fois.
            incident["bs_report_type"] = report_type
            incident["url"] = incident.get("url") or self._url_for(report_id)

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    async def open(
        self,
        *,
        title: str,
        message: str,
        level: str,
        affected: list[str],
        origin: str,
        author: str = MONITOR,
        type_: str | None = None,
        notify: bool = False,
        statuses: dict[str, str] | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        bs_report_id: str | None = None,
        bs_report_type: str | None = None,
        url: str | None = None,
        fingerprint: str | None = None,
        roots: list[str] | None = None,
        publish_betterstack: bool = True,
    ) -> dict:
        now = iso()
        incident: dict = {
            "id": incident_id(),
            "bs_report_id": bs_report_id,
            "bs_report_type": bs_report_type,
            "discord_message_id": None,
            "discord_channel_id": self._s.discord_status_channel_id,
            "discord_transport": None,
            "title": title,
            "message": message,
            "type": type_ or _type_for(level),
            "level": level,
            "origin": origin,
            "affected": list(affected),
            # Causes racines suivies par la détection auto — sert à décider,
            # cycle après cycle, si *cet* incident précis doit se résoudre.
            # Vide pour un incident manuel ou Better Stack : lui ne se résout
            # jamais tout seul.
            "roots": list(roots) if roots is not None else [],
            "status": "open",
            "created_by": author,
            "created_at": now,
            "resolved_at": None,
            "url": url or self._url_for(bs_report_id),
            "updates": [{"kind": "created", "at": now, "message": message, "author": author}],
            "state_fingerprint": fingerprint,
        }
        if starts_at:
            incident["starts_at"] = starts_at
        if ends_at:
            incident["ends_at"] = ends_at

        statuses = statuses or {s: DOWN for s in affected}
        if publish_betterstack:
            await self._publish_betterstack(incident, message, statuses=statuses, notify=notify)
        await self._save(incident)
        incident = await self._notifier.dispatch(incident)
        await self._save(incident)
        log.info("incident %s ouvert (%s, origine %s)", incident["id"], level, origin)
        return incident

    async def add_update(
        self,
        incident_id_: str,
        *,
        message: str,
        author: str = MONITOR,
        kind: str = "updated",
        level: str | None = None,
        affected: list[str] | None = None,
        roots: list[str] | None = None,
        notify: bool = False,
        statuses: dict[str, str] | None = None,
        publish_betterstack: bool = True,
        dedupe: bool = False,
        fingerprint: str | None = None,
    ) -> dict | None:
        incident = await self.get_active(incident_id_)
        if incident is None:
            log.warning("update demandé sur un incident %s introuvable", incident_id_)
            return None

        # Un update automatique qui ne dit rien de neuf n'en est pas un : sans
        # cette garde, une réconciliation qui se rouvre à chaque cycle reposte
        # le même texte toutes les 15s, sur Discord *et* sur la status page. Un
        # membre du staff, lui, a le droit de répéter : s'il resoumet le même
        # message, c'est qu'il le veut.
        if dedupe and _is_noop_update(incident, message, level, affected):
            log.debug("update identique au précédent ignoré (%s)", incident.get("id"))
            return incident

        if level:
            incident["level"] = level
            if incident.get("type") != TYPE_MAINTENANCE:
                incident["type"] = _type_for(level)
        if affected is not None:
            incident["affected"] = list(affected)
        if roots is not None:
            incident["roots"] = list(roots)

        incident["status"] = "updating"
        if fingerprint is not None:
            incident["state_fingerprint"] = fingerprint
        incident["updates"].append(
            {"kind": kind, "at": iso(), "message": message, "author": author}
        )

        if publish_betterstack:
            statuses = statuses or {s: DOWN for s in incident.get("affected") or []}
            await self._publish_betterstack(incident, message, statuses=statuses, notify=notify)

        await self._save(incident)
        incident = await self._notifier.dispatch(incident)
        await self._save(incident)
        return incident

    async def resolve(
        self,
        incident_id_: str,
        *,
        message: str,
        author: str = MONITOR,
        notify: bool = False,
        publish_betterstack: bool = True,
    ) -> dict | None:
        incident = await self.get_active(incident_id_)
        if incident is None:
            return None

        incident["status"] = "resolved"
        incident["resolved_at"] = iso()
        incident["updates"].append(
            {"kind": "resolved", "at": incident["resolved_at"], "message": message, "author": author}
        )

        report_id = incident.get("bs_report_id")
        if publish_betterstack and report_id and self._bs.enabled:
            # Pas d'endpoint `/resolve` : on poste un update dont chaque
            # ressource affectée porte `status: "resolved"`.
            await self._bs.resolve_report(
                report_id,
                message=message,
                services=incident.get("affected") or [],
                notify_subscribers=notify,
                report_type=_bs_report_type(incident),
            )
            if incident.get("type") == TYPE_MAINTENANCE:
                await self._close_bs_window(incident, report_id)

        incident = await self._notifier.dispatch(incident)
        await self._archive(incident)
        log.info("incident %s résolu", incident.get("id"))
        return incident

    async def _close_bs_window(self, incident: dict, report_id: str) -> None:
        """Ramène la fenêtre du report de maintenance à maintenant.

        Un report de maintenance n'est pas clos par un update — il n'accepte
        même pas `resolved`. C'est sa fenêtre qui parle : tant que `ends_at` est
        dans le futur, la status page continue d'annoncer une maintenance que le
        staff vient pourtant de clore ou d'annuler. Une maintenance annulée
        avant d'avoir commencé voit ses deux bornes ramenées : une fenêtre ne
        peut pas finir avant de commencer.
        """
        now = iso()
        fields = {
            field: now
            for field in ("starts_at", "ends_at")
            # `age_seconds` est négatif tant que la borne est à venir ; absente,
            # il n'y a rien à refermer.
            if (age_seconds(incident.get(field)) or 0) < 0
        }
        if not fields:
            return
        if await self._bs.patch_report(report_id, **fields):
            log.info("fenêtre de maintenance %s refermée à %s", report_id, now)
        else:
            log.warning("fenêtre de maintenance %s non refermée côté Better Stack", report_id)

    # ------------------------------------------------------------------
    # Détection automatique
    # ------------------------------------------------------------------
    async def reconcile(self, snapshot: Snapshot) -> None:
        """Aligne les incidents actifs sur l'état observé.

        Chaque groupe de causes racines *reliées entre elles* par le graphe
        d'impact (`HM_IMPACT_MAP`) est traité indépendamment : un service qui
        n'a rien à voir avec un incident déjà ouvert en déclenche un second,
        géré séparément — pas un update de plus sur le premier.
        """
        if snapshot.in_grace:
            return

        for service, _, new_status in snapshot.transitions:
            if new_status == OPERATIONAL:
                await self._notifier.reset(service)

        actives = await self.get_active_all()

        # Une maintenance n'est pas un incident. Pendant sa fenêtre, un service
        # qui tombe et qui remonte est l'objet même de l'opération : y empiler
        # « We are currently experiencing a service outage » dirait au public
        # le contraire de ce que le staff a annoncé. Toute maintenance active,
        # quels que soient ses propres services, met la détection auto en
        # pause — les incidents déjà ouverts, eux, restent gérés par le staff.
        still_open_maintenance = False
        for incident in actives:
            if incident.get("type") == TYPE_MAINTENANCE and not await self._close_expired_maintenance(incident):
                still_open_maintenance = True
        if still_open_maintenance:
            return

        auto_incidents = [i for i in actives if i.get("type") != TYPE_MAINTENANCE and i.get("origin") == "auto"]
        manual_incidents = [i for i in actives if i.get("type") != TYPE_MAINTENANCE and i.get("origin") != "auto"]

        root_all = list(snapshot.failing)
        statuses = snapshot.statuses()

        # Un incident auto se résout dès que plus aucune de ses causes
        # racines ne dure — indépendamment du sort des autres incidents.
        for incident in auto_incidents:
            roots = set(incident.get("roots") or [])
            if not (roots & set(root_all)):
                await self.resolve(incident["id"], message="All systems are operational again.")

        if not root_all:
            return

        for cluster in self._clusters(root_all):
            # Ordonnés comme la configuration (`HM_SERVICES`), jamais
            # alphabétiquement : c'est cet ordre que lisent titres et messages.
            roots_ordered = [s for s in root_all if s in cluster]
            collateral_set = {
                target for source in cluster for target in self._impact.impacts(source)
            } & set(snapshot.affected) - cluster
            collateral_ordered = [s for s in snapshot.affected if s in collateral_set]
            affected = [s for s in snapshot.affected if s in cluster or s in collateral_set]
            level = _cluster_level(cluster, statuses, self._s.critical_services)
            fingerprint = _fingerprint(level, {s: statuses.get(s, "unknown") for s in affected})

            matched = next(
                (i for i in auto_incidents if set(i.get("roots") or []) & cluster), None
            ) or next(
                (i for i in manual_incidents if set(i.get("affected") or []) & cluster), None
            )

            if matched is None:
                if not await self._allow_any(roots_ordered, statuses):
                    continue
                await self.open(
                    title=_auto_title(level, roots_ordered, self._s),
                    message=_auto_message(level, roots_ordered, collateral_ordered, self._s),
                    level=level,
                    affected=affected,
                    origin="auto",
                    notify=level == colors.MAJOR_OUTAGE,
                    statuses=statuses,
                    fingerprint=fingerprint,
                    roots=roots_ordered,
                )
                continue

            if matched.get("state_fingerprint") == fingerprint:
                continue

            is_auto = matched.get("origin") == "auto"
            target_level = level if is_auto else matched.get("level")
            merged_affected = list(matched.get("affected") or [])
            merged_affected += [s for s in affected if s not in merged_affected]
            await self.add_update(
                matched["id"],
                message=_auto_message(level, roots_ordered, collateral_ordered, self._s),
                level=target_level,
                affected=merged_affected,
                roots=roots_ordered if is_auto else None,
                notify=False,
                statuses=statuses,
                dedupe=True,
                fingerprint=fingerprint,
            )

    def _clusters(self, roots: list[str]) -> list[set[str]]:
        """Regroupe les causes racines reliées entre elles par le graphe d'impact.

        Deux services qui tombent en même temps ne sont la même panne que s'ils
        sont connectés — directement ou en chaîne — par `HM_IMPACT_MAP`. Sans
        ça, deux services sans aucun rapport (le dashboard et un service tiers,
        par exemple) fusionneraient dans le même incident au premier cycle où
        ils tombent ensemble.
        """
        pool = set(roots)
        adjacency: dict[str, set[str]] = {r: set() for r in pool}
        for r in pool:
            for target in self._impact.impacts(r):
                if target in pool:
                    adjacency[r].add(target)
                    adjacency[target].add(r)

        seen: set[str] = set()
        clusters: list[set[str]] = []
        for start in sorted(pool):
            if start in seen:
                continue
            cluster: set[str] = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in cluster:
                    continue
                cluster.add(node)
                stack.extend(adjacency[node] - cluster)
            seen |= cluster
            clusters.append(cluster)
        return clusters

    async def _close_expired_maintenance(self, incident: dict) -> bool:
        """Clôt une maintenance dont la fenêtre est passée. Renvoie `True` si clos.

        Tant que `ends_at` n'est pas dépassé, la maintenance couvre la
        détection : le monitor se tait. Une fois la fenêtre finie, elle ne
        couvre plus rien — la laisser ouverte, c'est rendre le monitor aveugle à
        la première vraie panne qui suit. Sans `ends_at` (maintenance adoptée
        depuis Better Stack), impossible de dire qu'elle est finie : elle reste
        au staff.
        """
        ends_at = incident.get("ends_at")
        if not ends_at:
            return False
        if (age_seconds(ends_at) or 0) <= 0:
            return False
        await self.resolve(incident["id"], message="The scheduled maintenance window has ended.")
        log.info("maintenance %s close : fenêtre terminée", incident.get("id"))
        return True

    async def _allow_any(self, services: list[str], statuses: dict[str, str]) -> bool:
        """Rate-limit : au moins un service doit être hors de sa fenêtre de 5 min."""
        allowed = False
        for service in services:
            if await self._notifier.allow(service, statuses.get(service, DOWN)):
                allowed = True
        return allowed

    # ------------------------------------------------------------------
    # Commandes du staff (bot -> monitor)
    # ------------------------------------------------------------------
    async def handle_command(self, action: str, payload: dict) -> dict | None:
        author = payload.get("author") or "Staff"
        notify = bool(payload.get("notify"))

        if action == "incident.create":
            level = payload.get("level") or colors.PARTIAL_OUTAGE
            affected = payload.get("affected") or []
            # L'état de chaque service vient du panneau de sévérité. Sans lui,
            # tout service affecté partirait en `downtime` sur la status page,
            # y compris ceux qui ne font que ralentir.
            statuses = payload.get("statuses") or None
            # « Create » ouvre toujours un incident distinct — plusieurs
            # peuvent être actifs à la fois, gérés indépendamment. Enrichir un
            # incident existant se fait via `/status update`, jamais ici.
            return await self.open(
                title=payload.get("title") or _auto_title(level, affected, self._s),
                message=payload.get("message") or "",
                level=level,
                affected=affected,
                origin="discord",
                author=author,
                notify=notify,
                statuses=statuses,
            )

        if action == "incident.update":
            incident_id_ = payload.get("incident_id")
            if not incident_id_:
                log.warning("incident.update sans incident_id")
                return None
            return await self.add_update(
                incident_id_,
                message=payload.get("message") or "",
                author=author,
                level=payload.get("level"),
                affected=payload.get("affected"),
                notify=notify,
            )

        if action == "incident.resolve":
            incident_id_ = payload.get("incident_id")
            if not incident_id_:
                log.warning("incident.resolve sans incident_id")
                return None
            return await self.resolve(
                incident_id_,
                message=payload.get("message") or "This incident has been resolved.",
                author=author,
                notify=notify,
            )

        if action == "maintenance.create":
            ends_at = payload.get("ends_at")
            if not ends_at:
                log.error("maintenance sans ends_at refusée")
                return None
            return await self.open(
                title=payload.get("title") or "Scheduled Maintenance",
                message=payload.get("message") or "",
                level=colors.MAINTENANCE,
                affected=payload.get("affected") or [],
                origin="discord",
                author=author,
                type_=TYPE_MAINTENANCE,
                notify=notify,
                starts_at=payload.get("starts_at") or iso(),
                ends_at=ends_at,
            )

        log.warning("commande inconnue: %s", action)
        return None

    # ------------------------------------------------------------------
    # Retour Better Stack (webhook + poll)
    # ------------------------------------------------------------------
    async def handle_bs_payload(self, payload: dict) -> None:
        """Traite un event webhook Better Stack (§6, anti-boucle)."""
        await self._store.set(keys.BS_LAST_EVENT, iso())

        node = payload.get("incident") or payload.get("maintenance")
        if not node:
            component = payload.get("component_update")
            if component:
                log.info(
                    "component_update Better Stack: %s -> %s",
                    (payload.get("component") or {}).get("name"),
                    component.get("new_status"),
                )
            return

        updates = [_normalize_update(u) for u in reversed(node.get("incident_updates") or [])]
        report = {
            "id": node.get("id"),
            "title": node.get("name"),
            "url": node.get("shortlink"),
            "report_type": "maintenance" if payload.get("event_type") == "maintenance" else "manual",
            "starts_at": node.get("starts_at"),
            "ends_at": node.get("ends_at"),
            "affected_resources": node.get("affected_resources") or [],
        }
        await process_report(
            self._bs,
            report,
            updates,
            on_owned_update=self._relay_update,
            on_foreign_incident=self._adopt,
        )

    async def sync_updates(self) -> list[dict]:
        """Recharge les updates de chaque incident actif depuis Better Stack.

        Une update *éditée* là-bas (texte corrigé sur un update déjà posté) ne
        déclenche jamais le webhook — l'anti-boucle ne marque que les ID
        *nouveaux* (`hm:bs:seen_updates`) — donc une correction n'atteignait
        jamais le message Discord. Cette commande resynchronise l'historique
        affiché sur celui de Better Stack, à la main, plutôt que d'attendre
        une update qui ne viendra pas. Porte sur tous les incidents actifs
        adossés à un report, pas un seul : ils sont indépendants.
        """
        actives = [i for i in await self.get_active_all() if i.get("bs_report_id")]
        if not actives:
            return []

        snapshot = await self._bs.poll_index()
        if snapshot is None:
            return []
        reports = {str(r.get("id")): r for r in snapshot.reports}

        synced: list[dict] = []
        for incident in actives:
            report = reports.get(str(incident.get("bs_report_id")))
            updates = sorted(
                (report.get("updates") or []) if report else [],
                key=lambda u: u.get("published_at") or "",
            )
            if not updates:
                continue

            incident["updates"] = [
                {
                    "kind": "created" if index == 0 else "updated",
                    "at": update.get("published_at"),
                    "message": update.get("message") or "",
                    "author": "Better Stack",
                }
                for index, update in enumerate(updates)
            ]
            await self._save(incident)
            if not await self._notifier.re_render(incident):
                log.warning(
                    "resync %s : réédition du message Discord impossible", incident.get("id")
                )
            synced.append(incident)
        return synced

    async def reconcile_betterstack(self) -> None:
        """Filet de sécurité : rattrape ce qu'un webhook manqué aurait perdu."""
        snapshot = await self._bs.poll_index()
        if snapshot is None:
            return

        # `index.json` porte tout l'historique de la status page. Au premier
        # poll — déploiement neuf, ou Redis vidé — `hm:bs:seen_updates` est vide
        # et chaque update d'archive passe pour nouveau : sans amorçage, le
        # monitor adopte un incident résolu il y a des mois et le rejoue.
        bootstrap = not await self._store.get(keys.BS_CURSOR)
        if bootstrap:
            log.info("premier poll Better Stack : l'historique est pris pour acquis")

        for report in snapshot.reports:
            updates = [
                {
                    "id": u["id"],
                    "message": u["message"],
                    "at": u.get("published_at"),
                    "affected_resources": u.get("affected_resources") or [],
                }
                for u in report.get("updates") or []
            ]
            if not updates:
                continue
            stale = bootstrap or self._too_old_to_adopt(report, updates)
            await process_report(
                self._bs,
                report,
                updates,
                # Un report trop vieux n'est que marqué vu : on ne le rejoue pas,
                # mais on ne le reverra pas non plus au poll suivant.
                on_owned_update=_ignore if stale else self._relay_update,
                on_foreign_incident=_ignore if stale else self._adopt,
            )
        await self._store.set(keys.BS_CURSOR, iso())

    def _too_old_to_adopt(self, report: dict, updates: list[dict]) -> bool:
        """Un incident dont le dernier mot remonte à des heures est de l'archive.

        `ends_at` reste `null` même sur un report résolu : impossible de s'y
        fier pour savoir s'il est clos. L'âge du dernier update, lui, ne ment
        pas.
        """
        # L'ordre des updates d'`index.json` n'est pas garanti : on prend le plus
        # récent, pas le dernier de la liste.
        ages = [
            age
            for age in (age_seconds(u.get("at")) for u in updates)
            if age is not None
        ]
        if not ages:
            ages = [age] if (age := age_seconds(report.get("updated_at"))) is not None else []
        if not ages:
            return True
        age = min(ages)
        if age > self._s.hm_bs_adopt_max_age:
            log.info(
                "report Better Stack %s ignoré : dernier update il y a %ds",
                report.get("id"),
                int(age),
            )
            return True
        return False

    async def _find_by_report(self, report_id: str) -> dict | None:
        for incident in await self.get_active_all():
            if str(incident.get("bs_report_id")) == report_id:
                return incident
        return None

    async def _relay_update(self, report: dict, update: dict) -> None:
        """Un update posté à la main sur *notre* incident : on le relaie."""
        active = await self._find_by_report(str(report.get("id")))
        if active is None:
            return
        await self.add_update(
            active["id"],
            message=update.get("message") or "",
            author=update.get("author") or "Better Stack",
            notify=False,
            # L'update existe déjà côté Better Stack : le republier bouclerait.
            publish_betterstack=False,
        )

    async def _adopt(self, report: dict, update: dict) -> None:
        """Incident créé hors du monitor (staff ou monitor Better Stack).

        Plusieurs incidents pouvant être actifs à la fois, un report Better
        Stack qui n'est *pas le nôtre* est toujours adopté comme un nouvel
        incident indépendant — il n'a par construction rien à voir avec ceux
        déjà suivis localement.
        """
        report_id = str(report.get("id"))

        active = await self._find_by_report(report_id)
        if active is not None:
            await self._relay_update(report, update)
            return

        # `report_type: automatic` = incident créé par un monitor Better Stack,
        # troisième origine distincte de manual/maintenance.
        is_maintenance = report.get("report_type") == "maintenance"

        # L'incident n'existe que côté Better Stack : ses services affectés sont
        # des ressources de status page, qu'il faut retraduire. Sans ça, le
        # message Discord annonçait « Affected services: — ».
        affected, statuses = self._bs.services_for(
            update.get("affected_resources") or report.get("affected_resources") or []
        )
        level = colors.MAINTENANCE if is_maintenance else _level_for(statuses)

        await self.open(
            title=report.get("title") or "Incident",
            message=update.get("message") or "",
            level=level,
            affected=affected,
            origin="betterstack",
            author="Better Stack",
            type_=TYPE_MAINTENANCE if is_maintenance else TYPE_INCIDENT,
            bs_report_id=report_id,
            # `automatic` (incident créé par un monitor Better Stack) suit les
            # mêmes règles d'états que `manual` : seul `maintenance` diffère.
            bs_report_type=TYPE_MAINTENANCE if is_maintenance else "manual",
            url=report.get("url") or self._url_for(report_id),
            statuses=statuses,
            # Le report existe déjà là-bas : lui renvoyer son propre message
            # ouvrirait une boucle.
            publish_betterstack=False,
        )

    async def webhook_seems_dead(self) -> bool:
        """Après 10 échecs de livraison, Better Stack coupe la souscription."""
        last = await self._store.get(keys.BS_LAST_EVENT)
        if not last:
            return False
        return (age_seconds(last) or 0) > self._s.hm_bs_webhook_silence_alert


# ----------------------------------------------------------------------
# Textes générés
# ----------------------------------------------------------------------
def _names(services: list[str], settings: Settings) -> str:
    if not services:
        return "several services"
    labels = [settings.display_name(s) for s in services]
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " & " + labels[-1]


def _auto_title(level: str, services: list[str], settings: Settings) -> str:
    label = _names(services, settings)
    if level == colors.MAJOR_OUTAGE:
        return f"Major Outage – {label} Unavailable"
    if level == colors.PARTIAL_OUTAGE:
        return f"Partial Outage – {label} Unavailable"
    if level == colors.MAINTENANCE:
        return f"Scheduled Maintenance – {label}"
    return f"Degraded Performance – {label}"


def _auto_message(
    level: str, services: list[str], collateral: list[str], settings: Settings
) -> str:
    label = _names(services, settings)
    if level == colors.DEGRADED:
        message = f"We are seeing degraded performance on {label}. We are looking into it."
    else:
        message = (
            f"We are currently experiencing a service outage affecting {label}. "
            "Our team has been alerted and is investigating."
        )
    if collateral:
        # Nommer les dégâts collatéraux plutôt que de les mélanger à la cause.
        message += f" {_names(collateral, settings)} may be degraded as a result."
    return message


async def _ignore(report: dict, update: dict) -> None:
    """Marquer vu sans rien faire — voir `reconcile_betterstack`."""
    return None


def _cluster_level(cluster: set[str], statuses: dict[str, str], critical: list[str]) -> str:
    """Sévérité d'un groupe de causes racines reliées entre elles.

    Reprend `Detector.aggregate()`, mais scopée au cluster : `major_outage`
    n'est mérité que si le cluster couvre *tous* les services critiques,
    `partial_outage` s'il n'en couvre qu'une partie.
    """
    critical_set = set(critical)
    down = {s for s in cluster if statuses.get(s) == DOWN}
    degraded = {s for s in cluster if statuses.get(s) == DEGRADED}
    critical_down = down & critical_set

    if critical_set and critical_down == critical_set:
        return colors.MAJOR_OUTAGE
    if critical_down:
        return colors.PARTIAL_OUTAGE
    if degraded or down:
        return colors.DEGRADED
    return colors.OPERATIONAL


def _level_for(statuses: dict[str, str]) -> str:
    """Sévérité déduite de l'état des ressources d'un incident adopté.

    Faute de ressource lisible, on reste sur `partial_outage` : un incident
    publié sur la status page n'est jamais anodin.
    """
    values = set(statuses.values())
    if DOWN in values:
        return colors.PARTIAL_OUTAGE
    if "degraded" in values:
        return colors.DEGRADED
    return colors.PARTIAL_OUTAGE


def _fingerprint(level: str, effective: dict[str, str]) -> str:
    """Signature de l'état observé : le niveau global, et l'état de chaque service.

    Un update d'incident ne se justifie que si cette signature bouge — un
    service qui tombe, un service qui revient, une sévérité qui change. Tant
    qu'elle est stable, il n'y a rien de neuf à dire, et le redire toutes les
    15 secondes noie l'incident sous ses propres updates.
    """
    services = ",".join(f"{name}={status}" for name, status in sorted(effective.items()))
    return f"{level}|{services}"


def _is_noop_update(
    incident: dict, message: str, level: str | None, affected: list[str] | None
) -> bool:
    """Le même texte, le même niveau, les mêmes services : rien à publier."""
    updates = incident.get("updates") or []
    if not updates or updates[-1].get("message") != message:
        return False
    if level and level != incident.get("level"):
        return False
    if affected is not None and list(affected) != list(incident.get("affected") or []):
        return False
    return True


def _normalize_update(update: dict) -> dict:
    """Uniformise les updates venus du webhook et ceux venus d'`index.json`."""
    return {
        "id": update.get("id"),
        "message": update.get("body") or update.get("message") or "",
        "at": update.get("created_at") or update.get("published_at") or iso(),
        "affected_resources": update.get("affected_resources")
        or update.get("affected_components")
        or [],
    }

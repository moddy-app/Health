"""Better Stack : écriture API v2, poll `index.json`, anti-boucle.

Rappel : les status reports d'une status page vivent sur
`/api/v2/status-pages/{spid}/status-reports`. `/api/v3/incidents` est l'API
Incident Management (on-call, escalade), un objet différent qui n'alimente pas
la page. Il n'existe pas d'endpoint `/resolve` : on résout en postant un status
update dont les `affected_resources` portent `status: "resolved"`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import keys
from ..config import Settings
from ..render import colors
from ..state import Store

log = logging.getLogger("hm.betterstack")

# États d'une ressource Better Stack -> vocabulaire du monitor.
_SERVICE_STATUS = {
    colors.BS_DOWNTIME: "down",
    colors.BS_DEGRADED: "degraded",
    colors.BS_MAINTENANCE: "maintenance",
    colors.BS_RESOLVED: "operational",
}

# Backoff exponentiel plafonné à 5 min (§11).
_BACKOFF = [2, 4, 8, 16, 32, 64, 128, 300]
_MAX_ATTEMPTS = 5


@dataclass
class IndexSnapshot:
    """Vue parsée de `index.json`."""

    aggregate_state: str = "operational"
    reports: list[dict] = field(default_factory=list)
    resources: dict[str, dict] = field(default_factory=dict)


class BetterStack:
    def __init__(
        self, settings: Settings, store: Store, client: httpx.AsyncClient | None = None
    ) -> None:
        self._s = settings
        self._store = store
        self._client = client
        self._owns_client = client is None

    @property
    def enabled(self) -> bool:
        return self._s.betterstack_enabled

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        if not self.enabled:
            log.debug("better stack non configuré, %s %s ignoré", method, path)
            return None

        url = f"{self._s.betterstack_api_base.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {self._s.betterstack_token}"}
        client = await self._http()

        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.request(method, url, json=payload, headers=headers)
            except Exception as exc:
                log.warning("BS %s %s réseau KO (%s): %s", method, path, attempt + 1, exc)
                await asyncio.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                continue

            if response.status_code == 429 or response.status_code >= 500:
                log.warning("BS %s %s -> %s, retry", method, path, response.status_code)
                await asyncio.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
                continue

            if not response.is_success:
                log.error("BS %s %s -> %s: %s", method, path, response.status_code, response.text[:400])
                return None

            try:
                return response.json()
            except Exception:
                return {}
        log.error("BS %s %s abandonné après %s tentatives", method, path, _MAX_ATTEMPTS)
        return None

    def _page_path(self, suffix: str = "") -> str:
        return f"/status-pages/{self._s.betterstack_status_page_id}/status-reports{suffix}"

    # ------------------------------------------------------------------
    # Ressources affectées
    # ------------------------------------------------------------------
    def resources_for(
        self,
        services: list[str],
        statuses: dict[str, str],
        level: str,
        *,
        report_type: str | None = None,
    ) -> list[dict]:
        """Ne marque que les ressources réellement affectées et mappées.

        Un service absent de `HM_BS_RESOURCE_MAP` est simplement ignoré : mieux
        vaut ne rien publier que salir la barre journalière d'une ressource qui
        va bien.

        `report_type` est celui du report visé : c'est lui qui autorise — ou
        interdit — l'état `maintenance` (voir `colors.bs_status_for`).
        """
        mapping = self._s.bs_resource_map
        out: list[dict] = []
        for service in services:
            resource_id = mapping.get(service)
            if not resource_id:
                log.debug("service %s sans resource_id, ignoré côté Better Stack", service)
                continue
            out.append(
                {
                    "status_page_resource_id": resource_id,
                    "status": colors.bs_status_for(
                        level, statuses.get(service, "down"), report_type
                    ),
                }
            )
        return out

    def services_for(self, resources: list[dict]) -> tuple[list[str], dict[str, str]]:
        """Chemin inverse de `resources_for` : ressources -> services, et leur état.

        Un incident créé à la main sur Better Stack ne connaît que des
        ressources ; sans cette traduction, le monitor le republie avec
        « Affected services: — ».
        """
        reverse = {str(rid): service for service, rid in self._s.bs_resource_map.items()}
        services: list[str] = []
        statuses: dict[str, str] = {}
        for item in resources or []:
            if not isinstance(item, dict):
                continue
            rid = str(
                item.get("status_page_resource_id")
                or item.get("status_page_resource")
                or item.get("id")
                or ""
            )
            service = reverse.get(rid)
            if not service or service in services:
                continue
            services.append(service)
            statuses[service] = _SERVICE_STATUS.get(str(item.get("status")), "down")
        return services, statuses

    # ------------------------------------------------------------------
    # Anti-boucle
    # ------------------------------------------------------------------
    async def mark_owned(self, report_id: str) -> None:
        await self._store.sadd(keys.BS_OWNED, str(report_id))

    async def is_owned(self, report_id: str) -> bool:
        return await self._store.sismember(keys.BS_OWNED, str(report_id))

    async def mark_update_seen(self, update_id: str) -> None:
        await self._store.sadd(keys.BS_SEEN_UPDATES, str(update_id))

    async def update_seen(self, update_id: str) -> bool:
        return await self._store.sismember(keys.BS_SEEN_UPDATES, str(update_id))

    # ------------------------------------------------------------------
    # Écriture
    # ------------------------------------------------------------------
    async def create_report(
        self,
        *,
        title: str,
        message: str,
        affected_resources: list[dict],
        report_type: str = "manual",
        notify_subscribers: bool = False,
        published_at: str | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
    ) -> str | None:
        payload: dict[str, Any] = {
            "title": title,
            "message": message,
            "report_type": report_type,
            "notify_subscribers": notify_subscribers,
        }
        if affected_resources:
            payload["affected_resources"] = affected_resources
        # `published_at` permet de republier un incident avec son horodatage
        # d'origine après une panne (file de rattrapage).
        if published_at:
            payload["published_at"] = published_at
        if starts_at:
            payload["starts_at"] = starts_at
        if ends_at:
            payload["ends_at"] = ends_at
        if report_type == "maintenance" and not ends_at:
            log.error("maintenance sans ends_at : Better Stack refusera le report")
            return None

        data = await self._request("POST", self._page_path(), payload)
        report_id = _extract_id(data)
        if report_id:
            # Avant tout autre traitement, sinon on perd la course avec le
            # webhook entrant qui nous renverra notre propre écriture.
            await self.mark_owned(report_id)
            for update_id in _extract_update_ids(data):
                await self.mark_update_seen(update_id)
            log.info("status report %s créé", report_id)
        return report_id

    async def post_update(
        self,
        report_id: str,
        *,
        message: str,
        affected_resources: list[dict],
        notify_subscribers: bool = False,
    ) -> str | None:
        """`affected_resources` est obligatoire sur les updates."""
        if not affected_resources:
            log.warning("update sur %s sans ressource affectée, ignoré", report_id)
            return None
        data = await self._request(
            "POST",
            self._page_path(f"/{report_id}/status-updates"),
            {
                "message": message,
                "notify_subscribers": notify_subscribers,
                "affected_resources": affected_resources,
            },
        )
        update_id = _extract_id(data)
        if update_id:
            # Dès la réponse 201, pour que l'écho webhook soit ignoré.
            await self.mark_update_seen(update_id)
        return update_id

    async def resolve_report(
        self,
        report_id: str,
        *,
        message: str,
        services: list[str],
        notify_subscribers: bool = False,
        report_type: str | None = None,
    ) -> str | None:
        """Résolution = un update avec `status: resolved` sur chaque ressource.

        Sauf sur un report de maintenance : Better Stack n'y accepte que
        `maintenance`. Une maintenance se termine par sa fenêtre, pas par un
        `resolved` — que l'API refuserait.
        """
        resources = self.resources_for(
            services,
            {s: "operational" for s in services},
            colors.OPERATIONAL,
            report_type=report_type,
        )
        return await self.post_update(
            report_id,
            message=message,
            affected_resources=resources,
            notify_subscribers=notify_subscribers,
        )

    async def patch_report(self, report_id: str, **fields: Any) -> bool:
        data = await self._request("PATCH", self._page_path(f"/{report_id}"), fields)
        return data is not None

    # ------------------------------------------------------------------
    # Lecture — poll index.json
    # ------------------------------------------------------------------
    async def poll_index(self) -> IndexSnapshot | None:
        if not self._s.betterstack_index_url:
            return None
        client = await self._http()
        try:
            response = await client.get(self._s.betterstack_index_url)
            response.raise_for_status()
            return parse_index(response.json())
        except Exception as exc:
            log.warning("poll index.json échoué: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Heartbeat sortant — surveiller le surveillant
    # ------------------------------------------------------------------
    async def self_heartbeat(self, ok: bool = True) -> bool:
        url = self._s.hm_self_heartbeat_url
        if not url:
            return False
        target = url.rstrip("/") + ("" if ok else "/fail")
        try:
            client = await self._http()
            response = await client.get(target, timeout=10)
            return response.is_success
        except Exception as exc:
            log.warning("self-heartbeat échoué: %s", exc)
            return False


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------
def _extract_id(data: dict | None) -> str | None:
    if not data:
        return None
    node = data.get("data")
    if isinstance(node, dict) and node.get("id") is not None:
        return str(node["id"])
    return None


def _extract_update_ids(data: dict | None) -> list[str]:
    """IDs des status_update créés en même temps que le report."""
    if not data:
        return []
    node = data.get("data")
    if not isinstance(node, dict):
        return []
    rel = (node.get("relationships") or {}).get("status_updates") or {}
    items = rel.get("data")
    if isinstance(items, list):
        return [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id") is not None]
    return []


def parse_index(payload: dict) -> IndexSnapshot:
    """Parse `index.json`.

    Trois pièges confirmés :
      1. `ends_at` reste `null` même sur les reports résolus — on ne s'y fie
         jamais, l'état vient de `aggregate_state` et des updates.
      2. `report_type` peut valoir `automatic` (incidents créés par les
         monitors Better Stack eux-mêmes), pas seulement manual/maintenance.
      3. `availability` (monitors) et `status_history` (reports) sont deux
         sources de vérité distinctes : on ne les croise pas.
    """
    snapshot = IndexSnapshot()
    data = payload.get("data") or {}
    snapshot.aggregate_state = ((data.get("attributes") or {}).get("aggregate_state")) or "operational"

    included = payload.get("included") or []
    updates_by_id: dict[str, dict] = {}
    reports: dict[str, dict] = {}

    for item in included:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        attrs = item.get("attributes") or {}
        item_id = str(item.get("id"))

        if kind == "status_update":
            updates_by_id[item_id] = {
                "id": item_id,
                "message": attrs.get("message") or attrs.get("body") or "",
                "published_at": attrs.get("published_at") or attrs.get("created_at"),
                "affected_resources": attrs.get("affected_resources") or [],
            }
        elif kind == "status_report":
            reports[item_id] = {
                "id": item_id,
                "title": attrs.get("title"),
                "report_type": attrs.get("report_type") or "manual",
                "published_at": attrs.get("published_at"),
                "starts_at": attrs.get("starts_at"),
                "updated_at": attrs.get("updated_at") or attrs.get("published_at"),
                "aggregate_state": attrs.get("aggregate_state"),
                "update_ids": [
                    str(u.get("id"))
                    for u in ((item.get("relationships") or {}).get("status_updates") or {}).get(
                        "data"
                    )
                    or []
                    if isinstance(u, dict) and u.get("id") is not None
                ],
            }
        elif kind == "status_page_resource":
            snapshot.resources[item_id] = {
                "id": item_id,
                "name": (attrs.get("public_name") or attrs.get("name") or "").strip(),
                # operational | degraded | downtime | maintenance | not_monitored
                "status": attrs.get("status") or "not_monitored",
            }

    for report in reports.values():
        report["updates"] = [
            updates_by_id[uid] for uid in report.pop("update_ids", []) if uid in updates_by_id
        ]
        snapshot.reports.append(report)

    return snapshot


# ----------------------------------------------------------------------
# Anti-boucle — chemin commun webhook / poll
# ----------------------------------------------------------------------
OnUpdate = Callable[[dict, dict], Awaitable[None]]
OnForeign = Callable[[dict, dict], Awaitable[None]]


async def process_report(
    bs: BetterStack,
    report: dict,
    updates: list[dict],
    *,
    on_owned_update: OnUpdate,
    on_foreign_incident: OnForeign,
) -> None:
    """Applique la règle d'anti-boucle à un report et ses updates.

    Les updates sont traités du plus ancien au plus récent. Chaque ID vu est
    marqué avant tout traitement : ça couvre à la fois l'écho de nos propres
    écritures et les livraisons multiples dues aux retries webhook.
    """
    report_id = str(report.get("id"))
    owned = await bs.is_owned(report_id)

    for update in updates:
        update_id = str(update.get("id"))
        if not update_id or update_id == "None":
            continue
        if await bs.update_seen(update_id):
            continue
        await bs.mark_update_seen(update_id)
        try:
            if owned:
                await on_owned_update(report, update)
            else:
                await on_foreign_incident(report, update)
        except Exception:
            log.exception("traitement de l'update %s échoué", update_id)

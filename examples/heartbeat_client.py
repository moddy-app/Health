"""Client de heartbeat à coller dans chaque service Moddy.

Une task asyncio isolée, fire-and-forget, timeout 5s, jamais bloquante : un
échec ne fait que logger. Le service ne doit jamais tomber parce que le monitor
est indisponible.

    from examples.heartbeat_client import HeartbeatClient

    hb = HeartbeatClient("moddy-api", url=HM_URL, token=TOKEN, build=build_checks)
    hb.start()
    ...
    await hb.stop()
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable

import httpx

log = logging.getLogger("heartbeat")

# 20s d'intervalle recommandé, donc TTL de 60s côté monitor (interval x 3).
INTERVAL = 20
TIMEOUT = 5


class HeartbeatClient:
    def __init__(
        self,
        service: str,
        *,
        url: str | None = None,
        token: str | None = None,
        version: str = "0.0.0",
        build: Callable[[], Awaitable[dict]] | None = None,
        interval: int = INTERVAL,
    ) -> None:
        self.service = service
        self.url = (url or os.getenv("HM_URL", "")).rstrip("/")
        self.token = token or os.getenv("HM_INGEST_TOKEN", "")
        self.version = version
        # Renvoie {"status": ..., "checks": {...}, "meta": {...}} ; le service
        # décide lui-même de son état, il connaît ses dépendances.
        self._build = build
        self._interval = interval
        self._started = time.monotonic()
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=TIMEOUT)
        # Renseigné par la réponse du monitor : permet de dégrader son propre
        # comportement pendant un incident (couper les notifs non critiques).
        self.incident_active = False

    def start(self) -> None:
        if self._task is None and self.url and self.token:
            self._task = asyncio.create_task(self._loop(), name=f"heartbeat:{self.service}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._http.aclose()

    async def _payload(self) -> dict:
        extra = await self._build() if self._build else {}
        return {
            "service": self.service,
            "status": extra.get("status", "ok"),
            "version": self.version,
            "uptime_s": int(time.monotonic() - self._started),
            "checks": extra.get("checks", {}),
            "meta": extra.get("meta", {}),
        }

    async def _loop(self) -> None:
        while True:
            try:
                response = await self._http.post(
                    f"{self.url}/ingest/heartbeat",
                    json=await self._payload(),
                    headers={"X-Health-Token": self.token},
                )
                if response.is_success:
                    self.incident_active = bool(response.json().get("incident_active"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("heartbeat failed: %s", exc)
            await asyncio.sleep(self._interval)


async def build_bot_checks(bot) -> dict:
    """Cas particulier du bot.

    Un event loop vivant dont la connexion gateway est morte ne doit pas se
    déclarer `ok` : on n'émet un statut sain que si `bot.is_ready()`.
    """
    ready = bool(bot.is_ready())
    # `bot.latency` vaut nan tant que la gateway n'a pas répondu.
    latency_ms = round(bot.latency * 1000) if math.isfinite(bot.latency) else None
    connected = len([s for s in getattr(bot, "shards", {}).values() if not s.is_closed()])
    total = getattr(bot, "shard_count", 1) or 1

    return {
        "status": "ok" if ready and connected == total else ("degraded" if ready else "down"),
        "checks": {
            "is_ready": {"ok": ready},
            "discord_gateway": {"ok": ready, "latency_ms": latency_ms},
            "shards": {"ok": connected == total, "connected": connected, "total": total},
        },
        "meta": {"shards": f"{connected}/{total}", "guilds": len(getattr(bot, "guilds", []))},
    }

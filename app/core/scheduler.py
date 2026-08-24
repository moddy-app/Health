"""Boucles asyncio : check, sonde HTTP, poll Better Stack, self-heartbeat, sticky, file.

Règle §11 : aucune exception ne remonte jusqu'à la boucle principale. Chaque
tâche est enveloppée, log, et continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from .. import keys
from ..config import Settings
from ..integrations.betterstack import BetterStack
from ..state import Store
from .detector import Detector
from .incident import IncidentManager
from .notifier import Notifier
from .probe import Probe

log = logging.getLogger("hm.scheduler")


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        detector: Detector,
        incidents: IncidentManager,
        notifier: Notifier,
        betterstack: BetterStack,
        probe: Probe,
    ) -> None:
        self._s = settings
        self._store = store
        self._detector = detector
        self._incidents = incidents
        self._notifier = notifier
        self._bs = betterstack
        self._probe = probe
        self._tasks: list[asyncio.Task] = []
        self._last_level: str | None = None

    # ------------------------------------------------------------------
    async def _loop(self, name: str, interval: float, step: Callable[[], Awaitable[None]]) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("boucle %s : itération en échec, on continue", name)
            await asyncio.sleep(interval)

    def start(self) -> None:
        # Sonde en premier : ses heartbeats synthétiques doivent exister avant
        # que la détection ne les lise.
        if self._probe.targets:
            self._spawn("probe", self._s.hm_probe_interval, self._probe_step)
        self._spawn("check", self._s.hm_check_interval, self._check_step)
        self._spawn("notify-queue", 30, self._queue_step)
        self._spawn("sticky", self._s.discord_sticky_interval, self._sticky_step)
        if self._s.hm_self_heartbeat_url:
            self._spawn("self-heartbeat", self._s.hm_self_heartbeat_interval, self._heartbeat_step)
        if self._s.betterstack_index_url:
            self._spawn("bs-poll", self._s.betterstack_poll_interval, self._bs_poll_step)
        log.info("%d boucle(s) démarrée(s)", len(self._tasks))

    def _spawn(self, name: str, interval: float, step: Callable[[], Awaitable[None]]) -> None:
        self._tasks.append(asyncio.create_task(self._loop(name, interval, step), name=f"hm:{name}"))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    # ------------------------------------------------------------------
    # Étapes
    # ------------------------------------------------------------------
    async def _check_step(self) -> None:
        # Tente une reconnexion Redis à chaque cycle : le resync est automatique.
        if self._store.degraded:
            await self._store.connect()

        snapshot = await self._detector.run_cycle()
        await self._incidents.reconcile(snapshot)

        incident = await self._incidents.get_active()
        public = self._detector.public_payload(snapshot, incident)
        await self._store.set_json(keys.STATUS_PUBLIC, public, ttl=self._s.hm_public_cache_ttl)

        if snapshot.level != self._last_level:
            # Changement d'état : rafraîchissement immédiat du sticky.
            self._last_level = snapshot.level
            await self._notifier.refresh_sticky(public)

    async def _probe_step(self) -> None:
        await self._probe.run_once()

    async def _queue_step(self) -> None:
        await self._notifier.drain_queue()

    async def _sticky_step(self) -> None:
        public = await self._store.get_json(keys.STATUS_PUBLIC)
        if public:
            await self._notifier.refresh_sticky(public)

    async def _heartbeat_step(self) -> None:
        await self._bs.self_heartbeat(ok=True)

    async def _bs_poll_step(self) -> None:
        await self._incidents.reconcile_betterstack()
        if await self._incidents.webhook_seems_dead():
            log.error(
                "aucun event Better Stack depuis plus de %ds : la souscription webhook "
                "a probablement été désactivée après 10 échecs, à recréer manuellement "
                "depuis la status page",
                self._s.hm_bs_webhook_silence_alert,
            )

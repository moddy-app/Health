"""Pubsub Redis entre le monitor et le bot Moddy.

Sortant  : `moddy:hm:notify`      -> le bot poste/édite le message d'incident
Entrant  : `moddy:hm:notify:ack`  -> le bot renvoie le `message_id` obtenu
Entrant  : `moddy:hm:command`     -> commandes `/status *` du staff

Le bot ne parle jamais directement à Better Stack : toute la logique d'incident
reste ici, et le token Better Stack n'existe que dans le monitor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from .. import keys
from ..state import Store

log = logging.getLogger("hm.bus")

CommandHandler = Callable[[str, dict], Awaitable[None]]


class RedisBus:
    def __init__(self, store: Store, ack_timeout: float = 5.0) -> None:
        self._store = store
        self._ack_timeout = ack_timeout
        self._pending: dict[str, asyncio.Future[str | None]] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Sortant
    # ------------------------------------------------------------------
    async def notify(self, action: str, payload: dict, expect_ack: bool = True) -> str | None:
        """Publie une demande vers le bot et attend son ACK.

        Renvoie le `message_id` acquitté, ou None (bot muet, Redis down, pas
        d'ACK sous `ack_timeout`) — l'appelant bascule alors sur le webhook.
        """
        nonce = uuid.uuid4().hex
        message = json.dumps({"nonce": nonce, "action": action, "payload": payload})

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str | None] = loop.create_future()
        if expect_ack:
            self._pending[nonce] = future

        try:
            if not await self._store.publish(keys.CH_NOTIFY, message):
                log.warning("publication %s impossible (redis down)", action)
                return None
            if not expect_ack:
                return None
            try:
                return await asyncio.wait_for(future, timeout=self._ack_timeout)
            except asyncio.TimeoutError:
                log.warning("pas d'ACK du bot pour %s sous %.0fs", action, self._ack_timeout)
                return None
        finally:
            self._pending.pop(nonce, None)

    async def signal(self, action: str, payload: dict | None = None) -> bool:
        """Notification sans ACK attendu (rafraîchissement du sticky, etc.)."""
        message = json.dumps({"action": action, "payload": payload or {}})
        return await self._store.publish(keys.CH_NOTIFY, message)

    # ------------------------------------------------------------------
    # Entrant
    # ------------------------------------------------------------------
    def _resolve_ack(self, data: dict) -> None:
        message_id = data.get("message_id")
        message_id = str(message_id) if message_id is not None else None
        nonce = data.get("nonce")

        future = self._pending.get(nonce) if nonce else None
        if future is None:
            # ACK sans nonce (vieux bot, ou relais simplifié) : on sert la plus
            # ancienne attente en cours.
            for candidate in self._pending.values():
                if not candidate.done():
                    future = candidate
                    break
        if future is not None and not future.done():
            future.set_result(message_id)

    async def _consume(self, channel: str, handle: Callable[[dict], Awaitable[None] | None]) -> None:
        """Boucle d'abonnement, résiliente aux coupures Redis."""
        while True:
            pubsub = self._store.pubsub()
            if pubsub is None:
                await self._store.connect()
                await asyncio.sleep(5)
                continue
            try:
                await pubsub.subscribe(channel)
                log.info("abonné à %s", channel)
                async for raw in pubsub.listen():
                    if raw.get("type") != "message":
                        continue
                    try:
                        data = json.loads(raw["data"])
                    except (json.JSONDecodeError, TypeError):
                        log.warning("message illisible sur %s", channel)
                        continue
                    try:
                        result = handle(data)
                        if result is not None:
                            await result
                    except Exception:
                        log.exception("traitement d'un message %s échoué", channel)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("abonnement %s interrompu: %s", channel, exc)
                await asyncio.sleep(5)
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()

    async def start(self, command_handler: CommandHandler | None = None) -> None:
        async def on_ack(data: dict) -> None:
            self._resolve_ack(data)

        self._tasks.append(asyncio.create_task(self._consume(keys.CH_NOTIFY_ACK, on_ack)))

        if command_handler is not None:

            async def on_command(data: dict) -> None:
                action = data.get("action")
                if not action:
                    return
                await command_handler(action, data.get("payload") or {})

            self._tasks.append(asyncio.create_task(self._consume(keys.CH_COMMAND, on_command)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

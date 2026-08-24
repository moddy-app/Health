"""Store Redis avec fallback mémoire.

Règle absolue : une panne Redis ne doit jamais empêcher une alerte de partir.
Chaque écriture est donc systématiquement mirrorée en mémoire ; quand Redis est
injoignable on sert la mémoire, on note les clés touchées, et on les rejoue dès
la reconnexion. Le volume de données est minuscule (quelques dizaines de clés),
le miroir ne coûte rien.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

try:  # pragma: no cover - dépend de l'environnement
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    Redis = None  # type: ignore[assignment]

log = logging.getLogger("hm.state")

# Intervalle minimum entre deux tentatives de reconnexion.
_RECONNECT_BACKOFF = 5.0


class _Memory:
    """Backend mémoire : strings (avec TTL), sets, listes."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expiry: dict[str, float] = {}

    def _expired(self, key: str) -> bool:
        deadline = self.expiry.get(key)
        if deadline is None:
            return False
        if deadline > time.time():
            return False
        self.drop(key)
        return True

    def drop(self, key: str) -> None:
        self.strings.pop(key, None)
        self.sets.pop(key, None)
        self.lists.pop(key, None)
        self.expiry.pop(key, None)

    def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self.strings.get(key)

    def set(self, key: str, value: str, ttl: int | None) -> None:
        self.strings[key] = value
        if ttl:
            self.expiry[key] = time.time() + ttl
        else:
            self.expiry.pop(key, None)

    def ttl_left(self, key: str) -> int | None:
        deadline = self.expiry.get(key)
        if deadline is None:
            return None
        return max(1, int(deadline - time.time()))

    def sadd(self, key: str, members: tuple[str, ...]) -> None:
        self.sets.setdefault(key, set()).update(members)

    def rpush(self, key: str, values: tuple[str, ...]) -> None:
        self.lists.setdefault(key, []).extend(values)


class Store:
    """Façade unique sur Redis + mémoire.

    Aucune méthode ne lève : un échec Redis bascule silencieusement (avec un
    warning) sur la mémoire.
    """

    def __init__(self, url: str = "") -> None:
        self._url = url
        self._redis: Any | None = None
        self._mem = _Memory()
        self._online = False
        self._dirty: set[str] = set()
        self._last_attempt = 0.0
        self._warned = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connexion
    # ------------------------------------------------------------------
    @property
    def online(self) -> bool:
        return self._online

    @property
    def degraded(self) -> bool:
        return not self._online

    async def connect(self) -> bool:
        """Tente une connexion. Ne lève jamais."""
        if not self._url or Redis is None:
            log.warning("REDIS_URL absente : le monitor tourne en mémoire seule")
            return False
        async with self._lock:
            if self._online:
                return True
            if time.time() - self._last_attempt < _RECONNECT_BACKOFF:
                return False
            self._last_attempt = time.time()
            try:
                client = Redis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_connect_timeout=3,
                    socket_timeout=3,
                    health_check_interval=30,
                )
                await client.ping()
            except Exception as exc:
                if not self._warned:
                    log.warning("redis indisponible, fallback mémoire: %s", exc)
                    self._warned = True
                return False
            self._redis = client
            self._online = True
            self._warned = False
            log.info("redis connecté")
        await self._resync()
        return True

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # pragma: no cover - best effort
                pass
        self._redis = None
        self._online = False

    def _offline(self, exc: Exception) -> None:
        if self._online:
            log.warning("redis perdu, bascule mémoire: %s", exc)
        self._online = False
        self._redis = None

    async def _resync(self) -> None:
        """Rejoue vers Redis tout ce qui a été écrit pendant la coupure."""
        if not self._dirty or self._redis is None:
            self._dirty.clear()
            return
        keys, self._dirty = self._dirty, set()
        log.info("resync redis: %d clé(s)", len(keys))
        for key in keys:
            try:
                if key in self._mem.strings:
                    await self._redis.set(key, self._mem.strings[key], ex=self._mem.ttl_left(key))
                elif key in self._mem.sets:
                    members = self._mem.sets[key]
                    if members:
                        await self._redis.sadd(key, *members)
                elif key in self._mem.lists:
                    values = self._mem.lists[key]
                    await self._redis.delete(key)
                    if values:
                        await self._redis.rpush(key, *values)
                else:
                    await self._redis.delete(key)
            except Exception as exc:
                self._dirty.add(key)
                self._offline(exc)
                return

    def _mark(self, *keys: str) -> None:
        if not self._online:
            self._dirty.update(keys)

    # ------------------------------------------------------------------
    # Strings
    # ------------------------------------------------------------------
    async def get(self, key: str) -> str | None:
        if self._online and self._redis is not None:
            try:
                return await self._redis.get(key)
            except Exception as exc:
                self._offline(exc)
        return self._mem.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._mem.set(key, value, ttl)
        if self._online and self._redis is not None:
            try:
                await self._redis.set(key, value, ex=ttl)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(key)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._mem.drop(key)
        if self._online and self._redis is not None:
            try:
                await self._redis.delete(*keys)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(*keys)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("json invalide sur %s", key)
            return None

    async def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        await self.set(key, json.dumps(value, separators=(",", ":")), ttl)

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    async def sadd(self, key: str, *members: str) -> None:
        if not members:
            return
        self._mem.sadd(key, members)
        if self._online and self._redis is not None:
            try:
                await self._redis.sadd(key, *members)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(key)

    async def sismember(self, key: str, member: str) -> bool:
        if self._online and self._redis is not None:
            try:
                return bool(await self._redis.sismember(key, member))
            except Exception as exc:
                self._offline(exc)
        return member in self._mem.sets.get(key, set())

    async def smembers(self, key: str) -> set[str]:
        if self._online and self._redis is not None:
            try:
                return set(await self._redis.smembers(key))
            except Exception as exc:
                self._offline(exc)
        return set(self._mem.sets.get(key, set()))

    # ------------------------------------------------------------------
    # Listes
    # ------------------------------------------------------------------
    async def rpush(self, key: str, *values: str) -> None:
        if not values:
            return
        self._mem.rpush(key, values)
        if self._online and self._redis is not None:
            try:
                await self._redis.rpush(key, *values)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(key)

    async def lpush(self, key: str, *values: str) -> None:
        """Insertion en tête — sert à remettre un message en début de file."""
        if not values:
            return
        items = self._mem.lists.setdefault(key, [])
        for value in values:
            items.insert(0, value)
        if self._online and self._redis is not None:
            try:
                await self._redis.lpush(key, *values)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(key)

    async def lpop(self, key: str) -> str | None:
        if self._online and self._redis is not None:
            try:
                value = await self._redis.lpop(key)
                items = self._mem.lists.get(key)
                if items:
                    items.pop(0)
                return value
            except Exception as exc:
                self._offline(exc)
        items = self._mem.lists.get(key)
        self._mark(key)
        return items.pop(0) if items else None

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        if self._online and self._redis is not None:
            try:
                return list(await self._redis.lrange(key, start, stop))
            except Exception as exc:
                self._offline(exc)
        items = self._mem.lists.get(key, [])
        end = None if stop == -1 else stop + 1
        return items[start:end]

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        items = self._mem.lists.get(key)
        if items is not None:
            end = None if stop == -1 else stop + 1
            self._mem.lists[key] = items[start:end]
        if self._online and self._redis is not None:
            try:
                await self._redis.ltrim(key, start, stop)
                return
            except Exception as exc:
                self._offline(exc)
        self._mark(key)

    async def llen(self, key: str) -> int:
        if self._online and self._redis is not None:
            try:
                return int(await self._redis.llen(key))
            except Exception as exc:
                self._offline(exc)
        return len(self._mem.lists.get(key, []))

    # ------------------------------------------------------------------
    # Compteur (rate limit)
    # ------------------------------------------------------------------
    async def incr_window(self, key: str, ttl: int) -> int:
        """Incrémente un compteur à fenêtre glissante, pose le TTL au premier hit."""
        if self._online and self._redis is not None:
            try:
                value = int(await self._redis.incr(key))
                if value == 1:
                    await self._redis.expire(key, ttl)
                return value
            except Exception as exc:
                self._offline(exc)
        current = int(self._mem.get(key) or 0) + 1
        self._mem.set(key, str(current), ttl if current == 1 else self._mem.ttl_left(key))
        return current

    async def claim(self, key: str, ttl: int) -> bool:
        """SET NX : renvoie True si la clé vient d'être posée (anti-doublon)."""
        if self._online and self._redis is not None:
            try:
                return bool(await self._redis.set(key, "1", ex=ttl, nx=True))
            except Exception as exc:
                self._offline(exc)
        if self._mem.get(key) is not None:
            return False
        self._mem.set(key, "1", ttl)
        self._mark(key)
        return True

    # ------------------------------------------------------------------
    # Pubsub
    # ------------------------------------------------------------------
    async def publish(self, channel: str, message: str) -> bool:
        if self._online and self._redis is not None:
            try:
                await self._redis.publish(channel, message)
                return True
            except Exception as exc:
                self._offline(exc)
        return False

    def pubsub(self) -> Any | None:
        if self._online and self._redis is not None:
            try:
                return self._redis.pubsub(ignore_subscribe_messages=True)
            except Exception as exc:
                self._offline(exc)
        return None

    # ------------------------------------------------------------------
    # Arrêt propre
    # ------------------------------------------------------------------
    async def flush(self) -> None:
        """Force un resync avant SIGTERM (Railway redéploie souvent)."""
        if await self.connect():
            await self._resync()

"""Message permanent d'état en bas du salon de statut.

Trois déclencheurs le font bouger : un message tiers posté dans le salon (il
n'est plus le dernier), le rafraîchissement passif, et un changement d'état
détecté par le cœur du monitor. Les trois peuvent tomber en même temps — d'où
le verrou.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .. import keys
from ..config import Settings
from ..render.model import StatusPresentation
from ..state import Store
from .views import StickyStatusView

log = logging.getLogger("hm.bot.sticky")


class StickyManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self._bot = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._message_id: int | None = None
        self._last_public: dict = {}

    def bind(self, bot) -> None:
        self._bot = bot

    @property
    def enabled(self) -> bool:
        return bool(self._s.hm_sticky_enabled and self._bot is not None and self._channel_id)

    @property
    def _channel_id(self) -> int:
        try:
            return int(self._s.discord_status_channel_id)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """Retrouve le sticky d'avant le redéploiement plutôt que d'en poster un autre.

        Sans ça, chaque redéploiement laisse un cadavre dans le salon.
        """
        raw = await self._store.get(keys.STICKY_MESSAGE_ID)
        self._message_id = int(raw) if raw and raw.isdigit() else None

    async def stop(self) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pending

    # ------------------------------------------------------------------
    # Déclencheurs
    # ------------------------------------------------------------------
    async def refresh(self, public: dict) -> None:
        """Rafraîchissement passif ou sur changement d'état : on **édite**."""
        if not self.enabled:
            return
        self._last_public = public or self._last_public
        async with self._lock:
            await self._render(repost=False)

    def on_channel_message(self, message_id: int) -> None:
        """Un tiers a parlé dans le salon : le sticky n'est plus le dernier message.

        Debounce : sans lui, une rafale de dix messages produit dix reposts et
        un rate limit Discord immédiat.
        """
        if not self.enabled or message_id == self._message_id:
            return
        if self._pending and not self._pending.done():
            self._pending.cancel()
        self._pending = asyncio.create_task(self._repost_after_delay())

    async def force_repost(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            await self._render(repost=True)

    async def _repost_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._s.hm_sticky_debounce)
            async with self._lock:
                await self._render(repost=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Règle §11 : aucune exception ne remonte jusqu'à une boucle.
            log.exception("repost du sticky en échec")

    # ------------------------------------------------------------------
    # Rendu
    # ------------------------------------------------------------------
    async def _channel(self):
        channel = self._bot.get_channel(self._channel_id)
        if channel is None:
            channel = await self._bot.fetch_channel(self._channel_id)
        return channel

    async def _snapshot(self) -> StatusPresentation:
        public = self._last_public or await self._store.get_json(keys.STATUS_PUBLIC) or {}
        self._last_public = public
        return StatusPresentation.from_public(public)

    async def _render(self, *, repost: bool) -> None:
        if not self._bot.is_ready():
            return
        snapshot = await self._snapshot()
        view = StickyStatusView(snapshot, self._s.discord_status_page_url)
        channel = await self._channel()

        if not repost and self._message_id:
            try:
                message = await channel.fetch_message(self._message_id)
                await message.edit(view=view)
                return
            except Exception as exc:
                # Supprimé à la main, ou jamais posté : on repost.
                log.info("sticky %s introuvable, repost: %s", self._message_id, exc)

        await self._delete_previous(channel)
        message = await channel.send(view=view)
        self._message_id = message.id
        await self._store.set(keys.STICKY_MESSAGE_ID, str(message.id))

    async def _delete_previous(self, channel) -> None:
        """Supprimer l'ancien avant de poster le nouveau, sans exiger qu'il existe."""
        if not self._message_id:
            return
        try:
            previous = await channel.fetch_message(self._message_id)
            await previous.delete()
        except Exception as exc:
            log.debug("ancien sticky déjà absent: %s", exc)
        self._message_id = None

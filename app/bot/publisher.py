"""Publication et édition du message d'incident par le bot.

Premier maillon de la chaîne de redondance (`core/notifier.py`). Ne décide de
rien : il reçoit une présentation déjà construite et se contente de l'envoyer,
ou de renvoyer un échec pour que le webhook prenne le relais.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..render.layout import build_layout_view
from ..render.model import IncidentPresentation

log = logging.getLogger("hm.bot.publisher")


class IncidentPublisher:
    """Transport « bot » du notifier.

    Le bot est créé avant d'être connecté : `bind` l'attache une fois le client
    construit, et `enabled` reste faux tant que la gateway n'est pas prête.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._bot = None
        self._sticky = None

    def bind(self, bot, sticky=None) -> None:
        self._bot = bot
        self._sticky = sticky

    @property
    def enabled(self) -> bool:
        # `is_ready()` plutôt qu'une simple présence : pendant une reconnexion
        # gateway, envoyer reviendrait à attendre l'ACK timeout pour rien.
        return bool(self._bot is not None and self._bot.is_ready() and self._channel_id)

    @property
    def _channel_id(self) -> int:
        try:
            return int(self._s.discord_status_channel_id)
        except (TypeError, ValueError):
            return 0

    async def _channel(self):
        channel = self._bot.get_channel(self._channel_id)
        if channel is None:
            # Absent du cache (redémarrage, salon créé après le READY) : un
            # fetch vaut mieux qu'un abandon.
            channel = await self._bot.fetch_channel(self._channel_id)
        return channel

    # ------------------------------------------------------------------
    async def send(self, presentation: IncidentPresentation) -> str | None:
        """Poste le message. Renvoie son ID, ou None pour basculer au webhook."""
        if not self.enabled:
            return None
        try:
            channel = await self._channel()
            message = await asyncio.wait_for(
                channel.send(view=build_layout_view(presentation)),
                timeout=self._s.hm_bot_ack_timeout,
            )
            return str(message.id)
        except Exception as exc:
            log.warning("publication par le bot impossible, bascule webhook: %s", exc)
            return None

    async def edit(self, message_id: str, presentation: IncidentPresentation) -> bool:
        if not self.enabled or not message_id:
            return False
        try:
            channel = await self._channel()
            message = await asyncio.wait_for(
                channel.fetch_message(int(message_id)), timeout=self._s.hm_bot_ack_timeout
            )
            await asyncio.wait_for(
                message.edit(view=build_layout_view(presentation)),
                timeout=self._s.hm_bot_ack_timeout,
            )
            return True
        except Exception as exc:
            log.warning("édition par le bot impossible (%s): %s", message_id, exc)
            return False

    async def refresh_sticky(self, public: dict) -> None:
        """Relaie au gestionnaire de sticky ; sans bot, il n'y a rien à faire."""
        if self._sticky is None:
            return
        await self._sticky.refresh(public)

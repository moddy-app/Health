"""Chaîne de redondance Discord.

    1. Bot Moddy (pubsub Redis `moddy:hm:notify`)
           | échec ou pas d'ACK sous 5s
    2. Webhook Discord direct
           | échec (Discord down)
    3. File de rattrapage `hm:notify:queue` + log

Le niveau 1 est préféré parce que le bot sait éditer ses propres messages et
gérer le sticky. Le niveau 2 garantit que si le bot est mort, l'alerte part
quand même.
"""

from __future__ import annotations

import hashlib
import json
import logging

from .. import keys
from ..config import Settings
from ..integrations.discord_webhook import DiscordWebhook
from ..integrations.redis_bus import RedisBus
from ..render.components import build_incident_components, build_incident_embed
from ..state import Store
from ..util import iso

log = logging.getLogger("hm.notifier")

# Taille max de la file de rattrapage, pour ne pas rejouer trois jours d'échecs.
QUEUE_MAX = 200


class Notifier:
    def __init__(
        self, settings: Settings, store: Store, bus: RedisBus, webhook: DiscordWebhook
    ) -> None:
        self._s = settings
        self._store = store
        self._bus = bus
        self._webhook = webhook

    # ------------------------------------------------------------------
    # Idempotence et rate-limit
    # ------------------------------------------------------------------
    @staticmethod
    def _dedup_key(incident: dict, channel: str) -> str:
        update_index = len(incident.get("updates") or [])
        raw = f"{incident.get('id')}:{update_index}:{channel}"
        return hashlib.sha1(raw.encode()).hexdigest()

    async def _already_sent(self, incident: dict, channel: str) -> bool:
        return await self._store.sismember(keys.NOTIFY_SENT, self._dedup_key(incident, channel))

    async def _mark_sent(self, incident: dict, channel: str) -> None:
        await self._store.sadd(keys.NOTIFY_SENT, self._dedup_key(incident, channel))

    async def allow(self, service: str, status: str) -> bool:
        """Max 1 notification par service et par état toutes les 5 minutes."""
        key = keys.NOTIFY_RATELIMIT.format(service=service, status=status)
        return await self._store.claim(key, self._s.hm_notify_rate_limit)

    # ------------------------------------------------------------------
    # Envoi
    # ------------------------------------------------------------------
    async def dispatch(self, incident: dict, *, queue_on_failure: bool = True) -> dict:
        """Poste ou édite le message d'incident. Renvoie l'incident enrichi."""
        if not await self.deliver(incident):
            if queue_on_failure:
                await self.enqueue(incident)
            log.error("aucun canal Discord disponible pour l'incident %s", incident.get("id"))
        return incident

    async def deliver(self, incident: dict) -> bool:
        """Descend la chaîne de redondance. Renvoie True dès qu'un canal a pris.

        L'incident est muté au passage (`discord_message_id`, `discord_transport`).
        """
        # Une version donnée d'un incident ne part qu'une fois, tous canaux
        # confondus : sans ça, un incident déjà relayé par le bot repartirait
        # une seconde fois par webhook au premier retry.
        if await self._already_sent(incident, "bot") or await self._already_sent(
            incident, "webhook"
        ):
            return True

        components = build_incident_components(incident, self._s.service_names)
        embed = build_incident_embed(incident, self._s.service_names)
        message_id = incident.get("discord_message_id")
        transport = incident.get("discord_transport")

        # --- Niveau 1 : le bot -------------------------------------------------
        if not await self._already_sent(incident, "bot"):
            action = "incident.edit" if message_id else "incident.post"
            payload = {
                "incident_id": incident.get("id"),
                "channel_id": self._s.discord_status_channel_id,
                "message_id": message_id,
                "flags": 32768,
                "components": components,
                "embed": embed,
            }
            acked = await self._bus.notify(action, payload)
            if acked:
                await self._mark_sent(incident, "bot")
                incident["discord_message_id"] = acked
                incident["discord_transport"] = "bot"
                incident["discord_channel_id"] = self._s.discord_status_channel_id
                return True

        # --- Niveau 2 : le webhook --------------------------------------------
        if self._webhook.enabled and not await self._already_sent(incident, "webhook"):
            sent_id: str | None = None
            if message_id and transport == "webhook":
                if await self._webhook.edit(message_id, components, embed):
                    sent_id = message_id
                else:
                    log.warning("édition webhook impossible, repost d'un message neuf")
            if sent_id is None:
                # Un message posté par le bot n'est pas éditable via webhook :
                # on en poste un nouveau plutôt que de perdre l'information.
                sent_id = await self._webhook.send(components, embed)
            if sent_id:
                await self._mark_sent(incident, "webhook")
                incident["discord_message_id"] = sent_id
                incident["discord_transport"] = "webhook"
                return True

        # --- Niveau 3 : l'appelant empile dans la file de rattrapage -----------
        return False

    # ------------------------------------------------------------------
    # File de rattrapage
    # ------------------------------------------------------------------
    async def enqueue(self, incident: dict) -> None:
        entry = {"queued_at": iso(), "incident": incident}
        await self._store.rpush(keys.NOTIFY_QUEUE, json.dumps(entry, separators=(",", ":")))
        await self._store.ltrim(keys.NOTIFY_QUEUE, -QUEUE_MAX, -1)
        log.warning("incident %s empilé dans la file de rattrapage", incident.get("id"))

    async def drain_queue(self) -> int:
        """Vide la file dans l'ordre, dès qu'un canal redevient disponible."""
        drained = 0
        pending = await self._store.llen(keys.NOTIFY_QUEUE)
        for _ in range(pending):
            raw = await self._store.lpop(keys.NOTIFY_QUEUE)
            if not raw:
                break
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            incident = entry.get("incident") or {}
            if await self.deliver(incident):
                drained += 1
                continue
            # Toujours pas de canal : on remet en tête et on s'arrête, l'ordre
            # d'origine doit être préservé.
            await self._store.lpush(keys.NOTIFY_QUEUE, raw)
            break
        if drained:
            log.info("file de rattrapage : %d message(s) rejoué(s)", drained)
        return drained

    # ------------------------------------------------------------------
    # Sticky
    # ------------------------------------------------------------------
    async def refresh_sticky(self, public: dict) -> None:
        """Demande au bot de rafraîchir le sticky (il en est propriétaire)."""
        await self._bus.signal(
            "sticky.refresh",
            {"channel_id": self._s.discord_status_channel_id, "status": public},
        )

"""Chaîne de redondance Discord.

    1. Bot Health Monitor (client discord.py, même process)
           | échec ou pas d'ACK sous HM_BOT_ACK_TIMEOUT
    2. Webhook Discord direct
           | échec (Discord down)
    3. File de rattrapage `hm:notify:queue` + log

Le niveau 1 est préféré parce que le bot sait éditer ses propres messages et
gérer le sticky. Le niveau 2 garantit que si la gateway est perdue ou
l'application suspendue, l'alerte part quand même — d'où un webhook créé à la
main dans le salon, et surtout pas par cette application.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Protocol

from .. import keys
from ..config import Settings
from ..integrations.discord_webhook import DiscordWebhook
from ..render.model import IncidentPresentation
from ..render.raw import build_raw_components, build_raw_embed
from ..state import Store
from ..util import iso
from .impact import DEGRADED, DOWN

log = logging.getLogger("hm.notifier")

# Taille max de la file de rattrapage, pour ne pas rejouer trois jours d'échecs.
QUEUE_MAX = 200


class BotTransport(Protocol):
    """Ce que le notifier attend du bot — voir `app/bot/publisher.py`."""

    @property
    def enabled(self) -> bool: ...

    async def send(self, presentation: IncidentPresentation) -> str | None: ...

    async def edit(self, message_id: str, presentation: IncidentPresentation) -> bool: ...

    async def refresh_sticky(self, public: dict) -> None: ...


class NoBot:
    """Doublure quand aucun DISCORD_TOKEN n'est configuré : tout passe au webhook."""

    enabled = False

    async def send(self, presentation: IncidentPresentation) -> str | None:
        return None

    async def edit(self, message_id: str, presentation: IncidentPresentation) -> bool:
        return False

    async def refresh_sticky(self, public: dict) -> None:
        return None


class Notifier:
    def __init__(
        self, settings: Settings, store: Store, bot: BotTransport, webhook: DiscordWebhook
    ) -> None:
        self._s = settings
        self._store = store
        self._bot = bot
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

    async def reset(self, service: str) -> None:
        """Un service revenu à la normale récupère aussitôt son droit d'alerter.

        Sans ça, toute résolution ouvrirait un angle mort : le service resterait
        muet jusqu'à la fin de sa fenêtre de 5 min, même en retombant pour de
        bon. Les rechutes rapprochées sont déjà absorbées par les seuils de
        détection, pas par ce compteur.
        """
        await self._store.delete(
            *(
                keys.NOTIFY_RATELIMIT.format(service=service, status=status)
                for status in (DOWN, DEGRADED)
            )
        )

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

        presentation = IncidentPresentation.from_incident(
            incident,
            self._s.service_names,
            mentions=self._s.mention_line(incident.get("affected") or []),
        )
        message_id = incident.get("discord_message_id")
        transport = incident.get("discord_transport")

        # --- Niveau 1 : le bot -------------------------------------------------
        if self._bot.enabled:
            sent_id: str | None = None
            # Le transport est collant : un message posté par webhook n'est pas
            # éditable par le bot, et inversement.
            if message_id and transport == "bot":
                if await self._bot.edit(message_id, presentation):
                    sent_id = message_id
                else:
                    log.warning("édition bot impossible, repost d'un message neuf")
                    sent_id = await self._bot.send(presentation)
            elif transport == "webhook" and self._webhook.enabled:
                # Le message appartient au webhook : lui seul peut l'éditer, le
                # bot ne ferait qu'en poster un doublon. On lui laisse la main.
                pass
            else:
                sent_id = await self._bot.send(presentation)
            if sent_id:
                await self._mark_sent(incident, "bot")
                incident["discord_message_id"] = sent_id
                incident["discord_transport"] = "bot"
                incident["discord_channel_id"] = self._s.discord_status_channel_id
                return True

        # --- Niveau 2 : le webhook --------------------------------------------
        if self._webhook.enabled:
            components = build_raw_components(presentation)
            embed = build_raw_embed(presentation)
            sent_id = None
            if message_id and transport == "webhook":
                if await self._webhook.edit(message_id, components, embed, presentation.mentions):
                    sent_id = message_id
                else:
                    log.warning("édition webhook impossible, repost d'un message neuf")
            if sent_id is None:
                # Un message posté par le bot n'est pas éditable via webhook :
                # on en poste un nouveau plutôt que de perdre l'information.
                sent_id = await self._webhook.send(components, embed, presentation.mentions)
            if sent_id:
                await self._mark_sent(incident, "webhook")
                incident["discord_message_id"] = sent_id
                incident["discord_transport"] = "webhook"
                return True

        # --- Niveau 3 : l'appelant empile dans la file de rattrapage -----------
        return False

    async def re_render(self, incident: dict) -> bool:
        """Réédite le message d'incident tel quel — sans le filtre anti-doublon.

        Sert au resync manuel des updates depuis Better Stack : leur contenu a
        changé sous les pieds du dédoublonneur, qui ne compte que le nombre
        d'updates (`_dedup_key`) et prendrait un texte corrigé pour un envoi
        déjà fait. Édite sur le transport déjà propriétaire du message ; n'en
        poste jamais un nouveau, et ne marque rien dans l'anti-doublon.
        """
        message_id = incident.get("discord_message_id")
        transport = incident.get("discord_transport")
        if not message_id or not transport:
            return False

        presentation = IncidentPresentation.from_incident(
            incident,
            self._s.service_names,
            mentions=self._s.mention_line(incident.get("affected") or []),
        )
        if transport == "bot" and self._bot.enabled:
            return await self._bot.edit(message_id, presentation)
        if transport == "webhook" and self._webhook.enabled:
            components = build_raw_components(presentation)
            embed = build_raw_embed(presentation)
            return await self._webhook.edit(message_id, components, embed, presentation.mentions)
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
        """Le sticky appartient au bot : lui seul peut le poster et l'éditer."""
        await self._bot.refresh_sticky(public)

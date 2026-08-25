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
from collections import deque

import discord

from .. import keys
from ..config import Settings
from ..render.model import StatusPresentation
from ..state import Store
from .views import REFRESH_ID, StickyStatusView

log = logging.getLogger("hm.bot.sticky")


class StickyManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self._bot = None
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task | None = None
        self._message_id: int | None = None
        # `channel.send()` n'a pas encore rendu l'ID que la gateway a déjà livré
        # le MESSAGE_CREATE correspondant. Sans mémoire de ce qu'on vient de
        # poster, le sticky ne se reconnaît pas, se croit poussé par un tiers et
        # se repost — indéfiniment. D'où l'historique court plutôt qu'un seul ID.
        self._own: deque[int] = deque(maxlen=10)
        self._last_public: dict = {}
        # Une permission manquante ne change pas d'avis : on la signale une fois.
        self._delete_warned = False

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

        Redis n'est qu'un raccourci : sans lui — mémoire seule, ou instance
        vidée — l'ID est perdu à chaque redémarrage, et le monitor postait un
        sticky neuf en abandonnant le précédent. Un redéploiement de plus, un
        cadavre de plus. Le salon, lui, ne ment jamais : c'est là qu'on va
        chercher la vérité si Redis ne l'a pas.
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
        if not self.enabled or message_id == self._message_id or message_id in self._own:
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

        # Redis peut avoir perdu l'ID (mémoire seule, redéploiement) : le salon
        # est la seule source de vérité qui survive à tout.
        if self._message_id is None:
            await self._adopt(channel)

        # Deuxième filet, celui qui ferme définitivement la course : si le
        # sticky est déjà le dernier message du salon, il n'y a rien à
        # descendre. Un repost ne ferait que produire le message qui déclenchera
        # le suivant.
        if repost and await self._is_last(channel):
            repost = False

        if not repost and await self._edit(channel, view):
            return

        # Ne jamais poster un sticky de plus si on n'a pas su retirer le
        # précédent : un sticky qui n'est pas tout en bas vaut mieux qu'un salon
        # qui se remplit de sticky morts.
        if not await self._delete_previous(channel) and await self._edit(channel, view):
            return

        message = await channel.send(view=view)
        self._message_id = message.id
        self._own.append(message.id)
        await self._store.set(keys.STICKY_MESSAGE_ID, str(message.id))

    async def _edit(self, channel, view) -> bool:
        """Édite le sticky en place. False s'il n'existe plus."""
        if not self._message_id:
            return False
        try:
            message = await channel.fetch_message(self._message_id)
            await message.edit(view=view)
            return True
        except Exception as exc:
            log.info("sticky %s inéditable: %s", self._message_id, exc)
            return False

    def _is_ours(self, message) -> bool:
        """Un sticky à nous : posté par le bot, et portant le bouton Refresh.

        Le `custom_id` distingue le sticky des messages d'incident, que le bot
        poste dans le même salon et qu'il ne faut surtout pas supprimer.
        """
        user = self._bot.user
        if user is None or getattr(message.author, "id", None) != user.id:
            return False
        return _carries(message.components, REFRESH_ID)

    async def _ours(self, channel, limit: int = 50) -> list:
        """Nos stickys encore présents dans le salon, du plus ancien au plus récent."""
        found = []
        try:
            async for message in channel.history(limit=limit):
                if self._is_ours(message):
                    found.append(message)
        except Exception as exc:
            log.warning("historique du salon illisible: %s", exc)
        found.reverse()
        return found

    async def _adopt(self, channel) -> None:
        """Redis a oublié l'ID : on reprend le dernier sticky du salon et on
        supprime les autres.

        C'est ce qui rend le sticky insensible à une panne Redis — et à un
        redéploiement pendant cette panne.
        """
        ours = await self._ours(channel)
        if not ours:
            return
        keep = ours[-1]
        for stale in ours[:-1]:
            await self._delete(stale)
        self._message_id = keep.id
        self._own.append(keep.id)
        await self._store.set(keys.STICKY_MESSAGE_ID, str(keep.id))
        if len(ours) > 1:
            log.info("%d sticky(s) orphelin(s) supprimé(s)", len(ours) - 1)

    async def _delete(self, message) -> bool:
        """Supprime un sticky. Renvoie False si Discord a refusé.

        Un refus ne doit pas passer en `debug` : sans `Manage Messages`, le bot
        n'efface jamais rien, chaque repost laisse un orphelin, et le salon se
        remplit en silence. C'est une erreur de configuration, elle doit se voir.
        """
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return True  # déjà parti, c'est le résultat voulu
        except discord.Forbidden:
            if not self._delete_warned:
                log.warning(
                    "suppression du sticky refusée : il manque la permission "
                    "« Manage Messages » au bot dans le salon %s. Le sticky sera "
                    "édité sur place au lieu d'être reposté.",
                    self._channel_id,
                )
                self._delete_warned = True
            return False
        except Exception as exc:
            log.warning("suppression du sticky %s impossible: %s", message.id, exc)
            return False

    async def _is_last(self, channel) -> bool:
        """Le sticky est-il déjà en bas du salon ?

        Lu sur l'API plutôt que sur `last_message_id`, qui vient du cache et
        peut être en retard au démarrage — précisément le moment où la course
        se produit.
        """
        if not self._message_id:
            return False
        try:
            async for message in channel.history(limit=1):
                return message.id == self._message_id
        except Exception as exc:
            log.debug("historique du salon illisible: %s", exc)
        return False

    async def _delete_previous(self, channel) -> bool:
        """Supprime tout sticky présent avant d'en poster un neuf.

        On balaie le salon plutôt que de se fier au seul ID connu : celui-ci
        peut avoir été perdu, et un orphelin resterait alors à l'écran.
        Renvoie False si l'un d'eux a résisté — l'appelant ne doit alors pas
        empiler un message de plus.
        """
        cleared = True
        for stale in await self._ours(channel):
            if not await self._delete(stale):
                cleared = False
        if cleared:
            self._message_id = None
        return cleared


def _carries(components, custom_id: str) -> bool:
    """Cherche un `custom_id` dans un arbre de composants V2.

    `discord.Message.components` rend l'arbre brut : un Container porte des
    ActionRow, qui portent les boutons. Pas de `walk_components` en 2.7.
    """
    for component in components or []:
        if getattr(component, "custom_id", None) == custom_id:
            return True
        if _carries(getattr(component, "children", None), custom_id):
            return True
    return False

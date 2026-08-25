"""Sticky message : debounce, verrou, persistance de l'ID."""

from __future__ import annotations

import asyncio

import pytest

from app import keys
from app.bot.sticky import StickyManager
from app.config import Settings


class FakeAuthor:
    def __init__(self, author_id: int) -> None:
        self.id = author_id


class FakeButton:
    def __init__(self, custom_id: str) -> None:
        self.custom_id = custom_id


class FakeContainer:
    """Un Container V2 porte ses composants dans `children`, comme discord.py."""

    def __init__(self, *children) -> None:
        self.children = list(children)


BOT_ID = 4242


class FakeMessage:
    def __init__(self, message_id: int, channel, *, sticky: bool = True,
                 author_id: int = BOT_ID) -> None:
        self.id = message_id
        self._channel = channel
        self.edits = 0
        self.author = FakeAuthor(author_id)
        self.components = (
            [FakeContainer(FakeButton("hm:sticky:refresh"))] if sticky else []
        )

    async def edit(self, **_):
        self.edits += 1
        self._channel.edited.append(self.id)

    async def delete(self):
        self._channel.deleted.append(self.id)
        self._channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(self) -> None:
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[int] = []
        self.edited: list[int] = []
        self.deleted: list[int] = []
        self.third_party: list[int] = []
        self.order: list[int] = []
        self._next = 100

    async def send(self, **kwargs):
        self._next += 1
        message = FakeMessage(self._next, self, sticky="view" in kwargs)
        self.messages[message.id] = message
        self.order.append(message.id)
        # Le sticky poste avec une View ; un tiers, non. Les distinguer rend
        # les assertions lisibles.
        (self.sent if "view" in kwargs else self.third_party).append(message.id)
        return message

    async def fetch_message(self, message_id: int):
        message = self.messages.get(message_id)
        if message is None:
            raise LookupError("unknown message")
        return message

    def history(self, limit: int = 1):
        ordered = [self.messages[mid] for mid in self.order if mid in self.messages]

        async def _iter():
            for message in reversed(ordered[-limit:]):
                yield message

        return _iter()


class FakeBot:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel
        self.user = FakeAuthor(BOT_ID)

    def is_ready(self) -> bool:
        return True

    def get_channel(self, _):
        return self.channel

    async def fetch_channel(self, _):  # pragma: no cover - le cache répond déjà
        return self.channel


PUBLIC = {
    "status": "operational",
    "updated_at": "2026-08-24T19:42:00Z",
    "services": [{"id": "moddy-bot", "name": "Moddy Bot", "status": "operational"}],
}


@pytest.fixture
def sticky(store):
    settings = Settings(
        redis_url="",
        discord_status_channel_id="42",
        hm_sticky_debounce=0,
    )
    manager = StickyManager(settings, store)
    channel = FakeChannel()
    manager.bind(FakeBot(channel))
    return manager, channel, store, settings


async def test_first_refresh_posts_and_persists_the_id(sticky):
    manager, channel, store, _ = sticky
    await manager.refresh(PUBLIC)
    assert len(channel.sent) == 1
    assert await store.get(keys.STICKY_MESSAGE_ID) == str(channel.sent[0])


async def test_a_passive_refresh_edits_rather_than_reposts(sticky):
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)
    await manager.refresh(PUBLIC)
    assert len(channel.sent) == 1
    assert channel.edited == [channel.sent[0]]


async def test_a_third_party_message_reposts_at_the_bottom(sticky):
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)
    first = channel.sent[0]

    await channel.send()  # un tiers parle : le sticky n'est plus le dernier
    manager.on_channel_message(channel.order[-1])
    await asyncio.sleep(0.05)
    assert len(channel.sent) == 2
    # L'ancien est supprimé avant que le nouveau ne parte.
    assert channel.deleted == [first]


async def test_a_burst_of_messages_produces_a_single_repost(sticky):
    """Sans debounce, dix messages donnent dix reposts et un rate limit."""
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)

    for _ in range(10):
        await channel.send()
        manager.on_channel_message(channel.order[-1])
    await asyncio.sleep(0.05)
    # Dix messages tiers, un seul repost : c'est tout l'intérêt du debounce.
    assert len(channel.sent) == 2


async def test_the_sticky_ignores_itself(sticky):
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)

    manager.on_channel_message(channel.sent[0])
    await asyncio.sleep(0.05)
    assert len(channel.sent) == 1


async def test_a_deleted_sticky_is_reposted_not_lost(sticky):
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)
    channel.messages.clear()  # quelqu'un l'a supprimé à la main

    await manager.refresh(PUBLIC)
    assert len(channel.sent) == 2


async def test_a_restart_reuses_the_persisted_message(sticky):
    manager, channel, store, settings = sticky
    await manager.refresh(PUBLIC)
    posted = channel.sent[0]

    # Redéploiement : un gestionnaire neuf, le même Redis.
    restarted = StickyManager(settings, store)
    restarted.bind(FakeBot(channel))
    await restarted.load()
    await restarted.refresh(PUBLIC)

    assert len(channel.sent) == 1  # aucun cadavre laissé dans le salon
    assert channel.edited == [posted]


async def test_a_disabled_sticky_never_posts(store):
    settings = Settings(redis_url="", discord_status_channel_id="42", hm_sticky_enabled=False)
    manager = StickyManager(settings, store)
    channel = FakeChannel()
    manager.bind(FakeBot(channel))

    await manager.refresh(PUBLIC)
    manager.on_channel_message(999)
    await asyncio.sleep(0.05)
    assert channel.sent == []


async def test_the_sticky_never_reposts_over_its_own_message(sticky):
    """Le bug de production : dix stickys empilés dans le salon.

    `channel.send()` n'a pas encore rendu l'ID que la gateway a déjà livré le
    MESSAGE_CREATE. Le sticky ne se reconnaissait pas, se croyait poussé par un
    tiers, et repostait — ce qui produisait le message suivant.
    """
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)
    posted = channel.sent[0]

    # L'événement arrive « en retard », après que l'ID a été enregistré.
    manager.on_channel_message(posted)
    await asyncio.sleep(0.05)
    assert len(channel.sent) == 1


async def test_a_repost_is_skipped_when_the_sticky_is_already_at_the_bottom(sticky):
    """Le filet qui ferme la course : rien à descendre s'il est déjà en bas."""
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)

    # L'ID nous est encore inconnu au moment de l'événement (la course).
    manager.on_channel_message(999_999)
    await asyncio.sleep(0.05)
    assert len(channel.sent) == 1
    assert channel.deleted == []


async def test_a_real_third_party_message_still_pushes_the_sticky_down(sticky):
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)
    first = channel.sent[0]

    # Un vrai message tiers : il devient le dernier du salon.
    await channel.send()
    manager.on_channel_message(channel.order[-1])
    await asyncio.sleep(0.05)

    assert len(channel.sent) == 2
    assert channel.deleted == [first]


async def test_ten_of_its_own_messages_produce_no_repost(sticky):
    """Même en rafale, le sticky ne se déclenche jamais sur lui-même."""
    manager, channel, *_ = sticky
    await manager.refresh(PUBLIC)

    for _ in range(10):
        manager.on_channel_message(channel.sent[-1])
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert len(channel.sent) == 1



async def test_a_redeploy_without_redis_reuses_the_sticky_already_in_the_channel(sticky):
    """La cause réelle de la pile : Redis ne survit pas au redémarrage.

    En mémoire seule, `hm:sticky:message_id` est perdu à chaque redéploiement.
    Le sticky repartait alors de zéro et abandonnait le précédent — un
    redéploiement, un cadavre de plus.
    """
    manager, channel, store, settings = sticky
    await manager.refresh(PUBLIC)
    original = channel.sent[0]

    # Redéploiement : Redis a tout oublié, le salon non.
    await store.delete(keys.STICKY_MESSAGE_ID)
    restarted = StickyManager(settings, store)
    restarted.bind(FakeBot(channel))
    await restarted.load()
    await restarted.refresh(PUBLIC)

    assert len(channel.sent) == 1                 # aucun sticky de plus
    assert channel.edited == [original]           # celui du salon est réutilisé
    assert await store.get(keys.STICKY_MESSAGE_ID) == str(original)


async def test_the_orphans_of_previous_deploys_are_swept(sticky):
    """Dix redéploiements avaient laissé dix stickys empilés."""
    manager, channel, store, settings = sticky
    orphans = []
    for _ in range(5):                            # cinq redéploiements sans Redis
        message = await channel.send(view=object())
        orphans.append(message.id)

    await store.delete(keys.STICKY_MESSAGE_ID)
    fresh = StickyManager(settings, store)
    fresh.bind(FakeBot(channel))
    await fresh.load()
    await fresh.refresh(PUBLIC)

    # Le plus récent est adopté, les quatre autres disparaissent.
    assert channel.deleted == orphans[:-1]
    assert channel.edited == [orphans[-1]]


async def test_an_incident_message_is_never_mistaken_for_a_sticky(sticky):
    """Le bot poste ses incidents dans le même salon : il ne doit pas les supprimer."""
    manager, channel, store, settings = sticky
    incident = await channel.send()               # pas de View : ce n'est pas un sticky
    await manager.refresh(PUBLIC)

    await store.delete(keys.STICKY_MESSAGE_ID)
    fresh = StickyManager(settings, store)
    fresh.bind(FakeBot(channel))
    await fresh.load()
    await fresh.refresh(PUBLIC)

    assert incident.id not in channel.deleted


async def test_a_message_from_someone_else_is_never_deleted(sticky):
    manager, channel, store, settings = sticky
    intruder = FakeMessage(777, channel, sticky=True, author_id=9999)
    channel.messages[777] = intruder
    channel.order.append(777)

    await manager.refresh(PUBLIC)
    assert 777 not in channel.deleted

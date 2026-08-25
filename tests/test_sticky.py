"""Sticky message : debounce, verrou, persistance de l'ID."""

from __future__ import annotations

import asyncio

import pytest

from app import keys
from app.bot.sticky import StickyManager
from app.config import Settings


class FakeMessage:
    def __init__(self, message_id: int, channel) -> None:
        self.id = message_id
        self._channel = channel
        self.edits = 0

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
        message = FakeMessage(self._next, self)
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

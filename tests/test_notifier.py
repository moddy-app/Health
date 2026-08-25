"""Chaîne de redondance Discord : bot -> webhook -> file de rattrapage."""

from __future__ import annotations

import pytest

from app import keys
from app.core.notifier import Notifier


class FakeBot:
    """Doublure du transport bot : ce que `IncidentPublisher` expose au notifier."""

    def __init__(self, message_id: str | None = "1409", enabled: bool = True) -> None:
        self.enabled = enabled
        self.message_id = message_id
        self.calls: list[tuple[str, str | None]] = []
        self.sticky: list[dict] = []

    async def send(self, presentation):
        self.calls.append(("send", None))
        return self.message_id

    async def edit(self, message_id, presentation):
        self.calls.append(("edit", message_id))
        return self.message_id is not None

    async def refresh_sticky(self, public):
        self.sticky.append(public)


class FakeWebhook:
    def __init__(self, enabled: bool = True, message_id: str | None = "w1") -> None:
        self.enabled = enabled
        self.message_id = message_id
        self.sent = 0
        self.edited: list[str] = []
        self.components: list[list[dict]] = []
        self.contents: list[str] = []

    async def send(self, components, embed=None, content=""):
        self.sent += 1
        self.components.append(components)
        self.contents.append(content)
        return self.message_id

    async def edit(self, message_id, components, embed=None, content=""):
        self.edited.append(message_id)
        self.contents.append(content)
        return self.message_id is not None


def incident(**over) -> dict:
    base = {
        "id": "inc_20260824_1942",
        "title": "Major Outage",
        "level": "major_outage",
        "status": "open",
        "affected": ["moddy-bot"],
        "updates": [{"kind": "created", "at": "2026-08-24T19:42:00Z", "message": "Investigating."}],
    }
    base.update(over)
    return base


@pytest.fixture
def make(settings, store):
    def _make(bot, webhook):
        return Notifier(settings, store, bot, webhook), store

    return _make


async def test_bot_is_preferred(make):
    bot, webhook = FakeBot(), FakeWebhook()
    notifier, _ = make(bot, webhook)

    result = await notifier.dispatch(incident())
    assert result["discord_transport"] == "bot"
    assert result["discord_message_id"] == "1409"
    assert bot.calls == [("send", None)]
    assert webhook.sent == 0


async def test_webhook_takes_over_when_the_bot_is_silent(make):
    bot, webhook = FakeBot(message_id=None), FakeWebhook()
    notifier, _ = make(bot, webhook)

    result = await notifier.dispatch(incident())
    assert result["discord_transport"] == "webhook"
    assert result["discord_message_id"] == "w1"
    assert webhook.sent == 1


async def test_the_webhook_sends_components_v2(make):
    bot, webhook = FakeBot(enabled=False), FakeWebhook()
    notifier, _ = make(bot, webhook)

    await notifier.dispatch(incident())
    assert webhook.components[0][0]["type"] == 17


async def test_everything_down_queues_then_replays(make):
    bot, webhook = FakeBot(enabled=False), FakeWebhook(enabled=False)
    notifier, store = make(bot, webhook)

    await notifier.dispatch(incident())
    assert await store.llen(keys.NOTIFY_QUEUE) == 1

    # Le webhook revient : la file se vide dans l'ordre.
    webhook.enabled = True
    assert await notifier.drain_queue() == 1
    assert await store.llen(keys.NOTIFY_QUEUE) == 0


async def test_queue_keeps_its_order_while_no_channel_is_back(make):
    bot, webhook = FakeBot(enabled=False), FakeWebhook(enabled=False)
    notifier, store = make(bot, webhook)

    await notifier.dispatch(incident(id="inc_1"))
    await notifier.dispatch(incident(id="inc_2"))
    assert await notifier.drain_queue() == 0
    queued = await store.lrange(keys.NOTIFY_QUEUE, 0, -1)
    assert '"inc_1"' in queued[0] and '"inc_2"' in queued[1]


async def test_a_retry_never_produces_two_messages(make):
    """Le bot a déjà relayé : le retry ne doit pas repartir par webhook."""
    bot, webhook = FakeBot(), FakeWebhook()
    notifier, _ = make(bot, webhook)

    sent = await notifier.dispatch(incident())
    await notifier.dispatch(sent)
    assert len(bot.calls) == 1
    assert webhook.sent == 0


async def test_a_new_update_is_edited_not_reposted(make):
    bot, webhook = FakeBot(), FakeWebhook()
    notifier, _ = make(bot, webhook)

    sent = await notifier.dispatch(incident())
    sent["updates"].append({"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": "Fix."})
    await notifier.dispatch(sent)
    assert bot.calls == [("send", None), ("edit", "1409")]


async def test_webhook_reposts_when_it_cannot_edit_its_own_message(make):
    bot, webhook = FakeBot(enabled=False), FakeWebhook(message_id=None)
    notifier, _ = make(bot, webhook)

    delivered = await notifier.deliver(
        incident(discord_message_id="1409", discord_transport="webhook")
    )
    assert webhook.edited == ["1409"]
    assert webhook.sent == 1
    assert delivered is False  # échec complet -> file de rattrapage


async def test_rate_limit_is_per_service_and_state(make):
    notifier, _ = make(FakeBot(), FakeWebhook())
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "down") is False
    assert await notifier.allow("moddy-bot", "degraded") is True
    assert await notifier.allow("moddy-api", "down") is True


async def test_reset_clears_the_rate_limit_on_recovery(make):
    notifier, _ = make(FakeBot(), FakeWebhook())
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "down") is False

    await notifier.reset("moddy-bot")
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "degraded") is True


async def test_a_bot_message_is_never_edited_by_the_webhook(make):
    """Le transport est collant : le webhook repost au lieu d'éditer."""
    bot, webhook = FakeBot(message_id=None), FakeWebhook()
    notifier, _ = make(bot, webhook)

    delivered = await notifier.deliver(
        incident(discord_message_id="1409", discord_transport="bot")
    )
    assert delivered is True
    assert webhook.edited == []  # jamais un PATCH sur le message d'un autre auteur
    assert webhook.sent == 1


async def test_the_bot_stays_out_of_a_webhook_thread(make):
    """Un message posté par webhook reste au webhook tant qu'il répond."""
    bot, webhook = FakeBot(), FakeWebhook()
    notifier, _ = make(bot, webhook)

    result = await notifier.deliver(
        incident(discord_message_id="w1", discord_transport="webhook")
    )
    assert result is True
    assert bot.calls == []
    assert webhook.edited == ["w1"]


async def test_the_bot_rescues_a_webhook_thread_when_the_webhook_dies(make):
    bot, webhook = FakeBot(), FakeWebhook(enabled=False)
    notifier, _ = make(bot, webhook)

    result = await notifier.deliver(
        incident(discord_message_id="w1", discord_transport="webhook")
    )
    assert result is True
    assert bot.calls == [("send", None)]


async def test_sticky_refresh_goes_to_the_bot(make):
    bot, webhook = FakeBot(), FakeWebhook()
    notifier, _ = make(bot, webhook)

    await notifier.refresh_sticky({"status": "operational"})
    assert bot.sticky == [{"status": "operational"}]

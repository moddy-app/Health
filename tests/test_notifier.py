"""Chaîne de redondance Discord : bot -> webhook -> file de rattrapage."""

from __future__ import annotations

import pytest

from app import keys
from app.core.notifier import Notifier


class FakeBus:
    def __init__(self, ack: str | None = None) -> None:
        self.ack = ack
        self.calls: list[tuple[str, dict]] = []

    async def notify(self, action, payload, expect_ack=True):
        self.calls.append((action, payload))
        return self.ack

    async def signal(self, action, payload=None):
        self.calls.append((action, payload or {}))
        return True


class FakeWebhook:
    def __init__(self, enabled: bool = True, message_id: str | None = "w1") -> None:
        self.enabled = enabled
        self.message_id = message_id
        self.sent = 0
        self.edited: list[str] = []

    async def send(self, components, embed=None):
        self.sent += 1
        return self.message_id

    async def edit(self, message_id, components, embed=None):
        self.edited.append(message_id)
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
    def _make(bus, webhook):
        return Notifier(settings, store, bus, webhook), store

    return _make


async def test_bot_is_preferred(make):
    bus, webhook = FakeBus(ack="1409"), FakeWebhook()
    notifier, _ = make(bus, webhook)

    result = await notifier.dispatch(incident())
    assert result["discord_transport"] == "bot"
    assert result["discord_message_id"] == "1409"
    assert bus.calls[0][0] == "incident.post"
    assert webhook.sent == 0


async def test_webhook_takes_over_when_the_bot_is_silent(make):
    bus, webhook = FakeBus(ack=None), FakeWebhook()
    notifier, _ = make(bus, webhook)

    result = await notifier.dispatch(incident())
    assert result["discord_transport"] == "webhook"
    assert result["discord_message_id"] == "w1"
    assert webhook.sent == 1


async def test_components_v2_flag_is_relayed_to_the_bot(make):
    bus, webhook = FakeBus(ack="1409"), FakeWebhook()
    notifier, _ = make(bus, webhook)

    await notifier.dispatch(incident())
    payload = bus.calls[0][1]
    assert payload["flags"] == 32768
    assert payload["components"][0]["type"] == 17


async def test_everything_down_queues_then_replays(make):
    bus, webhook = FakeBus(ack=None), FakeWebhook(enabled=False)
    notifier, store = make(bus, webhook)

    await notifier.dispatch(incident())
    assert await store.llen(keys.NOTIFY_QUEUE) == 1

    # Le webhook revient : la file se vide dans l'ordre.
    webhook.enabled = True
    assert await notifier.drain_queue() == 1
    assert await store.llen(keys.NOTIFY_QUEUE) == 0


async def test_queue_keeps_its_order_while_no_channel_is_back(make):
    bus, webhook = FakeBus(ack=None), FakeWebhook(enabled=False)
    notifier, store = make(bus, webhook)

    await notifier.dispatch(incident(id="inc_1"))
    await notifier.dispatch(incident(id="inc_2"))
    assert await notifier.drain_queue() == 0
    queued = await store.lrange(keys.NOTIFY_QUEUE, 0, -1)
    assert '"inc_1"' in queued[0] and '"inc_2"' in queued[1]


async def test_a_retry_never_produces_two_messages(make):
    """Le bot a déjà relayé : le retry ne doit pas repartir par webhook."""
    bus, webhook = FakeBus(ack="1409"), FakeWebhook()
    notifier, _ = make(bus, webhook)

    sent = await notifier.dispatch(incident())
    await notifier.dispatch(sent)
    assert len(bus.calls) == 1
    assert webhook.sent == 0


async def test_a_new_update_is_edited_not_reposted(make):
    bus, webhook = FakeBus(ack="1409"), FakeWebhook()
    notifier, _ = make(bus, webhook)

    sent = await notifier.dispatch(incident())
    sent["updates"].append({"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": "Fix."})
    await notifier.dispatch(sent)
    assert [call[0] for call in bus.calls] == ["incident.post", "incident.edit"]


async def test_webhook_reposts_when_it_cannot_edit_the_bot_message(make):
    bus, webhook = FakeBus(ack=None), FakeWebhook(message_id=None)
    notifier, _ = make(bus, webhook)

    delivered = await notifier.deliver(
        incident(discord_message_id="1409", discord_transport="webhook")
    )
    assert webhook.edited == ["1409"]
    assert webhook.sent == 1
    assert delivered is False  # échec complet -> file de rattrapage


async def test_rate_limit_is_per_service_and_state(make):
    notifier, _ = make(FakeBus(), FakeWebhook())
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "down") is False
    assert await notifier.allow("moddy-bot", "degraded") is True
    assert await notifier.allow("moddy-api", "down") is True


async def test_reset_clears_the_rate_limit_on_recovery(make):
    notifier, _ = make(FakeBus(), FakeWebhook())
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "down") is False

    await notifier.reset("moddy-bot")
    assert await notifier.allow("moddy-bot", "down") is True
    assert await notifier.allow("moddy-bot", "degraded") is True

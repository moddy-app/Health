"""Transport bot : publication, édition, et bascule dès que Discord traîne."""

from __future__ import annotations

import asyncio

import pytest

from app.bot.publisher import IncidentPublisher
from app.config import Settings
from app.render.model import IncidentPresentation

PRESENTATION = IncidentPresentation(
    title="Major Outage", level="major_outage", status="open", affected=["Moddy Bot"]
)


class FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits = 0

    async def edit(self, **_):
        self.edits += 1


class FakeChannel:
    def __init__(self, *, hang: bool = False, fail: bool = False) -> None:
        self.hang = hang
        self.fail = fail
        self.messages = {1409: FakeMessage(1409)}
        self.sent: list[FakeMessage] = []

    async def send(self, **_):
        if self.hang:
            await asyncio.sleep(10)
        if self.fail:
            raise RuntimeError("discord is down")
        message = FakeMessage(2000 + len(self.sent))
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id: int):
        message = self.messages.get(message_id)
        if message is None:
            raise LookupError("unknown message")
        return message


class FakeBot:
    def __init__(self, channel, ready: bool = True) -> None:
        self.channel = channel
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready

    def get_channel(self, _):
        return self.channel

    async def fetch_channel(self, _):  # pragma: no cover
        return self.channel


def make(channel, *, ready: bool = True, timeout: float = 5.0) -> IncidentPublisher:
    publisher = IncidentPublisher(
        Settings(redis_url="", discord_status_channel_id="42", hm_bot_ack_timeout=timeout)
    )
    publisher.bind(FakeBot(channel, ready))
    return publisher


async def test_a_published_incident_returns_its_message_id():
    channel = FakeChannel()
    assert await make(channel).send(PRESENTATION) == "2000"


async def test_an_edit_reuses_the_existing_message():
    channel = FakeChannel()
    assert await make(channel).edit("1409", PRESENTATION) is True
    assert channel.messages[1409].edits == 1
    assert channel.sent == []


async def test_a_deleted_message_cannot_be_edited():
    """Un 404 doit devenir un échec net : l'appelant reposte."""
    channel = FakeChannel()
    channel.messages.clear()
    assert await make(channel).edit("1409", PRESENTATION) is False


async def test_a_slow_discord_falls_back_instead_of_blocking():
    """La détection ne doit jamais attendre la gateway."""
    channel = FakeChannel(hang=True)
    assert await make(channel, timeout=0.01).send(PRESENTATION) is None


async def test_a_failing_send_falls_back_rather_than_raising():
    assert await make(FakeChannel(fail=True)).send(PRESENTATION) is None


@pytest.mark.parametrize("ready", [True, False])
async def test_a_disconnected_bot_is_not_enabled(ready):
    assert make(FakeChannel(), ready=ready).enabled is ready


async def test_without_a_channel_the_bot_stays_out_of_the_way():
    publisher = IncidentPublisher(Settings(redis_url="", discord_status_channel_id=""))
    publisher.bind(FakeBot(FakeChannel()))
    assert publisher.enabled is False
    assert await publisher.send(PRESENTATION) is None

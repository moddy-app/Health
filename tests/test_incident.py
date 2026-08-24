"""Cycle de vie des incidents : un seul actif, enrichi puis archivé."""

from __future__ import annotations

import pytest

from app import keys
from app.core.detector import DOWN, OPERATIONAL, ServiceState, Snapshot
from app.core.incident import TYPE_DEGRADED, TYPE_MAINTENANCE, IncidentManager
from app.integrations.betterstack import BetterStack
from app.render import colors
from app.util import iso


class StubNotifier:
    """Remplace la chaîne Discord : on teste la machine, pas le transport."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.allowed = True

    async def dispatch(self, incident, *, queue_on_failure=True):
        self.dispatched.append(incident)
        incident.setdefault("discord_message_id", "1409")
        incident.setdefault("discord_transport", "bot")
        return incident

    async def allow(self, service, status):
        return self.allowed

    async def refresh_sticky(self, public):
        return None


@pytest.fixture
def notifier():
    return StubNotifier()


@pytest.fixture
def manager(settings, store, notifier):
    return IncidentManager(settings, store, BetterStack(settings, store), notifier)


def snapshot(level: str, statuses: dict[str, str]) -> Snapshot:
    return Snapshot(
        level=level,
        updated_at=iso(),
        services={
            name: ServiceState(service=name, status=status) for name, status in statuses.items()
        },
    )


async def test_open_update_resolve(manager, notifier, store):
    await manager.reconcile(
        snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN, "moddy-api": OPERATIONAL})
    )
    incident = await manager.get_active()
    assert incident["origin"] == "auto"
    assert incident["level"] == colors.PARTIAL_OUTAGE
    assert incident["affected"] == ["moddy-bot"]
    assert incident["updates"][0]["kind"] == "created"

    # Un second service tombe : on enrichit l'incident, on n'en crée pas un autre.
    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN}))
    incident = await manager.get_active()
    assert incident["level"] == colors.MAJOR_OUTAGE
    assert incident["affected"] == ["moddy-bot", "moddy-api"]
    assert len(incident["updates"]) == 2

    # Tout revient : résolution puis archivage.
    await manager.reconcile(
        snapshot(colors.OPERATIONAL, {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL})
    )
    assert await manager.get_active() is None
    history = await manager.history()
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"]
    assert history[0]["updates"][-1]["kind"] == "resolved"


async def test_grace_period_blocks_every_alert(manager, notifier):
    snap = snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN})
    snap.in_grace = True
    await manager.reconcile(snap)
    assert await manager.get_active() is None
    assert notifier.dispatched == []


async def test_rate_limit_defers_the_alert(manager, notifier):
    notifier.allowed = False
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active() is None

    notifier.allowed = True
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active() is not None


async def test_unchanged_state_does_not_spam_updates(manager, notifier):
    snap = snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN})
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    assert len((await manager.get_active())["updates"]) == 1


async def test_degraded_is_never_published_on_the_status_page(manager, store):
    await manager.reconcile(snapshot(colors.DEGRADED, {"moddy-bot": "degraded"}))
    incident = await manager.get_active()
    assert incident["type"] == TYPE_DEGRADED
    assert incident["bs_report_id"] is None


async def test_staff_command_opens_then_resolves(manager):
    await manager.handle_command(
        "incident.create",
        {
            "title": "Manual incident",
            "message": "Looking into it.",
            "level": colors.PARTIAL_OUTAGE,
            "affected": ["moddy-api"],
            "author": "Jules",
        },
    )
    incident = await manager.get_active()
    assert incident["origin"] == "discord"
    assert incident["created_by"] == "Jules"

    await manager.handle_command("incident.update", {"message": "Fix deployed.", "author": "Jules"})
    assert len((await manager.get_active())["updates"]) == 2

    await manager.handle_command("incident.resolve", {"message": "Done.", "author": "Jules"})
    assert await manager.get_active() is None


async def test_second_create_command_enriches_the_active_incident(manager):
    payload = {"title": "A", "message": "m", "level": colors.PARTIAL_OUTAGE, "affected": ["moddy-api"]}
    await manager.handle_command("incident.create", payload)
    first_id = (await manager.get_active())["id"]
    await manager.handle_command("incident.create", {**payload, "title": "B"})
    active = await manager.get_active()
    assert active["id"] == first_id
    assert len(active["updates"]) == 2


async def test_maintenance_requires_ends_at(manager):
    assert await manager.handle_command("maintenance.create", {"title": "M", "message": "m"}) is None

    await manager.handle_command(
        "maintenance.create",
        {"title": "M", "message": "m", "affected": ["moddy-api"], "ends_at": iso()},
    )
    assert (await manager.get_active())["type"] == TYPE_MAINTENANCE


async def test_history_is_capped(manager, store):
    for index in range(3):
        await store.rpush(keys.INCIDENT_HISTORY, f'{{"id":"inc_{index}"}}')
    await store.ltrim(keys.INCIDENT_HISTORY, -2, -1)
    assert [i["id"] for i in await manager.history()] == ["inc_2", "inc_1"]

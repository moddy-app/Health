"""Machine à états, seuils anti faux-positifs, sévérité agrégée."""

from __future__ import annotations

import pytest

from app import keys
from app.core.detector import DOWN, OPERATIONAL, UNKNOWN, Detector
from app.render import colors
from app.util import iso


async def _beat(store, settings, service: str, status: str = "ok") -> None:
    await store.set_json(
        keys.hb(service),
        {"service": service, "status": status, "received_at": iso()},
        ttl=settings.hm_heartbeat_ttl,
    )


@pytest.fixture
async def detector(settings, store):
    det = Detector(settings, store)
    await det.load()
    return det


async def test_starts_unknown(detector):
    assert detector.states["moddy-bot"].status == UNKNOWN
    assert detector.aggregate() == colors.OPERATIONAL


async def test_recovery_needs_two_cycles(detector, store, settings):
    await _beat(store, settings, "moddy-bot")
    await _beat(store, settings, "moddy-api")

    await detector.run_cycle()
    assert detector.states["moddy-bot"].status == UNKNOWN  # 1 cycle OK, seuil = 2

    await detector.run_cycle()
    assert detector.states["moddy-bot"].status == OPERATIONAL


async def test_failure_needs_three_cycles(detector, store, settings):
    for _ in range(2):
        await _beat(store, settings, "moddy-bot")
        await _beat(store, settings, "moddy-api")
        await detector.run_cycle()
    assert detector.states["moddy-bot"].status == OPERATIONAL

    # Le heartbeat du bot expire.
    await store.delete(keys.hb("moddy-bot"))
    await detector.run_cycle()
    await detector.run_cycle()
    assert detector.states["moddy-bot"].status == OPERATIONAL  # 2 cycles < seuil

    snapshot = await detector.run_cycle()
    assert detector.states["moddy-bot"].status == DOWN
    assert snapshot.transitions == [("moddy-bot", OPERATIONAL, DOWN)]
    # Un seul service critique down sur deux.
    assert snapshot.level == colors.PARTIAL_OUTAGE


async def test_major_outage_when_all_critical_down(detector, store, settings):
    for _ in range(2):
        await _beat(store, settings, "moddy-bot")
        await _beat(store, settings, "moddy-api")
        await detector.run_cycle()

    await store.delete(keys.hb("moddy-bot"), keys.hb("moddy-api"))
    for _ in range(3):
        snapshot = await detector.run_cycle()
    assert snapshot.level == colors.MAJOR_OUTAGE
    assert snapshot.affected == ["moddy-bot", "moddy-api"]


async def test_service_declared_degraded_alerts_without_silence(detector, store, settings):
    """Le service émet toujours : exiger 60s de silence n'alerterait jamais."""
    for _ in range(3):
        await _beat(store, settings, "moddy-bot", status="degraded")
        await _beat(store, settings, "moddy-api")
        snapshot = await detector.run_cycle()

    assert detector.states["moddy-bot"].status == "degraded"
    assert snapshot.level == colors.DEGRADED


async def test_never_seen_service_ends_up_down(detector, store, settings):
    """Un service qui n'a jamais démarré ne doit pas rester du silence."""
    for _ in range(3):
        await _beat(store, settings, "moddy-api")
        snapshot = await detector.run_cycle()

    assert detector.states["moddy-bot"].status == DOWN
    assert snapshot.level == colors.PARTIAL_OUTAGE


async def test_grace_period_flags_snapshot(settings, store):
    settings.hm_startup_grace = 90
    det = Detector(settings, store)
    await det.load()
    snapshot = await det.run_cycle()
    assert snapshot.in_grace is True


async def test_public_payload_shape(detector, store, settings):
    await _beat(store, settings, "moddy-bot")
    await _beat(store, settings, "moddy-api")
    await detector.run_cycle()
    snapshot = await detector.run_cycle()

    payload = detector.public_payload(snapshot, None)
    assert payload["status"] == colors.OPERATIONAL
    assert payload["incident"] is None and payload["maintenance"] is None
    assert [s["id"] for s in payload["services"]] == ["moddy-bot", "moddy-api"]
    assert payload["services"][0]["name"] == "Moddy Bot"
    assert payload["services"][1]["name"] == "API"


async def test_public_payload_splits_maintenance(detector, settings, store):
    snapshot = detector.current_snapshot()
    incident = {
        "id": "inc_1",
        "type": "maintenance",
        "level": "maintenance",
        "title": "Scheduled Maintenance",
        "status": "open",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, incident)
    assert payload["incident"] is None
    assert payload["maintenance"]["title"] == "Scheduled Maintenance"

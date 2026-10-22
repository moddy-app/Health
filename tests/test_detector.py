"""Machine à états, seuils anti faux-positifs, sévérité agrégée."""

from __future__ import annotations

import pytest

from app import keys
from app.core.detector import DEGRADED, DOWN, OPERATIONAL, UNKNOWN, Detector, ServiceState, Snapshot
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
    assert snapshot.failing == ["moddy-bot", "moddy-api"]
    # Le site et le dashboard ne poussent pas de heartbeat mais sont dégradés
    # par ricochet.
    assert snapshot.affected == ["moddy-bot", "moddy-api", "moddy-website", "moddy-dashboard"]
    assert snapshot.collateral == ["moddy-website", "moddy-dashboard"]


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

    payload = detector.public_payload(snapshot, [])
    assert payload["status"] == colors.OPERATIONAL
    assert payload["incident"] is None and payload["maintenance"] is None
    assert [s["id"] for s in payload["services"]] == ["moddy-bot", "moddy-api"]
    assert payload["services"][0]["name"] == "Moddy Bot"
    assert payload["services"][1]["name"] == "API"


async def test_public_payload_follows_the_configured_display_order(store, settings):
    """L'ordre d'affichage est une variable d'environnement, pas l'ordre de surveillance."""
    settings.hm_services = "moddy-feeds,moddy-api,moddy-bot"
    settings.hm_service_order = "moddy-bot,moddy-api,moddy-feeds"
    det = Detector(settings, store)
    await det.load()
    snapshot = await det.run_cycle()

    payload = det.public_payload(snapshot, [])
    assert [s["id"] for s in payload["services"]] == ["moddy-bot", "moddy-api", "moddy-feeds"]


async def test_public_payload_splits_maintenance(detector, settings, store):
    snapshot = detector.current_snapshot()
    incident = {
        "id": "inc_1",
        "type": "maintenance",
        "level": "maintenance",
        "title": "Scheduled Maintenance",
        "status": "open",
        "starts_at": "2026-08-25T02:00:00Z",
        "ends_at": "2026-08-25T04:00:00Z",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    assert payload["incident"] is None
    assert payload["maintenance"]["title"] == "Scheduled Maintenance"
    # La fenêtre planifiée doit être lisible sans passer par le bot Discord.
    assert payload["maintenance"]["starts_at"] == "2026-08-25T02:00:00Z"
    assert payload["maintenance"]["ends_at"] == "2026-08-25T04:00:00Z"


async def test_public_payload_has_no_window_for_a_regular_incident(detector):
    """`starts_at`/`ends_at` n'existent que pour une maintenance planifiée."""
    snapshot = detector.current_snapshot()
    incident = {
        "id": "inc_2",
        "type": "incident",
        "level": "major_outage",
        "title": "Major Outage",
        "status": "open",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    assert payload["incident"]["starts_at"] is None
    assert payload["incident"]["ends_at"] is None


async def test_public_status_reflects_a_manual_incident_above_the_observed_level(
    detector, store, settings
):
    """Un incident ouvert à la main l'emporte si les heartbeats disent moins grave.

    `/status incident` peut annoncer `degraded` alors que le service se déclare
    toujours `operational` : le bandeau du sticky ne doit pas dire « All systems
    operational » juste au-dessus du titre de cet incident.
    """
    await _beat(store, settings, "moddy-bot", status="ok")
    await _beat(store, settings, "moddy-api", status="ok")
    await detector.run_cycle()
    snapshot = await detector.run_cycle()
    assert snapshot.level == colors.OPERATIONAL

    incident = {
        "id": "inc_3",
        "type": "incident",
        "level": colors.DEGRADED,
        "title": "Degraded Performance",
        "status": "open",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    assert payload["status"] == colors.DEGRADED


async def test_public_status_keeps_the_observed_level_when_more_severe(
    detector, store, settings
):
    """L'incident manuel ne peut pas *baisser* la sévérité affichée."""
    for _ in range(3):
        await _beat(store, settings, "moddy-bot", status="down")
        await _beat(store, settings, "moddy-api", status="down")
        snapshot = await detector.run_cycle()
    assert snapshot.level == colors.MAJOR_OUTAGE

    incident = {
        "id": "inc_4",
        "type": "incident",
        "level": colors.DEGRADED,
        "title": "Degraded Performance",
        "status": "open",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    assert payload["status"] == colors.MAJOR_OUTAGE


async def test_public_payload_floors_an_affected_service_still_reporting_ok(
    detector, store, settings
):
    """Un service cité dans un incident manuel ne reste pas « Operational ».

    Le service continue d'envoyer des heartbeats `ok` — il n'a pas conscience
    d'être en panne pour l'utilisateur — mais un incident a été ouvert à la
    main dessus (`/status incident`). Sans plancher par service, la ligne du
    sticky dirait « Operational » juste sous le titre de l'incident qui le
    cite : la priorité doit revenir à l'incident.
    """
    await _beat(store, settings, "moddy-bot", status="ok")
    await _beat(store, settings, "moddy-api", status="ok")
    await detector.run_cycle()
    snapshot = await detector.run_cycle()

    incident = {
        "id": "inc_6",
        "type": "incident",
        "level": colors.MAJOR_OUTAGE,
        "title": "Dashboard unavailable",
        "status": "open",
        "affected": ["moddy-api"],
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    services = {s["id"]: s for s in payload["services"]}
    assert services["moddy-api"]["status"] == "down"
    assert services["moddy-api"]["reported"] == "operational"
    # Un service non cité par l'incident garde son état observé.
    assert services["moddy-bot"]["status"] == "operational"


async def test_public_payload_lists_every_active_incident(detector):
    """Plusieurs incidents actifs à la fois : tous dans `incidents`, le plus
    sévère dans `incident` pour compatibilité descendante."""
    snapshot = detector.current_snapshot()
    minor = {
        "id": "inc_a",
        "type": "incident",
        "level": colors.DEGRADED,
        "title": "Minor thing",
        "status": "open",
        "affected": [],
        "updates": [],
    }
    major = {
        "id": "inc_b",
        "type": "incident",
        "level": colors.MAJOR_OUTAGE,
        "title": "Major thing",
        "status": "open",
        "affected": [],
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [minor, major])
    assert {i["id"] for i in payload["incidents"]} == {"inc_a", "inc_b"}
    assert payload["incident"]["id"] == "inc_b"
    assert payload["status"] == colors.MAJOR_OUTAGE


async def test_public_status_ignores_maintenance_level(detector, settings, store):
    """Une maintenance n'a jamais à faire monter le niveau agrégé (§ docs/incidents.md)."""
    snapshot = detector.current_snapshot()
    incident = {
        "id": "inc_5",
        "type": "maintenance",
        "level": colors.MAINTENANCE,
        "title": "Scheduled Maintenance",
        "status": "open",
        "starts_at": "2020-01-01T00:00:00Z",
        "ends_at": "2020-01-01T04:00:00Z",
        "updates": [],
    }
    payload = detector.public_payload(snapshot, [incident])
    assert payload["status"] == colors.OPERATIONAL


def _prod_like(store):
    """La configuration de production pendant l'incident Railway."""
    from app.config import Settings

    settings = Settings(
        redis_url="",
        hm_services="moddy-bot,moddy-api,moddy-altguard,moddy-feeds",
        hm_critical_services="moddy-bot,moddy-api",
        hm_probe_map="moddy-dashboard:https://dashboard.example/healthz,moddy-website:https://www.example",
        hm_bs_resource_map=(
            "moddy-bot:1,moddy-dashboard:2,moddy-website:3,moddy-api:4,"
            "moddy-altguard:5,moddy-feeds:6,moddy-ai:7"
        ),
        hm_startup_grace=0,
    )
    return settings, Detector(settings, store)


def _snapshot(det: Detector, statuses: dict[str, str]) -> Snapshot:
    effective, impacted_by = det.impact.apply(statuses)
    return Snapshot(
        level=colors.PARTIAL_OUTAGE,
        updated_at=iso(),
        services={n: ServiceState(service=n, status=s) for n, s in statuses.items()},
        effective=effective,
        impacted_by=impacted_by,
    )


async def test_the_sticky_shows_each_service_as_the_incident_declares_it(store):
    """Le cas de la production : un report `partial_outage` affichait tout en « Down ».

    Le site, donné en ligne sur la status page, et le bot, donné dégradé,
    héritaient du niveau de l'incident ; l'impact déduit de la panne de l'API
    ne doit pas non plus contredire ce que le staff a posé.
    """
    _, det = _prod_like(store)
    observed = {
        "moddy-bot": OPERATIONAL,
        "moddy-api": DOWN,
        "moddy-altguard": OPERATIONAL,
        "moddy-feeds": DOWN,
        "moddy-dashboard": OPERATIONAL,
        "moddy-website": OPERATIONAL,
    }
    incident = {
        "id": "inc",
        "type": "incident",
        "level": colors.PARTIAL_OUTAGE,
        "status": "updating",
        "affected": list(observed) + ["moddy-ai"],
        "statuses": {
            "moddy-bot": DEGRADED,
            "moddy-dashboard": DOWN,
            "moddy-website": OPERATIONAL,
            "moddy-api": DOWN,
            "moddy-altguard": DOWN,
            "moddy-feeds": DOWN,
            "moddy-ai": DOWN,
        },
        "updates": [],
    }

    payload = det.public_payload(_snapshot(det, observed), [incident])
    shown = {s["id"]: s["status"] for s in payload["services"]}
    assert shown == {
        "moddy-bot": DEGRADED,
        "moddy-dashboard": DOWN,
        "moddy-website": OPERATIONAL,
        "moddy-api": DOWN,
        "moddy-altguard": DOWN,
        "moddy-feeds": DOWN,
        "moddy-ai": DOWN,
    }


async def test_a_service_that_reports_itself_down_stays_down(store):
    """L'annonce du staff l'emporte sur l'impact déduit, jamais sur le service lui-même."""
    _, det = _prod_like(store)
    observed = {"moddy-bot": OPERATIONAL, "moddy-api": DOWN}
    incident = {
        "id": "inc",
        "type": "incident",
        "level": colors.DEGRADED,
        "status": "open",
        "affected": ["moddy-api"],
        "statuses": {"moddy-api": DEGRADED},
        "updates": [],
    }

    payload = det.public_payload(_snapshot(det, observed), [incident])
    shown = {s["id"]: s["status"] for s in payload["services"]}
    assert shown["moddy-api"] == DOWN


async def test_an_unmonitored_status_page_resource_is_listed_as_online(store):
    """AI Features n'envoie rien : il figure quand même, en ligne par défaut."""
    _, det = _prod_like(store)
    observed = {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL}

    payload = det.public_payload(_snapshot(det, observed), [])
    ai = next(s for s in payload["services"] if s["id"] == "moddy-ai")
    assert ai["name"] == "AI Features"
    assert ai["status"] == OPERATIONAL
    assert ai["reported"] is None
    assert payload["services"][-1]["id"] == "moddy-ai"


async def test_an_incident_without_per_service_states_still_floors_its_services(store):
    """Ouvert sans panneau de sévérité, un incident vaut pour tous ses services."""
    _, det = _prod_like(store)
    observed = {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL}
    incident = {
        "id": "inc",
        "type": "incident",
        "level": colors.PARTIAL_OUTAGE,
        "status": "open",
        "affected": ["moddy-ai"],
        "updates": [],
    }

    payload = det.public_payload(_snapshot(det, observed), [incident])
    shown = {s["id"]: s["status"] for s in payload["services"]}
    assert shown["moddy-ai"] == DOWN
    assert shown["moddy-bot"] == OPERATIONAL

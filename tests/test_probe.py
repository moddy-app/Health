"""Sonde HTTP : ce que le monitor écrit après avoir interrogé une URL publique."""

from __future__ import annotations

import pytest

from app import keys
from app.config import Settings
from app.core.detector import Detector
from app.core.probe import Probe
from app.util import iso

URL = "https://dashboard.moddy.app/healthz"


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeClient:
    """Doublure d'`httpx.AsyncClient` : renvoie ce qu'on lui a mis, ou lève."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def get(self, url, timeout=None, follow_redirects=True):
        self.calls.append(url)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture
def probe_settings():
    return Settings(
        redis_url="",
        hm_services="moddy-bot,moddy-api",
        hm_probe_map=f"moddy-dashboard:{URL}",
        hm_startup_grace=0,
        hm_min_silence=0,
        hm_failure_threshold=3,
        hm_recovery_threshold=2,
    )


async def written(store, service="moddy-dashboard") -> dict:
    return await store.get_json(keys.hb(service))


async def keep_alive(store, settings, *services: str) -> None:
    """Heartbeats des services qui poussent : sans eux, tout tombe par ricochet."""
    for service in services or ("moddy-bot", "moddy-api"):
        await store.set_json(
            keys.hb(service),
            {"service": service, "status": "ok", "received_at": iso()},
            ttl=settings.hm_heartbeat_ttl,
        )


# ----------------------------------------------------------------------
# La sonde
# ----------------------------------------------------------------------
async def test_a_200_writes_an_ok_heartbeat(probe_settings, store):
    probe = Probe(probe_settings, store, FakeClient(200))

    await probe.run_once()

    record = await written(store)
    assert record["status"] == "ok"
    assert record["service"] == "moddy-dashboard"
    assert record["checks"]["http"]["status_code"] == 200


async def test_the_url_is_the_one_configured(probe_settings, store):
    client = FakeClient(200)
    probe = Probe(probe_settings, store, client)

    await probe.run_once()

    # `_csv_map` coupe à la première `:` : le schéma de l'URL doit rester entier.
    assert client.calls == [URL]


@pytest.mark.parametrize("code", [301, 404, 500, 503])
async def test_anything_but_a_2xx_is_down(probe_settings, store, code):
    probe = Probe(probe_settings, store, FakeClient(code))

    await probe.run_once()

    assert (await written(store))["status"] == "down"


async def test_a_redirect_is_not_followed(probe_settings, store):
    """Le client HTTP partagé suit les redirections (index.json Better Stack) ;
    la sonde ne doit pas hériter de ce comportement et transformer un 3xx en `ok`."""

    class Redirecting(FakeClient):
        async def get(self, url, timeout=None, follow_redirects=True):
            assert follow_redirects is False
            return await super().get(url, timeout=timeout, follow_redirects=follow_redirects)

    probe = Probe(probe_settings, store, Redirecting(302))

    await probe.run_once()

    assert (await written(store))["status"] == "down"


async def test_an_unreachable_url_is_down_not_silence(probe_settings, store):
    """Une sonde qui échoue doit écrire, sinon la détection attend l'expiration du TTL."""
    probe = Probe(probe_settings, store, FakeClient(TimeoutError("timed out")))

    await probe.run_once()

    record = await written(store)
    assert record["status"] == "down"
    assert "timed out" in record["checks"]["http"]["error"]


async def test_a_broken_target_does_not_stop_the_others(probe_settings, store):
    settings = probe_settings.model_copy(
        update={"hm_probe_map": f"moddy-dashboard:{URL},moddy-website:https://moddy.app"}
    )

    class Exploding(FakeClient):
        async def get(self, url, timeout=None, follow_redirects=True):
            if "dashboard" in url:
                raise RuntimeError("boom")
            return await super().get(url, timeout=timeout, follow_redirects=follow_redirects)

    await Probe(settings, store, Exploding(200)).run_once()

    assert (await written(store, "moddy-website"))["status"] == "ok"


async def test_the_origin_of_a_synthetic_heartbeat_is_traceable(probe_settings, store):
    await Probe(probe_settings, store, FakeClient(200)).run_once()

    assert (await written(store))["meta"] == {"source": "probe", "url": URL}


# ----------------------------------------------------------------------
# Ce que la détection en fait
# ----------------------------------------------------------------------
def test_a_probed_service_is_monitored_without_being_declared_twice(probe_settings):
    assert probe_settings.services == ["moddy-bot", "moddy-api", "moddy-dashboard"]


def test_the_synthetic_heartbeat_outlives_three_probes(probe_settings):
    assert probe_settings.probe_ttl >= probe_settings.hm_probe_interval * 3


async def test_a_failing_probe_ends_up_down(probe_settings, store):
    """Une sonde en échec suit les mêmes seuils qu'un heartbeat poussé."""
    probe = Probe(probe_settings, store, FakeClient(503))
    detector = Detector(probe_settings, store)
    await detector.load()

    for _ in range(probe_settings.hm_failure_threshold):
        await keep_alive(store, probe_settings)
        await probe.run_once()
        snapshot = await detector.run_cycle()

    assert snapshot.services["moddy-dashboard"].status == "down"
    assert snapshot.effective["moddy-dashboard"] == "down"


async def test_a_healthy_probe_does_not_save_a_dashboard_without_backend(probe_settings, store):
    """La sonde ne dit que « la page répond » : sans l'API, le dashboard est down."""
    probe = Probe(probe_settings, store, FakeClient(200))
    detector = Detector(probe_settings, store)
    await detector.load()

    for _ in range(probe_settings.hm_failure_threshold):
        # Seul `moddy-api` ne pousse rien : il finit par tomber.
        await keep_alive(store, probe_settings, "moddy-bot")
        await probe.run_once()
        snapshot = await detector.run_cycle()

    assert snapshot.services["moddy-api"].status == "down"
    # Sa propre sonde le dit vivant...
    assert snapshot.services["moddy-dashboard"].status == "operational"
    # ...mais l'utilisateur, lui, n'a plus de dashboard.
    assert snapshot.effective["moddy-dashboard"] == "down"
    assert snapshot.impacted_by["moddy-dashboard"] == ["moddy-api"]


async def test_a_healthy_probe_keeps_the_dashboard_operational(probe_settings, store):
    probe = Probe(probe_settings, store, FakeClient(200))
    detector = Detector(probe_settings, store)
    await detector.load()

    for _ in range(probe_settings.hm_recovery_threshold + 1):
        await keep_alive(store, probe_settings)
        await probe.run_once()
        snapshot = await detector.run_cycle()

    assert snapshot.effective["moddy-dashboard"] == "operational"

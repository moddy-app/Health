"""Contrats HTTP : ingestion, webhook Better Stack, API publique."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import keys
from app.main import create_app

TOKEN = {"X-Health-Token": "test-token"}

HEARTBEAT = {
    "service": "moddy-bot",
    "status": "ok",
    "version": "1.4.2",
    "uptime_s": 84213,
    "checks": {"discord_gateway": {"ok": True, "latency_ms": 78}},
    "meta": {"shards": "3/3", "guilds": 3742},
}


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_health_has_no_dependency(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "ok"


def test_heartbeat_requires_the_token(client):
    assert client.post("/ingest/heartbeat", json=HEARTBEAT).status_code == 401
    assert (
        client.post("/ingest/heartbeat", json=HEARTBEAT, headers={"X-Health-Token": "nope"})
    ).status_code == 401


def test_heartbeat_is_stored_with_a_ttl(client):
    response = client.post("/ingest/heartbeat", json=HEARTBEAT, headers=TOKEN)
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is True
    assert body["incident_active"] is False
    assert body["received_at"].endswith("Z")

    store = client.app.state.ctx.store
    assert store._mem.ttl_left(keys.hb("moddy-bot")) is not None


def test_heartbeat_keeps_free_form_checks_untouched(client):
    client.post("/ingest/heartbeat", json=HEARTBEAT, headers=TOKEN)
    stored = client.app.state.ctx.store._mem.strings[keys.hb("moddy-bot")]
    assert "discord_gateway" in stored and "shards" in stored


def test_status_shape(client):
    body = client.get("/v1/status").json()
    assert set(body) == {"status", "updated_at", "services", "incident", "maintenance"}
    assert [s["id"] for s in body["services"]] == ["moddy-bot", "moddy-api"]
    assert set(body["services"][0]) == {
        "id",
        "name",
        "status",  # vécu par l'utilisateur, propagation comprise
        "reported",  # ce que le service dit de lui-même
        "impacted_by",
        "since",
    }


def test_status_is_cacheable_and_public(client):
    response = client.get("/v1/status")
    assert response.headers["cache-control"] == "public, max-age=30"


def test_banner_is_minimal(client):
    body = client.get("/v1/status/banner").json()
    assert set(body) == {"level", "title", "url", "message"}
    assert body["message"] is None


async def test_banner_message_is_generic_for_an_unrelated_caller(client):
    """Un consommateur non concerné n'a pas à savoir quel service interne est en cause."""
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Partial Outage – Moddy Bot Unavailable",
        message="m",
        level="partial_outage",
        affected=["moddy-bot"],
        origin="discord",
        url="https://status.moddy.app/incident/1",
    )
    # Le cache de `/v1/status` peut avoir été rempli sans incident par la
    # boucle de fond avant l'ouverture ci-dessus : on force le recalcul.
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-dashboard").json()
    assert body["message"] == (
        "**Some Moddy services** are currently unavailable. "
        "[View status](https://status.moddy.app/incident/1)"
    )
    assert "Moddy Bot" not in body["message"]


async def test_banner_message_names_the_caller_when_it_is_affected(client):
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Partial Outage – Moddy Bot Unavailable",
        message="m",
        level="partial_outage",
        affected=["moddy-bot"],
        origin="discord",
        url="https://status.moddy.app/incident/1",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-bot").json()
    assert body["message"] == (
        "**Moddy Bot** is currently unavailable. "
        "[View status](https://status.moddy.app/incident/1)"
    )


async def test_banner_message_reflects_degraded_and_maintenance(client):
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Degraded",
        message="m",
        level="degraded",
        affected=["moddy-api"],
        origin="discord",
        type_="degraded_performance",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-api").json()
    assert body["message"] == (
        "**API** is experiencing degraded performance. "
        "[View status](https://status.moddy.app)"
    )

    await ctx.incidents.resolve(message="fixed")
    await ctx.incidents.open(
        title="Maintenance",
        message="m",
        level="maintenance",
        affected=["moddy-api"],
        origin="discord",
        type_="maintenance",
        # Encadre "maintenant" très largement : le test doit rester vrai
        # quelle que soit la date d'exécution.
        starts_at="2020-01-01T00:00:00Z",
        ends_at="2099-01-01T00:00:00Z",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-api").json()
    assert body["message"] == (
        "**API** is undergoing scheduled maintenance, "
        "from 2020-01-01 00:00 to 2099-01-01 00:00 UTC. [View status](https://status.moddy.app)"
    )


async def test_banner_maintenance_window_spans_two_days(client):
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Maintenance",
        message="m",
        level="maintenance",
        affected=["moddy-api"],
        origin="discord",
        type_="maintenance",
        starts_at="2020-01-01T23:00:00Z",
        ends_at="2020-01-02T01:00:00Z",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner").json()
    assert body["message"] == (
        "**Some Moddy services** underwent scheduled maintenance, "
        "from 2020-01-01 23:00 to 2020-01-02 01:00 UTC. [View status](https://status.moddy.app)"
    )


async def test_banner_maintenance_without_a_window_says_nothing_about_it(client):
    """Une fenêtre à moitié écrite est pire que pas de fenêtre du tout."""
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Maintenance",
        message="m",
        level="maintenance",
        affected=["moddy-api"],
        origin="discord",
        type_="maintenance",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner").json()
    assert body["message"] == (
        "**Some Moddy services** are undergoing scheduled maintenance. "
        "[View status](https://status.moddy.app)"
    )


async def test_banner_announces_an_upcoming_maintenance_ahead_of_time(client):
    """`/status maintenance` rend l'incident actif dès sa création, même
    programmée pour plus tard : le message ne doit pas prétendre qu'elle est
    déjà en cours."""
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Maintenance",
        message="m",
        level="maintenance",
        affected=["moddy-api"],
        origin="discord",
        type_="maintenance",
        starts_at="2099-01-01T02:00:00Z",
        ends_at="2099-01-01T04:00:00Z",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-api").json()
    assert body["message"] == (
        "**API** will undergo scheduled maintenance, "
        "from 2099-01-01 02:00 to 04:00 UTC. [View status](https://status.moddy.app)"
    )


async def test_banner_reports_a_maintenance_left_unresolved_past_its_window(client):
    ctx = client.app.state.ctx
    await ctx.incidents.open(
        title="Maintenance",
        message="m",
        level="maintenance",
        affected=["moddy-api"],
        origin="discord",
        type_="maintenance",
        starts_at="2020-01-01T02:00:00Z",
        ends_at="2020-01-01T04:00:00Z",
    )
    await ctx.store.delete(keys.STATUS_PUBLIC)
    body = client.get("/v1/status/banner?service=moddy-api").json()
    assert body["message"] == (
        "**API** underwent scheduled maintenance, "
        "from 2020-01-01 02:00 to 04:00 UTC. [View status](https://status.moddy.app)"
    )


def test_rate_limit_kicks_in(client):
    codes = {client.get("/v1/status/banner").status_code for _ in range(62)}
    assert 429 in codes


def test_betterstack_webhook_answers_immediately_and_adopts(client):
    payload = {
        "event_type": "incident",
        "page": {"id": 237745, "status_indicator": "downtime"},
        "incident": {
            "id": 98765,
            "name": "Database connection issues",
            "shortlink": "https://status.moddy.app/98765/incidents",
            "incident_updates": [
                {"id": 22222, "body": "Still investigating", "created_at": "2026-01-15T11:45:00Z"},
                {"id": 11111, "body": "We are investigating", "created_at": "2026-01-15T10:30:00Z"},
            ],
        },
    }
    assert client.post("/ingest/betterstack", json=payload).status_code == 202

    store = client.app.state.ctx.store
    incident = store._mem.strings.get(keys.INCIDENT_ACTIVE)
    assert incident is not None
    assert "Database connection issues" in incident
    assert '"origin":"betterstack"' in incident
    # `shortlink` est utilisé tel quel plutôt que reconstruit.
    assert "https://status.moddy.app/98765/incidents" in incident


def test_betterstack_webhook_rejects_a_bad_key(client):
    client.app.state.ctx.settings.betterstack_webhook_secret = "s3cret"
    try:
        assert client.post("/ingest/betterstack?k=wrong", json={}).status_code == 403
    finally:
        client.app.state.ctx.settings.betterstack_webhook_secret = ""


def test_the_bot_no_longer_has_an_http_command_route(client):
    """Le bot vit dans ce process : il appelle `handle_command` directement."""
    assert client.post("/ingest/command", json={}, headers=TOKEN).status_code == 404


@pytest.mark.parametrize(
    "origin",
    [
        "https://moddy.app",
        # `moddy.app` (sans `www`) répond 307 vers `www.moddy.app` : c'est
        # cette origine que le navigateur envoie réellement.
        "https://www.moddy.app",
        "https://dashboard.moddy.app",
        "https://preview.moddy.app",  # URL réelle du dashboard
        # Un module qui n'existe pas encore aujourd'hui : le motif couvre tout
        # sous-domaine de moddy.app sans qu'on ait à l'énumérer.
        "https://some-future-module.moddy.app",
    ],
)
def test_cors_allows_any_moddy_app_subdomain(client, origin):
    response = client.get("/v1/status", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin


def test_cors_rejects_an_unrelated_origin(client):
    response = client.get("/v1/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers

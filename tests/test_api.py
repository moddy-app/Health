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
    assert set(body["services"][0]) == {"id", "name", "status", "since"}


def test_status_is_cacheable_and_public(client):
    response = client.get("/v1/status")
    assert response.headers["cache-control"] == "public, max-age=30"


def test_banner_is_minimal(client):
    body = client.get("/v1/status/banner").json()
    assert set(body) == {"level", "title", "url"}


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


def test_command_endpoint_is_the_bot_fallback(client):
    response = client.post(
        "/ingest/command",
        json={"action": "incident.resolve", "payload": {"message": "done", "author": "Jules"}},
        headers=TOKEN,
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "action": "incident.resolve"}

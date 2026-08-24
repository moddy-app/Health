"""Parsing d'`index.json`, mapping des ressources, anti-boucle."""

from __future__ import annotations

import pytest

from app.integrations.betterstack import BetterStack, parse_index, process_report
from app.render import colors

INDEX = {
    "data": {
        "id": "237745",
        "attributes": {"aggregate_state": "downtime"},
    },
    "included": [
        {
            "type": "status_report",
            "id": "995593",
            "attributes": {
                "title": "Major Outage",
                "report_type": "automatic",
                "published_at": "2026-08-24T19:42:00Z",
                # Piège n°1 : `ends_at` reste null même sur un report résolu.
                "ends_at": None,
            },
            "relationships": {"status_updates": {"data": [{"id": "1"}, {"id": "2"}]}},
        },
        {
            "type": "status_update",
            "id": "1",
            "attributes": {"message": "Investigating", "published_at": "2026-08-24T19:42:00Z"},
        },
        {
            "type": "status_update",
            "id": "2",
            "attributes": {"message": "Resolved", "published_at": "2026-08-24T20:15:00Z"},
        },
        {
            "type": "status_page_resource",
            "id": "8720238",
            "attributes": {"public_name": "Moddy Bot ", "status": "not_monitored"},
        },
    ],
}


def test_parse_index():
    snapshot = parse_index(INDEX)
    assert snapshot.aggregate_state == "downtime"
    assert len(snapshot.reports) == 1

    report = snapshot.reports[0]
    # Piège n°2 : `report_type` peut valoir `automatic`.
    assert report["report_type"] == "automatic"
    assert [u["message"] for u in report["updates"]] == ["Investigating", "Resolved"]
    # Le nom public de la ressource est nettoyé de ses espaces parasites.
    assert snapshot.resources["8720238"]["name"] == "Moddy Bot"
    assert snapshot.resources["8720238"]["status"] == "not_monitored"


def test_parse_index_tolerates_an_empty_page():
    snapshot = parse_index({})
    assert snapshot.aggregate_state == "operational"
    assert snapshot.reports == []


@pytest.fixture
def bs(settings, store):
    settings.hm_bs_resource_map = "moddy-bot:8720238,moddy-api:8720241"
    return BetterStack(settings, store)


def test_resources_skip_unmapped_services(bs):
    resources = bs.resources_for(
        ["moddy-bot", "moddy-feeds"], {"moddy-bot": "down"}, colors.MAJOR_OUTAGE
    )
    # `moddy-feeds` n'a pas de resource_id : on ne salit pas une barre voisine.
    assert resources == [{"status_page_resource_id": "8720238", "status": "downtime"}]


def test_resource_status_mapping(bs):
    statuses = {"moddy-bot": "degraded", "moddy-api": "operational"}
    resources = bs.resources_for(["moddy-bot", "moddy-api"], statuses, colors.DEGRADED)
    assert [r["status"] for r in resources] == ["degraded", "resolved"]


def test_maintenance_forces_the_maintenance_status(bs):
    resources = bs.resources_for(["moddy-bot"], {"moddy-bot": "down"}, colors.MAINTENANCE)
    assert resources[0]["status"] == "maintenance"


async def test_owned_updates_are_relayed_not_adopted(bs, store):
    await bs.mark_owned("995593")
    relayed, adopted = [], []

    async def on_owned(report, update):
        relayed.append(update["id"])

    async def on_foreign(report, update):
        adopted.append(update["id"])

    report = {"id": "995593"}
    updates = [{"id": "1", "message": "a"}, {"id": "2", "message": "b"}]
    await process_report(bs, report, updates, on_owned_update=on_owned, on_foreign_incident=on_foreign)
    assert relayed == ["1", "2"] and adopted == []

    # Écho webhook / retry : rien ne doit repartir.
    await process_report(bs, report, updates, on_owned_update=on_owned, on_foreign_incident=on_foreign)
    assert relayed == ["1", "2"]


async def test_foreign_incidents_are_adopted(bs):
    adopted = []

    async def on_foreign(report, update):
        adopted.append(update["id"])

    async def on_owned(report, update):
        raise AssertionError("ne devrait pas être appelé")

    await process_report(
        bs,
        {"id": "42"},
        [{"id": "9", "message": "hello"}],
        on_owned_update=on_owned,
        on_foreign_incident=on_foreign,
    )
    assert adopted == ["9"]


async def test_our_own_updates_are_pre_marked_as_seen(bs):
    """L'ID d'un update qu'on vient de créer est marqué dès la réponse 201."""
    await bs.mark_update_seen("777")
    called = []

    async def on_owned(report, update):
        called.append(update["id"])

    await bs.mark_owned("995593")
    await process_report(
        bs,
        {"id": "995593"},
        [{"id": "777", "message": "notre écriture"}],
        on_owned_update=on_owned,
        on_foreign_incident=on_owned,
    )
    assert called == []

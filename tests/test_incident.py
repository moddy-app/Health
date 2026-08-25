"""Cycle de vie des incidents : un seul actif, enrichi puis archivé."""

from __future__ import annotations

import pytest

from app import keys
from app.core.detector import DEGRADED, DOWN, OPERATIONAL, ServiceState, Snapshot
from app.core.impact import ImpactGraph
from app.core.incident import TYPE_DEGRADED, TYPE_MAINTENANCE, IncidentManager
from app.integrations.betterstack import BetterStack
from app.render import colors
from app.util import iso


class StubNotifier:
    """Remplace la chaîne Discord : on teste la machine, pas le transport."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.resets: list[str] = []
        self.allowed = True

    async def dispatch(self, incident, *, queue_on_failure=True):
        self.dispatched.append(incident)
        incident.setdefault("discord_message_id", "1409")
        incident.setdefault("discord_transport", "bot")
        return incident

    async def allow(self, service, status):
        return self.allowed

    async def reset(self, service):
        self.resets.append(service)

    async def refresh_sticky(self, public):
        return None


@pytest.fixture
def notifier():
    return StubNotifier()


@pytest.fixture
def manager(settings, store, notifier):
    return IncidentManager(settings, store, BetterStack(settings, store), notifier)


@pytest.fixture
def snapshot(settings):
    """Construit un Snapshot en passant par la vraie propagation d'impact."""
    graph = ImpactGraph(
        settings.hm_impact_map, settings.known_services, monitored=settings.services
    )

    def _snapshot(
        level: str,
        statuses: dict[str, str],
        transitions: list[tuple[str, str, str]] | None = None,
    ) -> Snapshot:
        effective, impacted_by = graph.apply(statuses)
        return Snapshot(
            level=level,
            updated_at=iso(),
            services={
                name: ServiceState(service=name, status=status)
                for name, status in statuses.items()
            },
            effective=effective,
            impacted_by=impacted_by,
            transitions=transitions or [],
        )

    return _snapshot


async def test_open_update_resolve(manager, notifier, store, snapshot):
    await manager.reconcile(
        snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN, "moddy-api": OPERATIONAL})
    )
    incident = await manager.get_active()
    assert incident["origin"] == "auto"
    assert incident["level"] == colors.PARTIAL_OUTAGE
    # Le bot tombe : tout le reste est dégradé par ricochet.
    assert incident["affected"] == [
        "moddy-bot",
        "moddy-api",
        "moddy-website",
        "moddy-dashboard",
    ]
    assert incident["updates"][0]["kind"] == "created"
    # Le titre ne nomme que la cause racine.
    assert incident["title"] == "Partial Outage – Moddy Bot Unavailable"
    assert "API, Website & Dashboard may be degraded as a result." in incident["message"]

    # Un second service tombe : on enrichit l'incident, on n'en crée pas un autre.
    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN}))
    incident = await manager.get_active()
    assert incident["level"] == colors.MAJOR_OUTAGE
    assert incident["title"] == "Partial Outage – Moddy Bot Unavailable"  # titre figé à l'ouverture
    assert len(incident["updates"]) == 2
    assert "Moddy Bot & API" in incident["updates"][-1]["message"]

    # Tout revient : résolution puis archivage.
    await manager.reconcile(
        snapshot(colors.OPERATIONAL, {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL})
    )
    assert await manager.get_active() is None
    history = await manager.history()
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"]
    assert history[0]["updates"][-1]["kind"] == "resolved"


async def test_grace_period_blocks_every_alert(manager, notifier, snapshot):
    snap = snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN})
    snap.in_grace = True
    await manager.reconcile(snap)
    assert await manager.get_active() is None
    assert notifier.dispatched == []


async def test_rate_limit_defers_the_alert(manager, notifier, snapshot):
    notifier.allowed = False
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active() is None

    notifier.allowed = True
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active() is not None


async def test_unchanged_state_does_not_spam_updates(manager, notifier, snapshot):
    snap = snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN})
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    assert len((await manager.get_active())["updates"]) == 1


async def test_a_degraded_incident_is_published_too(mapped_settings, store, notifier, snapshot):
    """Un service non-critique en `degraded` mérite la même visibilité qu'une
    panne majeure — la status page ne cache pas les petits soucis."""
    bs = BetterStack(mapped_settings, store)

    async def fake_request(method, path, payload=None):
        return {"data": {"id": "555", "relationships": {}}}

    bs._request = fake_request
    manager = IncidentManager(mapped_settings, store, bs, notifier)

    await manager.reconcile(snapshot(colors.DEGRADED, {"moddy-bot": "degraded"}))
    incident = await manager.get_active()
    assert incident["type"] == TYPE_DEGRADED
    assert incident["bs_report_id"] == "555"


async def test_recovery_gives_the_service_back_its_right_to_alert(manager, notifier, snapshot):
    """Sans ce reset, toute résolution ouvre un angle mort de 5 minutes."""
    await manager.reconcile(
        snapshot(
            colors.OPERATIONAL,
            {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL},
            transitions=[("moddy-bot", DOWN, OPERATIONAL)],
        )
    )
    assert notifier.resets == ["moddy-bot"]


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


# ----------------------------------------------------------------------
# Régressions observées en production (déploiement du 2026-08-25)
# ----------------------------------------------------------------------
async def test_a_stable_outage_stops_producing_updates(manager, snapshot, notifier):
    """Un état qui ne bouge pas ne produit qu'un seul message.

    En production, un incident adopté depuis Better Stack prenait un update
    toutes les 15 secondes : son niveau n'étant jamais réécrit, la garde de
    sortie de `reconcile` ne se refermait pas.
    """
    down = {"moddy-bot": DOWN, "moddy-api": DOWN}
    for _ in range(10):
        await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, down))

    assert len((await manager.get_active())["updates"]) == 1


async def test_an_adopted_incident_stops_producing_updates(manager, snapshot, store):
    """Le cas exact de la production : incident ouvert ailleurs, panne stable."""
    await manager.open(
        title="Billing issue",
        message="...",
        level=colors.PARTIAL_OUTAGE,
        affected=[],
        origin="betterstack",
        bs_report_id="995593",
    )
    down = {"moddy-bot": DOWN, "moddy-api": DOWN}
    for _ in range(10):
        await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, down))

    incident = await manager.get_active()
    # Un seul update : celui qui apporte réellement les services affectés.
    assert len(incident["updates"]) == 2
    # Le niveau d'un incident ouvert ailleurs n'est pas réécrit par la détection.
    assert incident["level"] == colors.PARTIAL_OUTAGE


async def test_a_real_transition_is_always_reported(manager, snapshot, notifier):
    """Le rate-limit ne doit pas avaler un vrai changement d'état.

    Il garde l'*ouverture* d'un incident ; une fois l'incident ouvert, c'est la
    signature de l'état observé qui décide, sinon une reprise de service passe
    à la trappe pendant cinq minutes.
    """
    notifier.allowed = False
    await manager.open(
        title="Ouvert à la main",
        message="...",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-bot", "moddy-api", "moddy-website", "moddy-dashboard"],
        origin="discord",
    )
    await manager.reconcile(
        snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN})
    )
    assert len((await manager.get_active())["updates"]) == 2


async def test_one_update_per_change_and_not_one_more(manager, snapshot):
    """Le comportement demandé : un update par changement réel, jamais de répétition."""
    steps = [
        # (état observé, updates attendus au total)
        ({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}, 1),   # ouverture
        ({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}, 1),   # rien n'a bougé
        ({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}, 1),
        ({"moddy-bot": DOWN, "moddy-api": DOWN}, 2),          # l'API tombe
        ({"moddy-bot": DOWN, "moddy-api": DOWN}, 2),          # rien n'a bougé
        ({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}, 3),   # l'API revient
        ({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}, 3),
    ]
    for statuses, expected in steps:
        level = colors.MAJOR_OUTAGE if statuses["moddy-api"] == DOWN else colors.PARTIAL_OUTAGE
        await manager.reconcile(snapshot(level, statuses))
        assert len((await manager.get_active())["updates"]) == expected


async def test_a_service_going_from_degraded_to_down_is_a_change(manager, snapshot):
    """`affected` ne distingue pas les deux : la signature, si."""
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DEGRADED}))
    before = len((await manager.get_active())["updates"])

    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN}))
    assert len((await manager.get_active())["updates"]) == before + 1


async def test_a_staff_update_is_never_deduplicated(manager):
    """Le staff a le droit de répéter : la garde ne vaut que pour l'automatique."""
    await manager.open(
        title="A", message="m", level=colors.PARTIAL_OUTAGE, affected=["moddy-api"], origin="discord"
    )
    await manager.add_update(message="m", author="Jules")
    await manager.add_update(message="m", author="Jules")
    assert len((await manager.get_active())["updates"]) == 3


class StubIndex:
    """Doublure de `poll_index` : `index.json` porte tout l'historique."""

    def __init__(self, reports: list[dict]) -> None:
        self.reports = reports
        self.resources: dict = {}
        self.aggregate_state = "operational"
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return self


def report(report_id: str, *, message: str, at: str) -> dict:
    return {
        "id": report_id,
        "title": "Billing issue",
        "report_type": "manual",
        "updated_at": at,
        "updates": [{"id": f"u{report_id}", "message": message, "published_at": at}],
    }


@pytest.fixture
def polling(settings, store, notifier):
    """Un manager dont le poll Better Stack sert un `index.json` fabriqué."""

    def _polling(reports: list[dict]) -> IncidentManager:
        bs = BetterStack(settings, store)
        bs.poll_index = StubIndex(reports)
        return IncidentManager(settings, store, bs, notifier)

    return _polling


async def test_the_first_poll_takes_the_history_for_granted(polling):
    """Le bug de production : un incident résolu en archive, rejoué au démarrage.

    `index.json` porte tout l'historique de la status page. Au premier poll,
    `hm:bs:seen_updates` est vide et chaque update d'archive passe pour neuf.
    """
    manager = polling([report("995593", message="Fully restored.", at=iso())])

    await manager.reconcile_betterstack()
    assert await manager.get_active() is None


async def test_an_archived_report_is_never_adopted(polling, store):
    """`ends_at` reste `null` même résolu : c'est l'âge du dernier mot qui tranche."""
    await store.set(keys.BS_CURSOR, iso())  # le poll d'amorçage a déjà eu lieu
    manager = polling([report("995593", message="Restored.", at="2026-01-01T00:00:00Z")])

    await manager.reconcile_betterstack()
    assert await manager.get_active() is None


async def test_a_fresh_foreign_incident_is_still_adopted(polling, store):
    await store.set(keys.BS_CURSOR, iso())
    manager = polling([report("1019848", message="We are investigating.", at=iso())])

    await manager.reconcile_betterstack()
    incident = await manager.get_active()
    assert incident is not None
    assert incident["origin"] == "betterstack"
    assert incident["bs_report_id"] == "1019848"


@pytest.fixture
def mapped_settings():
    """Une configuration où les ressources Better Stack sont mappées."""
    from app.config import Settings

    return Settings(
        redis_url="",
        hm_services="moddy-bot,moddy-api",
        hm_startup_grace=0,
        hm_bs_resource_map="moddy-bot:4242,moddy-api:4243",
        betterstack_token="token",
        betterstack_status_page_id="42",
    )


async def test_an_adopted_incident_names_its_affected_services(mapped_settings, store, notifier):
    """Un incident ouvert à la main sur Better Stack annonçait « Affected services: — ».

    Il ne connaît que des ressources de status page : sans traduction inverse,
    le message Discord partait sans le moindre service.
    """
    await store.set(keys.BS_CURSOR, iso())
    bs = BetterStack(mapped_settings, store)
    bs.poll_index = StubIndex(
        [
            {
                "id": "1019848",
                "title": "Moddy Is Down",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {
                        "id": "u1",
                        "message": "We are investigating.",
                        "published_at": iso(),
                        "affected_resources": [
                            {"status_page_resource_id": "4242", "status": "downtime"}
                        ],
                    }
                ],
            }
        ]
    )
    calls: list[tuple] = []

    async def _never(*args, **kwargs):
        calls.append(args)
        return None

    bs._request = _never

    manager = IncidentManager(mapped_settings, store, bs, notifier)
    await manager.reconcile_betterstack()

    incident = await manager.get_active()
    assert incident["affected"] == ["moddy-bot"]
    assert incident["level"] == colors.PARTIAL_OUTAGE
    # Le report existe déjà là-bas : lui renvoyer son propre message bouclerait.
    assert calls == []


def test_the_incident_url_has_no_locale_segment(manager):
    """La status page en tire sa propre langue : pas de `/en/` en dur."""
    url = manager._url_for("995593")
    assert url == "https://status.moddy.app/incident/995593"

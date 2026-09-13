"""Cycle de vie des incidents : plusieurs actifs à la fois, gérés indépendamment."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import keys
from app.core.detector import DEGRADED, DOWN, OPERATIONAL, ServiceState, Snapshot
from app.core.impact import ImpactGraph
from app.core.incident import TYPE_DEGRADED, TYPE_MAINTENANCE, IncidentManager
from app.integrations.betterstack import BetterStack
from app.render import colors
from app.util import iso, utcnow


class StubNotifier:
    """Remplace la chaîne Discord : on teste la machine, pas le transport."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.re_rendered: list[dict] = []
        self.resets: list[str] = []
        self.allowed = True

    async def dispatch(self, incident, *, queue_on_failure=True):
        self.dispatched.append(incident)
        incident.setdefault("discord_message_id", "1409")
        incident.setdefault("discord_transport", "bot")
        return incident

    async def re_render(self, incident):
        self.re_rendered.append(dict(incident))
        return True

    async def allow(self, service, status):
        return self.allowed

    async def reset(self, service):
        self.resets.append(service)

    async def refresh_sticky(self, public):
        return None


@pytest.fixture
def notifier():
    return StubNotifier()


def _impact_graph(settings) -> ImpactGraph:
    return ImpactGraph(settings.hm_impact_map, settings.known_services, monitored=settings.services)


@pytest.fixture
def manager(settings, store, notifier):
    return IncidentManager(settings, store, BetterStack(settings, store), notifier, _impact_graph(settings))


@pytest.fixture
def snapshot(settings):
    """Construit un Snapshot en passant par la vraie propagation d'impact."""
    graph = _impact_graph(settings)

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


async def _solo(manager: IncidentManager) -> dict:
    """L'unique incident actif — échoue s'il y en a zéro ou plusieurs."""
    actives = await manager.get_active_all()
    assert len(actives) == 1, actives
    return actives[0]


async def test_open_update_resolve(manager, notifier, store, snapshot):
    await manager.reconcile(
        snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN, "moddy-api": OPERATIONAL})
    )
    incident = await _solo(manager)
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

    # Un second service tombe, relié au premier par le graphe d'impact : on
    # enrichit l'incident, on n'en crée pas un autre.
    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN}))
    incident = await _solo(manager)
    assert incident["level"] == colors.MAJOR_OUTAGE
    assert incident["title"] == "Partial Outage – Moddy Bot Unavailable"  # titre figé à l'ouverture
    assert len(incident["updates"]) == 2
    assert "Moddy Bot & API" in incident["updates"][-1]["message"]

    # Tout revient : résolution puis archivage.
    await manager.reconcile(
        snapshot(colors.OPERATIONAL, {"moddy-bot": OPERATIONAL, "moddy-api": OPERATIONAL})
    )
    assert await manager.get_active_all() == []
    history = await manager.history()
    assert history[0]["status"] == "resolved"
    assert history[0]["resolved_at"]
    assert history[0]["updates"][-1]["kind"] == "resolved"


async def test_an_unrelated_service_opens_a_separate_incident(store, notifier):
    """Le cas signalé : deux pannes sans rapport ne doivent pas se mélanger.

    Sans `HM_IMPACT_MAP` pour les relier, `moddy-bot` et `moddy-api` tombant en
    même temps n'ont rien à voir l'un avec l'autre — chacun ouvre son propre
    incident, géré indépendamment.
    """
    from app.config import Settings

    settings = Settings(
        redis_url="",
        hm_services="moddy-bot,moddy-api",
        hm_critical_services="",
        hm_impact_map="",
        hm_failure_threshold=3,
        hm_recovery_threshold=2,
        hm_startup_grace=0,
        hm_min_silence=0,
    )
    manager = IncidentManager(settings, store, BetterStack(settings, store), notifier, _impact_graph(settings))
    graph = _impact_graph(settings)

    def snap(statuses: dict[str, str]) -> Snapshot:
        effective, impacted_by = graph.apply(statuses)
        return Snapshot(
            level=colors.DEGRADED,
            updated_at=iso(),
            services={n: ServiceState(service=n, status=s) for n, s in statuses.items()},
            effective=effective,
            impacted_by=impacted_by,
        )

    # Le dashboard (ici `moddy-bot`) tombe en premier.
    await manager.reconcile(snap({"moddy-bot": DOWN, "moddy-api": OPERATIONAL}))
    first = await manager.get_active_all()
    assert len(first) == 1
    assert first[0]["roots"] == ["moddy-bot"]

    # Un service sans rapport tombe à son tour : un second incident, distinct.
    await manager.reconcile(snap({"moddy-bot": DOWN, "moddy-api": DOWN}))
    actives = await manager.get_active_all()
    assert len(actives) == 2
    ids = {i["id"] for i in actives}
    assert first[0]["id"] in ids
    roots = {i["id"]: i["roots"] for i in actives}
    assert sorted(roots.values()) == [["moddy-api"], ["moddy-bot"]]

    # Le premier se rétablit : seul le sien se résout, l'autre continue sa vie.
    await manager.reconcile(snap({"moddy-bot": OPERATIONAL, "moddy-api": DOWN}))
    actives = await manager.get_active_all()
    assert len(actives) == 1
    assert actives[0]["roots"] == ["moddy-api"]


async def test_grace_period_blocks_every_alert(manager, notifier, snapshot):
    snap = snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN, "moddy-api": DOWN})
    snap.in_grace = True
    await manager.reconcile(snap)
    assert await manager.get_active_all() == []
    assert notifier.dispatched == []


async def test_rate_limit_defers_the_alert(manager, notifier, snapshot):
    notifier.allowed = False
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active_all() == []

    notifier.allowed = True
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN}))
    assert await manager.get_active_all() != []


async def test_unchanged_state_does_not_spam_updates(manager, notifier, snapshot):
    snap = snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DOWN})
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    await manager.reconcile(snap)
    assert len((await _solo(manager))["updates"]) == 1


async def test_a_degraded_incident_is_published_too(mapped_settings, store, notifier, snapshot):
    """Un service non-critique en `degraded` mérite la même visibilité qu'une
    panne majeure — la status page ne cache pas les petits soucis."""
    bs = BetterStack(mapped_settings, store)

    async def fake_request(method, path, payload=None):
        return {"data": {"id": "555", "relationships": {}}}

    bs._request = fake_request
    manager = IncidentManager(mapped_settings, store, bs, notifier, _impact_graph(mapped_settings))

    await manager.reconcile(snapshot(colors.DEGRADED, {"moddy-bot": "degraded"}))
    incident = await _solo(manager)
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
    incident = await manager.handle_command(
        "incident.create",
        {
            "title": "Manual incident",
            "message": "Looking into it.",
            "level": colors.PARTIAL_OUTAGE,
            "affected": ["moddy-api"],
            "author": "Jules",
        },
    )
    assert incident["origin"] == "discord"
    assert incident["created_by"] == "Jules"
    incident_id = incident["id"]

    await manager.handle_command(
        "incident.update",
        {"incident_id": incident_id, "message": "Fix deployed.", "author": "Jules"},
    )
    assert len((await manager.get_active(incident_id))["updates"]) == 2

    await manager.handle_command(
        "incident.resolve",
        {"incident_id": incident_id, "message": "Done.", "author": "Jules"},
    )
    assert await manager.get_active(incident_id) is None


async def test_second_create_command_always_opens_a_new_incident(manager):
    """`/status incident` n'enrichit plus jamais l'existant : plusieurs incidents
    gérés à la fois, chacun individuellement — voir `/status update`."""
    payload = {"title": "A", "message": "m", "level": colors.PARTIAL_OUTAGE, "affected": ["moddy-api"]}
    first = await manager.handle_command("incident.create", payload)
    second = await manager.handle_command("incident.create", {**payload, "title": "B"})
    assert first["id"] != second["id"]
    actives = await manager.get_active_all()
    assert {i["id"] for i in actives} == {first["id"], second["id"]}


async def test_maintenance_requires_ends_at(manager):
    assert await manager.handle_command("maintenance.create", {"title": "M", "message": "m"}) is None

    await manager.handle_command(
        "maintenance.create",
        {"title": "M", "message": "m", "affected": ["moddy-api"], "ends_at": iso()},
    )
    assert (await _solo(manager))["type"] == TYPE_MAINTENANCE


async def test_a_maintenance_absorbs_the_detection_instead_of_being_updated(manager, snapshot):
    """Pendant une maintenance, un service qui tombe est l'objet de l'opération.

    Constaté en production pendant la migration DNS : les services flappaient,
    et chaque flap collait un « We are currently experiencing a service outage »
    sous une maintenance annoncée — 28 updates, et autant de 422 côté Better
    Stack, qui refuse le mélange des deux.
    """
    await manager.handle_command(
        "maintenance.create",
        {
            "title": "DNS Migration",
            "message": "m",
            "affected": ["moddy-api"],
            "ends_at": iso(utcnow() + timedelta(hours=1)),
        },
    )
    for status in (DOWN, OPERATIONAL, DOWN):
        await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": status}))

    incident = await _solo(manager)
    assert incident["type"] == TYPE_MAINTENANCE
    assert len(incident["updates"]) == 1


async def test_a_finished_maintenance_gives_the_detection_back(manager, snapshot):
    """Une fois la fenêtre passée, la maintenance ne couvre plus rien.

    La laisser ouverte rendrait le monitor aveugle à la première vraie panne
    qui suit — et elle continuerait de traîner dans le salon.
    """
    await manager.handle_command(
        "maintenance.create",
        {
            "title": "DNS Migration",
            "message": "m",
            "affected": ["moddy-api"],
            "ends_at": iso(utcnow() - timedelta(minutes=1)),
        },
    )
    await manager.reconcile(snapshot(colors.OPERATIONAL, {"moddy-bot": OPERATIONAL}))
    assert await manager.get_active_all() == []
    assert (await manager.history())[0]["type"] == TYPE_MAINTENANCE

    # Et la détection reprend son cours sur l'incident suivant.
    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN}))
    incident = await _solo(manager)
    assert incident["origin"] == "auto"
    assert incident["type"] != TYPE_MAINTENANCE


async def test_closing_a_maintenance_early_closes_its_window_too(
    mapped_settings, store, notifier
):
    """Un report de maintenance n'est clos que par sa fenêtre.

    Sans ce PATCH, une maintenance terminée — ou annulée — depuis Discord
    continue d'être annoncée sur la status page jusqu'à l'heure prévue. Un
    `resolved` n'y changerait rien : le report ne l'accepte pas.
    """
    bs = BetterStack(mapped_settings, store)
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"data": {"id": "1032967", "relationships": {}}}

    bs._request = fake_request
    manager = IncidentManager(mapped_settings, store, bs, notifier, _impact_graph(mapped_settings))

    incident = await manager.handle_command(
        "maintenance.create",
        {
            "title": "DNS Migration",
            "message": "m",
            "affected": ["moddy-api"],
            "starts_at": iso(utcnow() + timedelta(hours=1)),
            "ends_at": iso(utcnow() + timedelta(hours=2)),
        },
    )
    await manager.handle_command(
        "incident.resolve",
        {"incident_id": incident["id"], "message": "Cancelled.", "author": "Jules"},
    )

    patches = [payload for method, _, payload in calls if method == "PATCH"]
    assert patches, "la fenêtre doit être refermée côté Better Stack"
    # Annulée avant d'avoir commencé : une fenêtre ne peut pas finir avant de
    # commencer, les deux bornes reviennent à maintenant.
    assert set(patches[0]) == {"starts_at", "ends_at"}


async def test_a_maintenance_whose_window_already_passed_is_not_patched(
    mapped_settings, store, notifier
):
    """Rien à refermer : la fenêtre a fait son travail toute seule."""
    bs = BetterStack(mapped_settings, store)
    calls: list[str] = []

    async def fake_request(method, path, payload=None):
        calls.append(method)
        return {"data": {"id": "1032967", "relationships": {}}}

    bs._request = fake_request
    manager = IncidentManager(mapped_settings, store, bs, notifier, _impact_graph(mapped_settings))

    incident = await manager.handle_command(
        "maintenance.create",
        {
            "title": "DNS Migration",
            "message": "m",
            "affected": ["moddy-api"],
            "starts_at": iso(utcnow() - timedelta(hours=2)),
            "ends_at": iso(utcnow() - timedelta(hours=1)),
        },
    )
    await manager.handle_command(
        "incident.resolve",
        {"incident_id": incident["id"], "message": "Done.", "author": "Jules"},
    )
    assert "PATCH" not in calls


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

    assert len((await _solo(manager))["updates"]) == 1


async def test_an_adopted_incident_stops_producing_updates(manager, snapshot, store):
    """Le cas exact de la production : incident ouvert ailleurs, panne stable."""
    await manager.open(
        title="Billing issue",
        message="...",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-bot", "moddy-api"],
        origin="betterstack",
        bs_report_id="995593",
    )
    down = {"moddy-bot": DOWN, "moddy-api": DOWN}
    for _ in range(10):
        await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, down))

    incident = await _solo(manager)
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
    assert len((await _solo(manager))["updates"]) == 2


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
        assert len((await _solo(manager))["updates"]) == expected


async def test_a_service_going_from_degraded_to_down_is_a_change(manager, snapshot):
    """`affected` ne distingue pas les deux : la signature, si."""
    await manager.reconcile(snapshot(colors.PARTIAL_OUTAGE, {"moddy-bot": DEGRADED}))
    before = len((await _solo(manager))["updates"])

    await manager.reconcile(snapshot(colors.MAJOR_OUTAGE, {"moddy-bot": DOWN}))
    assert len((await _solo(manager))["updates"]) == before + 1


async def test_a_staff_update_is_never_deduplicated(manager):
    """Le staff a le droit de répéter : la garde ne vaut que pour l'automatique."""
    incident = await manager.open(
        title="A", message="m", level=colors.PARTIAL_OUTAGE, affected=["moddy-api"], origin="discord"
    )
    await manager.add_update(incident["id"], message="m", author="Jules")
    await manager.add_update(incident["id"], message="m", author="Jules")
    assert len((await manager.get_active(incident["id"]))["updates"]) == 3


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


def report(report_id: str, *, message: str, at: str, aggregate_state: str | None = None) -> dict:
    return {
        "id": report_id,
        "title": "Billing issue",
        "report_type": "manual",
        "updated_at": at,
        "aggregate_state": aggregate_state,
        "updates": [{"id": f"u{report_id}", "message": message, "published_at": at}],
    }


@pytest.fixture
def polling(settings, store, notifier):
    """Un manager dont le poll Better Stack sert un `index.json` fabriqué."""

    def _polling(reports: list[dict]) -> IncidentManager:
        bs = BetterStack(settings, store)
        bs.poll_index = StubIndex(reports)
        return IncidentManager(settings, store, bs, notifier, _impact_graph(settings))

    return _polling


async def test_the_first_poll_takes_the_history_for_granted(polling):
    """Le bug de production : un incident résolu en archive, rejoué au démarrage.

    `index.json` porte tout l'historique de la status page. Au premier poll,
    `hm:bs:seen_updates` est vide et chaque update d'archive passe pour neuf.
    """
    manager = polling([report("995593", message="Fully restored.", at=iso())])

    await manager.reconcile_betterstack()
    assert await manager.get_active_all() == []


async def test_an_archived_report_is_never_adopted(polling, store):
    """`ends_at` reste `null` même résolu : c'est l'âge du dernier mot qui tranche."""
    await store.set(keys.BS_CURSOR, iso())  # le poll d'amorçage a déjà eu lieu
    manager = polling([report("995593", message="Restored.", at="2026-01-01T00:00:00Z")])

    await manager.reconcile_betterstack()
    assert await manager.get_active_all() == []


async def test_a_fresh_foreign_incident_is_still_adopted(polling, store):
    await store.set(keys.BS_CURSOR, iso())
    manager = polling([report("1019848", message="We are investigating.", at=iso())])

    await manager.reconcile_betterstack()
    incident = await _solo(manager)
    assert incident["origin"] == "betterstack"
    assert incident["bs_report_id"] == "1019848"


async def test_an_adopted_incident_carries_its_whole_history(polling, store):
    """Un incident adopté ne commence pas à sa dernière update.

    Constaté en production : un incident ouvert depuis la veille, repris après
    un redéploiement, s'est affiché sur Discord avec pour seul message « access
    appears to be recovering » — tout ce qui l'avait précédé avait disparu.
    """
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            {
                "id": "1019848",
                "title": "Dashboard : Authentication Issues",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {"id": "u2", "message": "Root cause identified.", "published_at": "2026-09-12T19:37:00Z"},
                    {"id": "u1", "message": "The dashboard is unavailable.", "published_at": "2026-09-12T16:23:00Z"},
                    {"id": "u3", "message": "Access is recovering.", "published_at": iso()},
                ],
            }
        ]
    )

    await manager.reconcile_betterstack()

    incident = await _solo(manager)
    # Triées du plus ancien au plus récent, quel que soit l'ordre d'index.json.
    assert [u["message"] for u in incident["updates"]] == [
        "The dashboard is unavailable.",
        "Root cause identified.",
        "Access is recovering.",
    ]
    assert [u["kind"] for u in incident["updates"]] == ["created", "updated", "updated"]
    # Le message d'ouverture est le premier mot de l'incident, pas le dernier.
    assert incident["message"] == "The dashboard is unavailable."


async def test_adopting_a_history_does_not_relay_it_a_second_time(polling, store):
    """Les updates reprises sont marquées vues : sans ça, `process_report`
    les rejouerait une par une juste après l'adoption."""
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            {
                "id": "1019848",
                "title": "Billing issue",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {"id": "u1", "message": "Investigating.", "published_at": "2026-09-12T16:23:00Z"},
                    {"id": "u2", "message": "Fix deployed.", "published_at": iso()},
                ],
            }
        ]
    )

    await manager.reconcile_betterstack()
    assert len((await _solo(manager))["updates"]) == 2

    # Un second poll ne doit rien ajouter : tout a déjà été vu.
    await manager.reconcile_betterstack()
    assert len((await _solo(manager))["updates"]) == 2


async def test_the_legacy_single_incident_format_is_migrated(manager, store):
    """Le bug du déploiement : `hm:incident:active` portait un incident *seul*.

    Lu comme une carte `{id: incident}`, ses propres champs passaient pour des
    entrées et étaient tous écartés — l'incident en cours disparaissait au
    redéploiement, sticky et commandes l'avaient oublié alors qu'il vivait
    toujours sur Better Stack.
    """
    legacy = {
        "id": "inc_20260912_1623",
        "title": "Dashboard : Authentication Issues",
        "level": colors.PARTIAL_OUTAGE,
        "origin": "discord",
        "affected": ["moddy-api"],
        "status": "open",
        "created_at": "2026-09-12T16:23:00Z",
        "bs_report_id": "1019848",
        "updates": [{"kind": "created", "at": "2026-09-12T16:23:00Z", "message": "m", "author": "Jules"}],
    }
    await store.set_json(keys.INCIDENT_ACTIVE, legacy)

    actives = await manager.get_active_all()
    assert [i["id"] for i in actives] == ["inc_20260912_1623"]
    assert (await manager.get_active("inc_20260912_1623"))["title"] == legacy["title"]

    # Migré une fois pour toutes, et un nouvel incident cohabite avec lui.
    assert await store.get_json(keys.INCIDENT_ACTIVE) == {"inc_20260912_1623": legacy}
    await manager.open(
        title="Autre chose", message="m", level=colors.DEGRADED, affected=["moddy-bot"], origin="discord"
    )
    assert len(await manager.get_active_all()) == 2


async def test_the_half_migrated_format_keeps_both_sides(manager, store):
    """La forme qu'a laissée le déploiement : l'ancien incident *et* ce que la
    version suivante a écrit par-dessus lui.

    Le code d'après lisait la clé, y trouvait un dictionnaire, et y ajoutait ses
    propres incidents comme s'il s'agissait d'une carte. Réparer l'ancien format
    sans voir ces entrées-là perdrait l'incident adopté depuis.
    """
    legacy = {
        "id": "inc_20260912_1623",
        "title": "Ancien incident",
        "level": colors.PARTIAL_OUTAGE,
        "origin": "discord",
        "status": "open",
        "created_at": "2026-09-12T16:23:00Z",
        "updates": [{"kind": "created", "at": "2026-09-12T16:23:00Z", "message": "m", "author": "Jules"}],
    }
    adopted = {
        "id": "inc_20260913_1125",
        "title": "Adopté depuis",
        "level": colors.PARTIAL_OUTAGE,
        "origin": "betterstack",
        "status": "open",
        "created_at": "2026-09-13T11:25:00Z",
        "updates": [{"kind": "created", "at": "2026-09-13T11:25:00Z", "message": "m", "author": "Better Stack"}],
    }
    await store.set_json(keys.INCIDENT_ACTIVE, {**legacy, adopted["id"]: adopted})

    actives = await manager.get_active_all()
    assert [i["id"] for i in actives] == [legacy["id"], adopted["id"]]
    # L'ancien incident ne garde pas l'autre collé dans ses propres champs.
    assert adopted["id"] not in actives[0]


async def test_sync_updates_follows_a_severity_edited_on_better_stack(polling, notifier):
    """Passer une ressource de `degraded` à `downtime` là-bas doit remonter ici.

    Recharger les seuls textes laissait le message Discord annoncer « Degraded
    Performance » sous une ressource devenue `downtime`.
    """
    manager = polling(
        [
            {
                "id": "995593",
                "title": "Feeds",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {
                        "id": "u1",
                        "message": "Feeds is unavailable.",
                        "published_at": iso(),
                        "affected_resources": [
                            {"status_page_resource_id": "4242", "status": "downtime"}
                        ],
                    }
                ],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242"
    await manager.open(
        title="Feeds",
        message="m",
        level=colors.DEGRADED,
        affected=["moddy-bot"],
        origin="betterstack",
        bs_report_id="995593",
    )

    synced = await manager.sync_updates()

    assert synced[0]["level"] == colors.PARTIAL_OUTAGE
    assert synced[0]["type"] == "incident"  # plus `degraded_performance`


async def test_sync_updates_reloads_a_title_renamed_on_better_stack(polling):
    """Renommer le report sur la status page doit atteindre Discord.

    Le titre est figé à l'ouverture — mais un renommage par le staff est
    délibéré, et `/status reload` existe pour que les deux disent la même chose.
    """
    manager = polling(
        [
            {
                "id": "995593",
                "title": "Dashboard : Authentication & Server Access Issues",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [{"id": "u1", "message": "Investigating.", "published_at": iso()}],
            }
        ]
    )
    incident = await manager.open(
        title="Dashboard down",
        message="Investigating.",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-api"],
        origin="auto",  # même un incident auto : rien ne réécrit son titre en face
        bs_report_id="995593",
    )

    synced = await manager.sync_updates()

    assert synced[0]["title"] == "Dashboard : Authentication & Server Access Issues"
    assert (await manager.get_active(incident["id"]))["title"] == synced[0]["title"]


async def test_sync_updates_leaves_an_auto_incident_level_alone(polling):
    """La détection possède le niveau d'un incident `auto` : le reprendre de
    Better Stack ne ferait qu'un aller-retour à chaque cycle."""
    manager = polling(
        [
            {
                "id": "995593",
                "title": "Feeds",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {
                        "id": "u1",
                        "message": "Feeds is unavailable.",
                        "published_at": iso(),
                        "affected_resources": [
                            {"status_page_resource_id": "4242", "status": "downtime"}
                        ],
                    }
                ],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242"
    await manager.open(
        title="Feeds",
        message="m",
        level=colors.DEGRADED,
        affected=["moddy-bot"],
        origin="auto",
        bs_report_id="995593",
    )

    synced = await manager.sync_updates()
    assert synced[0]["level"] == colors.DEGRADED


def _resolved_update(update_id: str, message: str, *, resource: str = "4242") -> dict:
    """Une résolution Better Stack : toutes les ressources affectées revenues."""
    return {
        "id": update_id,
        "message": message,
        "published_at": iso(),
        "affected_resources": [{"status_page_resource_id": resource, "status": "resolved"}],
    }


async def test_a_resolution_made_on_better_stack_closes_the_incident(polling, store, notifier):
    """Le cas signalé : résolu là-bas, l'incident repassait en rouge ici.

    Une résolution n'a pas d'endpoint dédié côté Better Stack : c'est un update
    dont chaque ressource est `resolved`. Relayé comme un update ordinaire, il
    remettait le message Discord en « On Going » sous un incident pourtant clos.
    """
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            {
                "id": "1019848",
                "title": "Dashboard : Authentication Issues",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [_resolved_update("u9", "Everything is back to normal.")],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242"
    incident = await manager.open(
        title="Dashboard : Authentication Issues",
        message="The dashboard is unavailable.",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-bot"],
        origin="betterstack",
        bs_report_id="1019848",
    )

    await manager.reconcile_betterstack()

    assert await manager.get_active(incident["id"]) is None
    archived = (await manager.history())[0]
    assert archived["status"] == "resolved"
    assert archived["updates"][-1]["kind"] == "resolved"
    assert archived["updates"][-1]["message"] == "Everything is back to normal."


async def test_a_resolved_report_is_never_adopted(polling, store):
    """Clos là-bas avant d'arriver ici : il n'y a plus rien à annoncer."""
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            {
                "id": "1019848",
                "title": "Billing issue",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [_resolved_update("u9", "Fully restored.")],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242"

    await manager.reconcile_betterstack()
    assert await manager.get_active_all() == []
    # `/status reload` ne le ressuscite pas davantage.
    assert await manager.adopt_missing() == []


async def test_sync_updates_closes_an_incident_resolved_on_better_stack(polling, notifier):
    """`/status reload` sur un incident résolu là-bas le clôt, sans le repeindre."""
    manager = polling(
        [
            {
                "id": "995593",
                "title": "Feeds",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {"id": "u1", "message": "Feeds is unavailable.", "published_at": "2026-09-13T08:41:00Z"},
                    _resolved_update("u2", "Feeds is back."),
                ],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242"
    incident = await manager.open(
        title="Feeds",
        message="Feeds is unavailable.",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-bot"],
        origin="betterstack",
        bs_report_id="995593",
    )

    synced = await manager.sync_updates()

    assert synced[0]["status"] == "resolved"
    assert await manager.get_active(incident["id"]) is None
    # L'historique garde le premier mot, et la résolution ne s'y écrit qu'une fois.
    messages = [u["message"] for u in synced[0]["updates"]]
    assert messages == ["Feeds is unavailable.", "Feeds is back."]


async def test_a_partial_recovery_does_not_close_anything(polling, store):
    """Une ressource revenue sur deux n'est pas une résolution."""
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            {
                "id": "1019848",
                "title": "Billing issue",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {
                        "id": "u9",
                        "message": "The API is back, the bot is still down.",
                        "published_at": iso(),
                        "affected_resources": [
                            {"status_page_resource_id": "4242", "status": "resolved"},
                            {"status_page_resource_id": "4243", "status": "downtime"},
                        ],
                    }
                ],
            }
        ]
    )
    manager._s.hm_bs_resource_map = "moddy-bot:4242,moddy-api:4243"

    await manager.reconcile_betterstack()
    incident = await _solo(manager)
    assert incident["status"] == "open"
    assert incident["level"] == colors.PARTIAL_OUTAGE


async def test_adopt_missing_recovers_an_incident_the_bootstrap_swallowed(polling):
    """Le cas signalé : un incident en cours côté Better Stack, jamais chargé
    ici — parce que le tout premier poll l'a pris pour de l'archive. `reload`
    doit quand même l'envoyer, sans attendre que le webhook ou le poll
    automatique ne le revoient jamais (ils ne le reverront pas : ses updates
    sont déjà marqués vus)."""
    manager = polling([report("995593", message="Still ongoing.", at=iso())])

    await manager.reconcile_betterstack()  # premier poll : avalé sans être chargé
    assert await manager.get_active_all() == []

    adopted = await manager.adopt_missing()
    assert len(adopted) == 1
    assert adopted[0]["bs_report_id"] == "995593"
    incident = await _solo(manager)
    assert incident["origin"] == "betterstack"

    # Rejoué, il ne duplique pas : l'incident est maintenant suivi.
    assert await manager.adopt_missing() == []


async def test_adopt_missing_skips_a_report_actually_resolved(polling):
    """Ce qui est réellement clos côté Better Stack ne doit pas être rouvert ici."""
    manager = polling(
        [report("995593", message="Restored.", at="2026-01-01T00:00:00Z", aggregate_state="resolved")]
    )

    await manager.reconcile_betterstack()  # avalé par l'amorçage, comme ci-dessus
    assert await manager.adopt_missing() == []
    assert await manager.get_active_all() == []


async def test_adopt_missing_ignores_an_incident_already_tracked_locally(polling, store):
    """Ne réadopte pas ce qui est déjà suivi — `sync_updates` s'en charge."""
    await store.set(keys.BS_CURSOR, iso())
    manager = polling([report("995593", message="Investigating.", at=iso())])
    await manager.open(
        title="Billing issue",
        message="Investigating.",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-api"],
        origin="discord",
        bs_report_id="995593",
    )

    assert await manager.adopt_missing() == []
    assert len(await manager.get_active_all()) == 1


async def test_a_second_foreign_incident_is_adopted_independently(polling, store):
    """Plusieurs incidents Better Stack peuvent être actifs à la fois."""
    await store.set(keys.BS_CURSOR, iso())
    manager = polling(
        [
            report("1019848", message="We are investigating.", at=iso()),
            report("1019849", message="Something else entirely.", at=iso()),
        ]
    )

    await manager.reconcile_betterstack()
    actives = await manager.get_active_all()
    assert {i["bs_report_id"] for i in actives} == {"1019848", "1019849"}


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

    manager = IncidentManager(mapped_settings, store, bs, notifier, _impact_graph(mapped_settings))
    await manager.reconcile_betterstack()

    incident = await _solo(manager)
    assert incident["affected"] == ["moddy-bot"]
    assert incident["level"] == colors.PARTIAL_OUTAGE
    # Le report existe déjà là-bas : lui renvoyer son propre message bouclerait.
    assert calls == []


def test_the_incident_url_has_no_locale_segment(manager):
    """La status page en tire sa propre langue : pas de `/en/` en dur."""
    url = manager._url_for("995593")
    assert url == "https://status.moddy.app/incident/995593"


async def test_sync_updates_reloads_a_correction_made_on_better_stack(polling, notifier):
    """Le bug signalé : éditer une update sur Better Stack ne repasse jamais
    par le webhook — seul un ID *nouveau* déclenche l'anti-boucle."""
    manager = polling(
        [
            {
                "id": "995593",
                "title": "Billing issue",
                "report_type": "manual",
                "updated_at": iso(),
                "updates": [
                    {"id": "u1", "message": "Investigating.", "published_at": "2026-08-24T19:42:00Z"},
                    {
                        "id": "u2",
                        "message": "Fix deployed (corrected wording).",
                        "published_at": "2026-08-24T19:55:00Z",
                    },
                ],
            }
        ]
    )
    await manager.open(
        title="Billing issue",
        message="Investigating.",
        level=colors.PARTIAL_OUTAGE,
        affected=["moddy-api"],
        origin="discord",
        bs_report_id="995593",
    )

    synced = await manager.sync_updates()

    assert len(synced) == 1
    incident = synced[0]
    assert [u["message"] for u in incident["updates"]] == [
        "Investigating.",
        "Fix deployed (corrected wording).",
    ]
    assert incident["updates"][0]["kind"] == "created"
    assert incident["updates"][1]["kind"] == "updated"
    assert notifier.re_rendered  # le message a bien été réédité, hors anti-doublon


async def test_sync_updates_without_a_report_does_nothing(polling, notifier):
    manager = polling([])
    await manager.open(
        title="A", message="m", level=colors.PARTIAL_OUTAGE, affected=["moddy-api"], origin="discord"
    )
    assert await manager.sync_updates() == []
    assert notifier.re_rendered == []


async def test_sync_updates_without_an_active_incident_does_nothing(polling, notifier):
    manager = polling([])
    assert await manager.sync_updates() == []
    assert notifier.re_rendered == []

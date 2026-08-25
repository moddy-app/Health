"""Propagation d'impact — les quatre règles produit, vérifiées une par une."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.core.impact import ImpactGraph, parse_rules

ALL = ["moddy-bot", "moddy-api", "moddy-altguard", "moddy-feeds"]


@pytest.fixture
def prod_settings():
    """La configuration réelle : services surveillés + ressources Better Stack."""
    return Settings(
        redis_url="",
        hm_services=",".join(ALL),
        hm_bs_resource_map=(
            "moddy-bot:8720238,moddy-website:8720239,"
            "moddy-dashboard:8720240,moddy-api:8720241"
        ),
    )


@pytest.fixture
def graph(prod_settings):
    return ImpactGraph(
        prod_settings.hm_impact_map,
        prod_settings.known_services,
        monitored=prod_settings.services,
    )


def down(**over: str) -> dict[str, str]:
    """États observés : tout opérationnel sauf ce qu'on précise."""
    return {service: over.get(service, "operational") for service in ALL}


# ----------------------------------------------------------------------
# Les règles produit
# ----------------------------------------------------------------------
def test_bot_down_degrades_everything(graph):
    effective, impacted_by = graph.apply(down(**{"moddy-bot": "down"}))

    assert effective["moddy-bot"] == "down"
    assert effective["moddy-api"] == "degraded"
    assert effective["moddy-altguard"] == "degraded"
    assert effective["moddy-feeds"] == "degraded"
    assert effective["moddy-website"] == "degraded"
    assert effective["moddy-dashboard"] == "degraded"
    assert impacted_by["moddy-api"] == ["moddy-bot"]


def test_api_down_takes_the_dashboard_down_with_it(graph):
    """Sans son backend, le dashboard n'affiche plus rien : il est down, pas dégradé."""
    effective, impacted_by = graph.apply(down(**{"moddy-api": "down"}))

    assert effective["moddy-api"] == "down"
    assert effective["moddy-dashboard"] == "down"
    assert impacted_by["moddy-dashboard"] == ["moddy-api"]


def test_api_down_degrades_website_and_bot(graph):
    effective, impacted_by = graph.apply(down(**{"moddy-api": "down"}))

    assert effective["moddy-website"] == "degraded"
    assert effective["moddy-bot"] == "degraded"
    # Les petits services ne dépendent pas de l'API.
    assert effective["moddy-altguard"] == "operational"
    assert effective["moddy-feeds"] == "operational"
    assert impacted_by["moddy-bot"] == ["moddy-api"]


def test_dashboard_down_impacts_nobody(graph):
    # Le dashboard n'est ici surveillé par personne : on force son état à la main.
    observed = {**down(), "moddy-dashboard": "down"}
    effective, impacted_by = graph.apply(observed)

    assert effective["moddy-dashboard"] == "down"
    assert impacted_by == {}
    assert all(effective[s] == "operational" for s in ALL)


def test_small_services_do_not_touch_the_big_three(graph):
    effective, impacted_by = graph.apply(
        down(**{"moddy-altguard": "down", "moddy-feeds": "down"})
    )

    assert effective["moddy-bot"] == "operational"
    assert effective["moddy-api"] == "operational"
    # Le site n'émet pas de heartbeat : sans impact, le monitor n'a rien à en dire.
    assert effective["moddy-website"] == "unknown"
    assert impacted_by == {}


# ----------------------------------------------------------------------
# Les garde-fous
# ----------------------------------------------------------------------
def test_degraded_does_not_propagate(graph):
    """Sinon un simple hoquet du bot repeindrait toute la status page."""
    effective, impacted_by = graph.apply(down(**{"moddy-bot": "degraded"}))

    assert effective["moddy-bot"] == "degraded"
    assert effective["moddy-api"] == "operational"
    assert impacted_by == {}


def test_propagation_creates_a_down_only_where_the_rule_says_so(graph):
    """Le joker du bot dégrade ; seule une cible marquée `=down` tombe."""
    effective, _ = graph.apply(down(**{"moddy-bot": "down"}))
    assert list(effective.values()).count("down") == 1


def test_an_observed_down_is_never_overwritten(graph):
    effective, impacted_by = graph.apply(down(**{"moddy-bot": "down", "moddy-api": "down"}))
    assert effective["moddy-api"] == "down"
    assert "moddy-api" not in impacted_by


def test_an_own_failure_beats_the_propagated_one(graph):
    """Le dashboard tombé de son propre chef reste sa propre cause, pas un dégât collatéral."""
    observed = {**down(**{"moddy-api": "down"}), "moddy-dashboard": "down"}
    effective, impacted_by = graph.apply(observed)

    assert effective["moddy-dashboard"] == "down"
    assert "moddy-dashboard" not in impacted_by


def test_a_propagated_down_does_not_propagate_in_turn(prod_settings):
    """Un seul saut : le `down` dérivé du dashboard ne repart pas comme source."""
    graph = ImpactGraph(
        "moddy-api>moddy-dashboard=down;moddy-dashboard>moddy-feeds=down",
        prod_settings.known_services,
        monitored=prod_settings.services,
    )
    effective, _ = graph.apply(down(**{"moddy-api": "down"}))

    assert effective["moddy-dashboard"] == "down"
    assert effective["moddy-feeds"] == "operational"


def test_a_down_rule_upgrades_an_already_degraded_target(graph):
    """Deux sources, la plus sévère l'emporte, et les deux sont créditées."""
    effective, impacted_by = graph.apply(down(**{"moddy-bot": "down", "moddy-api": "down"}))

    assert effective["moddy-dashboard"] == "down"
    assert impacted_by["moddy-dashboard"] == ["moddy-bot", "moddy-api"]


def test_two_sources_are_both_credited(graph):
    _, impacted_by = graph.apply(down(**{"moddy-bot": "down", "moddy-api": "down"}))
    assert impacted_by["moddy-website"] == ["moddy-bot", "moddy-api"]


def test_a_monitored_service_without_data_stays_unknown(graph):
    """Déclarer dégradé un service dont on n'a aucune nouvelle serait infondé."""
    observed = {**down(**{"moddy-bot": "down"}), "moddy-feeds": "unknown"}
    effective, impacted_by = graph.apply(observed)

    assert effective["moddy-feeds"] == "unknown"
    assert "moddy-feeds" not in impacted_by
    # Le site, lui, n'émet pas de heartbeat : il est bien dégradé.
    assert effective["moddy-website"] == "degraded"


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def test_parse_rules():
    assert parse_rules("a>b,c;d>*") == {
        "a": {"b": "degraded", "c": "degraded"},
        "d": {"*": "degraded"},
    }
    assert parse_rules("  a > b ; ") == {"a": {"b": "degraded"}}
    assert parse_rules("") == {}
    assert parse_rules("garbage") == {}


def test_a_target_can_declare_its_severity():
    assert parse_rules("a>b=down,c") == {"a": {"b": "down", "c": "degraded"}}


def test_an_unreadable_severity_falls_back_to_degraded():
    """Une faute de frappe dégrade la règle, elle n'empêche pas le démarrage."""
    assert parse_rules("a>b=downn") == {"a": {"b": "degraded"}}


def test_a_named_target_wins_over_the_wildcard(prod_settings):
    graph = ImpactGraph(
        "moddy-bot>*,moddy-dashboard=down",
        prod_settings.known_services,
        monitored=prod_settings.services,
    )
    effective, _ = graph.apply(down(**{"moddy-bot": "down"}))

    assert effective["moddy-dashboard"] == "down"
    assert effective["moddy-website"] == "degraded"


def test_known_services_spans_heartbeats_and_resources(prod_settings):
    assert prod_settings.known_services == [
        "moddy-bot",
        "moddy-api",
        "moddy-altguard",
        "moddy-feeds",
        "moddy-website",
        "moddy-dashboard",
    ]


def test_wildcard_excludes_the_source_itself(graph):
    assert "moddy-bot" not in graph.impacts("moddy-bot")


def test_a_service_without_rule_impacts_nothing(graph):
    assert graph.impacts("moddy-feeds") == []


def test_unknown_targets_are_ignored(prod_settings):
    graph = ImpactGraph("moddy-bot>ghost-service", prod_settings.known_services)
    assert graph.impacts("moddy-bot") == []


def test_impact_can_be_disabled_entirely(prod_settings):
    graph = ImpactGraph("", prod_settings.known_services, monitored=prod_settings.services)
    effective, impacted_by = graph.apply(down(**{"moddy-bot": "down"}))
    assert effective["moddy-api"] == "operational"
    assert impacted_by == {}

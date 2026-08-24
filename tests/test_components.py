"""Rendu Components V2 : le JSON doit correspondre au payload de référence."""

from __future__ import annotations

from app.render import colors
from app.render.components import (
    IS_COMPONENTS_V2,
    TYPE_CONTAINER,
    TYPE_SECTION,
    TYPE_SEPARATOR,
    TYPE_TEXT_DISPLAY,
    build_incident_components,
    build_incident_embed,
)

NAMES = {"moddy-bot": "Moddy Bot", "moddy-api": "API", "moddy-dashboard": "Dashboard"}

INCIDENT = {
    "id": "inc_20260824_1942",
    "title": "Major Outage – Bot & API Unavailable",
    "type": "incident",
    "level": "major_outage",
    "status": "resolved",
    "created_by": "Moddy Health Monitor",
    "affected": ["moddy-bot", "moddy-api", "moddy-dashboard"],
    "url": "https://status.moddy.app/en/incident/995593",
    "created_at": "2026-08-24T19:42:00Z",
    "updates": [
        {"kind": "created", "at": "2026-08-24T19:42:00Z", "message": "Investigating."},
        {"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": "Fix deployed."},
        {"kind": "resolved", "at": "2026-08-24T20:15:00Z", "message": "All good."},
    ],
}


def test_flag_value():
    assert IS_COMPONENTS_V2 == 32768


def test_two_containers():
    header, updates = build_incident_components(INCIDENT, NAMES)
    assert header["type"] == TYPE_CONTAINER
    assert updates["type"] == TYPE_CONTAINER
    assert updates["accent_color"] is None


def test_resolved_incident_is_green_with_check_emoji():
    header, _ = build_incident_components(INCIDENT, NAMES)
    assert header["accent_color"] == colors.ACCENT_RESOLVED
    assert colors.EMOJI_RESOLVED in header["components"][0]["components"][0]["content"]
    assert "**Status:** " + colors.EMOJI_RESOLVED + "Resolved" in header["components"][1]["content"]


def test_ongoing_major_outage_is_red():
    header, _ = build_incident_components({**INCIDENT, "status": "open"}, NAMES)
    assert header["accent_color"] == colors.ACCENT_MAJOR
    assert colors.EMOJI_ONGOING in header["components"][0]["components"][0]["content"]


def test_header_carries_link_button():
    header, _ = build_incident_components(INCIDENT, NAMES)
    section = header["components"][0]
    assert section["type"] == TYPE_SECTION
    assert section["accessory"]["style"] == 5
    assert section["accessory"]["url"] == INCIDENT["url"]


def test_header_without_url_degrades_to_text():
    """Une Section sans accessory est refusée par l'API Discord."""
    header, _ = build_incident_components({**INCIDENT, "url": None}, NAMES)
    assert header["components"][0]["type"] == TYPE_TEXT_DISPLAY


def test_affected_services_use_display_names():
    header, _ = build_incident_components(INCIDENT, NAMES)
    body = header["components"][1]["content"]
    assert "``Moddy Bot``, ``API``, ``Dashboard``" in body


def test_updates_are_separated_and_timestamped():
    _, updates = build_incident_components(INCIDENT, NAMES)
    kinds = [c for c in updates["components"] if c["type"] == TYPE_TEXT_DISPLAY]
    separators = [c for c in updates["components"] if c["type"] == TYPE_SEPARATOR]
    assert kinds[0]["content"] == "### **Updates:**"
    assert len(separators) == 2  # trois updates, deux séparateurs
    assert kinds[1]["content"].startswith("**Created** — <t:")
    assert kinds[-1]["content"].startswith("**Resolved** — <t:")
    assert "\n> All good." in kinds[-1]["content"]


def test_multiline_message_is_fully_quoted():
    incident = {
        **INCIDENT,
        "updates": [{"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": "a\nb"}],
    }
    _, updates = build_incident_components(incident, NAMES)
    assert updates["components"][-1]["content"].endswith("> a\n> b")


def test_no_updates_means_single_container():
    containers = build_incident_components({**INCIDENT, "updates": []}, NAMES)
    assert len(containers) == 1


def test_embed_fallback_keeps_the_essentials():
    embed = build_incident_embed(INCIDENT, NAMES)
    assert embed["title"] == INCIDENT["title"]
    assert embed["url"] == INCIDENT["url"]
    assert embed["color"] == colors.ACCENT_RESOLVED

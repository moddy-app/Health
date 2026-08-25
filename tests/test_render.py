"""Rendu Components V2 : le JSON doit correspondre au payload de référence."""

from __future__ import annotations

from app.render import colors
from app.render.model import IncidentPresentation
from app.render.raw import (
    IS_COMPONENTS_V2,
    TYPE_CONTAINER,
    TYPE_SECTION,
    TYPE_SEPARATOR,
    TYPE_TEXT_DISPLAY,
    build_raw_components,
    build_raw_embed,
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


def render(**over):
    return build_raw_components(IncidentPresentation.from_incident({**INCIDENT, **over}, NAMES))


def test_flag_value():
    assert IS_COMPONENTS_V2 == 32768


def test_two_containers():
    header, updates = render()
    assert header["type"] == TYPE_CONTAINER
    assert updates["type"] == TYPE_CONTAINER
    assert updates["accent_color"] is None


def test_resolved_incident_is_green_with_check_emoji():
    header, _ = render()
    assert header["accent_color"] == colors.ACCENT_RESOLVED
    assert colors.EMOJI_RESOLVED in header["components"][0]["components"][0]["content"]
    assert "**Status:** " + colors.EMOJI_RESOLVED + "Resolved" in header["components"][1]["content"]


def test_ongoing_major_outage_is_red():
    header, _ = render(status="open")
    assert header["accent_color"] == colors.ACCENT_MAJOR
    assert colors.EMOJI_ONGOING in header["components"][0]["components"][0]["content"]


def test_only_the_three_allowed_icons_are_used():
    """Le rendu doit rester sobre : trois icônes, jamais d'émoji décoratif."""
    allowed = {colors.EMOJI_ONGOING, colors.EMOJI_RESOLVED, colors.EMOJI_PENDING}
    from app.render import theme

    used = {style.emoji for style in theme.STATUS_STYLES.values()}
    assert used <= allowed
    assert set(theme.SERVICE_ICONS.values()) <= allowed


def test_maintenance_shows_its_own_status_while_ongoing():
    header, _ = render(status="open", type="maintenance", level="maintenance")
    assert "Maintenance" in header["components"][1]["content"]
    assert header["accent_color"] == colors.ACCENT_MAINTENANCE


def test_header_carries_link_button():
    header, _ = render()
    section = header["components"][0]
    assert section["type"] == TYPE_SECTION
    assert section["accessory"]["style"] == 5
    assert section["accessory"]["url"] == INCIDENT["url"]


def test_header_without_url_degrades_to_text():
    """Une Section sans accessory est refusée par l'API Discord."""
    header, _ = render(url=None)
    assert header["components"][0]["type"] == TYPE_TEXT_DISPLAY


def test_affected_services_use_display_names():
    header, _ = render()
    body = header["components"][1]["content"]
    assert "``Moddy Bot``, ``API``, ``Dashboard``" in body


def test_updates_are_separated_and_timestamped():
    _, updates = render()
    kinds = [c for c in updates["components"] if c["type"] == TYPE_TEXT_DISPLAY]
    separators = [c for c in updates["components"] if c["type"] == TYPE_SEPARATOR]
    assert kinds[0]["content"] == "### **Updates:**"
    assert len(separators) == 2  # trois updates, deux séparateurs
    assert kinds[1]["content"].startswith("**Created** — <t:")
    assert kinds[-1]["content"].startswith("**Resolved** — <t:")
    assert "\n> All good." in kinds[-1]["content"]


def test_multiline_message_is_fully_quoted():
    _, updates = render(
        updates=[{"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": "a\nb"}]
    )
    assert updates["components"][-1]["content"].endswith("> a\n> b")


def test_only_the_last_updates_are_rendered():
    many = [
        {"kind": "updated", "at": "2026-08-24T19:55:00Z", "message": f"m{i}"} for i in range(20)
    ]
    _, updates = render(updates=many)
    assert "-# 5 earlier update(s) not shown." in updates["components"][1]["content"]


def test_no_updates_means_single_container():
    assert len(render(updates=[])) == 1


def test_embed_fallback_keeps_the_essentials():
    embed = build_raw_embed(IncidentPresentation.from_incident(INCIDENT, NAMES))
    assert embed["title"] == INCIDENT["title"]
    assert embed["url"] == INCIDENT["url"]
    assert embed["color"] == colors.ACCENT_RESOLVED

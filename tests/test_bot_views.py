"""Vues du salon de statut et modals V2 : structure, sobriété, contraintes API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot import modals
from app.bot.views import DETAILS_ID, DETAILS_REFRESH_ID, StickyStatusView, build_detail_view
from app.config import Settings
from app.render import colors
from app.render.model import StatusPresentation

PUBLIC = {
    "status": "degraded",
    "updated_at": "2026-08-24T19:42:00Z",
    "services": [
        {"id": "moddy-bot", "name": "Moddy Bot", "status": "operational"},
        {"id": "moddy-api", "name": "API", "status": "degraded", "impacted_by": ["moddy-bot"]},
    ],
    "incident": {"title": "Partial Outage", "url": "https://status.moddy.app/en/incident/1"},
}


def flatten(components) -> str:
    """Tout le texte d'un payload de composants, containers compris."""
    out = []
    for node in components:
        if node.get("content"):
            out.append(node["content"])
        out.extend([flatten(node.get("components") or [])])
    return "\n".join(part for part in out if part)


def test_the_sticky_is_a_single_components_v2_container():
    view = StickyStatusView(StatusPresentation.from_public(PUBLIC), "https://status.moddy.app")
    payload = view.to_components()
    assert len(payload) == 1
    assert payload[0]["type"] == 17


def test_the_sticky_lists_every_service_with_its_icon():
    view = StickyStatusView(StatusPresentation.from_public(PUBLIC), "https://status.moddy.app")
    text = flatten(view.to_components())
    # Les noms sont alignés sur la plus longue colonne, d'où le remplissage.
    assert "``Moddy Bot``" in text and "``API      ``" in text
    assert colors.EMOJI_OPERATIONAL in text and colors.EMOJI_DEGRADED in text
    assert "Last updated <t:" in text


def test_the_sticky_headline_follows_the_global_level():
    view = StickyStatusView(StatusPresentation.from_public(PUBLIC))
    text = flatten(view.to_components())
    assert text.startswith(f"### {colors.EMOJI_DEGRADED} Degraded Performance")

    healthy = StatusPresentation.from_public({**PUBLIC, "status": "operational", "incident": None})
    assert colors.EMOJI_OPERATIONAL in flatten(StickyStatusView(healthy).to_components())


def test_the_details_button_survives_a_redeploy():
    """Un `custom_id` fixe et `timeout=None` : sinon le bouton meurt au redéploiement."""
    view = StickyStatusView()
    assert view.timeout is None
    row = view.to_components()[0]["components"][-1]
    assert row["components"][0]["custom_id"] == DETAILS_ID
    assert row["components"][0]["label"] == "Details"


def test_the_detail_panel_is_persistent_too():
    """L'éphémère reste affiché après un redéploiement : son bouton doit vivre."""
    view = build_detail_view(StatusPresentation.from_public(PUBLIC), {})
    assert view.timeout is None
    row = view.to_components()[0]["components"][-1]
    assert row["components"][0]["custom_id"] == DETAILS_REFRESH_ID


def test_the_status_page_button_is_dropped_when_there_is_no_url():
    """Un bouton lien sans URL lève à l'envoi."""
    row = StickyStatusView(StatusPresentation.from_public(PUBLIC)).to_components()[0]["components"][-1]
    assert len(row["components"]) == 1


def test_the_detail_view_shows_heartbeat_diagnostics():
    snapshot = StatusPresentation.from_public(PUBLIC)
    heartbeats = {
        "moddy-bot": {
            "version": "1.4.2",
            "uptime_s": 7300,
            "received_at": "2026-08-24T19:41:50Z",
            "checks": {"discord": "ok", "db": "ok"},
        }
    }
    text = flatten(build_detail_view(snapshot, heartbeats).to_components())
    assert "`1.4.2`" in text
    assert "up 2h01" in text
    assert "no heartbeat" in text  # moddy-api n'en a pas
    assert "impacted by moddy-bot" in text


def test_the_detail_panel_summarises_healthy_checks():
    """Le dump brut des dictionnaires était illisible : on compte, sans détailler."""
    snapshot = StatusPresentation.from_public(PUBLIC)
    heartbeats = {"moddy-bot": {"checks": {"redis": {"ok": True}, "db": {"ok": True}}}}
    text = flatten(build_detail_view(snapshot, heartbeats).to_components())
    assert "2 checks passing" in text
    assert "{'ok': True}" not in text


def test_the_detail_panel_names_only_the_failing_checks():
    snapshot = StatusPresentation.from_public(PUBLIC)
    heartbeats = {
        "moddy-bot": {
            "checks": {"redis": {"ok": True}, "discord_gateway": {"ok": False, "latency_ms": 900}}
        }
    }
    text = flatten(build_detail_view(snapshot, heartbeats).to_components())
    assert "discord gateway" in text
    assert "1/2 passing" in text


@pytest.fixture
def ctx():
    return SimpleNamespace(settings=Settings(redis_url=""), incidents=None)


def test_a_modal_never_exceeds_five_top_level_components(ctx):
    for modal in (
        modals.IncidentCreateModal(ctx),
        modals.IncidentUpdateModal(ctx),
        modals.IncidentResolveModal(ctx),
        modals.MaintenanceModal(ctx),
    ):
        assert len(modal.children) <= 5


def test_the_affected_services_come_from_the_configuration():
    """Ajouter un service reste une affaire de variables d'environnement."""
    settings = Settings(redis_url="", hm_services="moddy-bot,acme-thing")
    ctx = SimpleNamespace(settings=settings, incidents=None)
    values = [option.value for option in modals.IncidentCreateModal(ctx).affected.component.options]
    assert "acme-thing" in values


def test_the_affected_group_never_allows_more_choices_than_it_offers(ctx):
    """Discord refuse le modal entier si `max_values` dépasse le nombre d'options."""
    for modal in (modals.IncidentCreateModal(ctx), modals.MaintenanceModal(ctx)):
        group = modal.affected.component
        assert 1 <= group.max_values <= len(group.options)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-25 02:00 -> 04:00", ("2026-08-25T02:00:00Z", "2026-08-25T04:00:00Z")),
        ("2026-08-25 23:00 -> 2026-08-26 01:00", ("2026-08-25T23:00:00Z", "2026-08-26T01:00:00Z")),
        ("2026-08-25 02:00", (None, None)),
        ("nonsense", (None, None)),
    ],
)
def test_the_maintenance_window_is_parsed_from_a_single_field(raw, expected):
    """Deux champs séparés feraient six composants : au-dessus de la limite."""
    assert modals.parse_window(raw) == expected


def test_every_modal_can_read_its_own_components(ctx):
    """Les composants ne rendent pas tous la même chose : `RadioGroup` a `value`,
    `CheckboxGroup` a `values`. Se tromper de nom fait tomber le modal au submit,
    après que le staff a tout tapé."""
    interaction = SimpleNamespace(user=SimpleNamespace(display_name="Jules"))
    payload = modals.IncidentCreateModal(ctx).build_payload(interaction)
    assert payload["level"] == colors.PARTIAL_OUTAGE  # rien de coché : le défaut
    assert payload["affected"] == [] and payload["notify"] is False
    assert payload["author"] == "Jules"

    for modal in (modals.IncidentUpdateModal(ctx), modals.IncidentResolveModal(ctx)):
        assert modal.build_payload(interaction)["author"] == "Jules"
    # Fenêtre vide : le modal refuse au lieu de lever.
    assert modals.MaintenanceModal(ctx).build_payload(interaction) is None


def test_the_buttons_carry_their_colour_and_icon():
    row = StickyStatusView().to_components()[0]["components"][-1]
    details = row["components"][0]
    assert details["style"] == 1  # primary, bleu
    assert details["emoji"]["name"] == "info"

    row = build_detail_view(StatusPresentation.from_public(PUBLIC), {}).to_components()[0][
        "components"
    ][-1]
    refresh = row["components"][0]
    assert refresh["style"] == 3  # success, vert
    assert refresh["emoji"]["name"] == "refresh"


def test_a_service_not_yet_revealed_shows_nothing_but_a_spinner():
    snapshot = StatusPresentation.from_public(PUBLIC)
    heartbeats = {"moddy-bot": {"version": "1.4.2", "received_at": "2026-08-24T19:41:50Z"}}
    text = flatten(build_detail_view(snapshot, heartbeats, revealed=set()).to_components())
    assert colors.EMOJI_LOADING in text
    assert "1.4.2" not in text
    assert colors.EMOJI_OPERATIONAL not in text
    # L'en-tête ne conclut rien tant que rien n'est révélé.
    assert "All Systems Operational" not in text and "Degraded Performance" not in text


def test_the_last_reveal_restores_the_full_panel():
    snapshot = StatusPresentation.from_public(PUBLIC)
    every = {service.id for service in snapshot.services}
    view = build_detail_view(snapshot, {}, revealed=every)
    text = flatten(view.to_components())
    assert colors.EMOJI_LOADING not in text
    assert "Degraded Performance" in text
    assert view.to_components()[0]["accent_color"] == snapshot.accent


def test_the_heartbeat_age_is_a_discord_timestamp():
    """Un âge calculé devient faux dès que le panneau reste affiché."""
    snapshot = StatusPresentation.from_public(PUBLIC)
    heartbeats = {"moddy-bot": {"received_at": "2026-08-24T19:41:50Z"}}
    text = flatten(build_detail_view(snapshot, heartbeats).to_components())
    assert "heartbeat <t:1787600510:R>" in text

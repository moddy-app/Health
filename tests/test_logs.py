"""Journalisation structurée : Railway ne lit que quatre niveaux, et une ligne."""

from __future__ import annotations

import json
import logging

import pytest

from app.logs import RailwayFormatter, configure


def record(level: int, message: str = "redis perdu", **extra) -> logging.LogRecord:
    rec = logging.LogRecord("hm.state", level, "state.py", 42, message, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def emitted(level: int, **extra) -> dict:
    return json.loads(RailwayFormatter().format(record(level, **extra)))


@pytest.mark.parametrize(
    ("python_level", "railway_level"),
    [
        (logging.DEBUG, "debug"),
        (logging.INFO, "info"),
        (logging.WARNING, "warn"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "error"),
    ],
)
def test_python_levels_are_translated_for_railway(python_level, railway_level):
    """`WARNING` n'existe pas côté Railway : non traduit, tout ressort en info."""
    assert emitted(python_level)["level"] == railway_level


def test_a_log_line_is_a_single_json_object():
    """Le multi-ligne casse le parsing : un objet, une ligne."""
    line = RailwayFormatter().format(record(logging.INFO, "moddy-bot: unknown -> down"))
    assert "\n" not in line
    assert json.loads(line)["message"] == "moddy-bot: unknown -> down"


def test_a_traceback_stays_attached_to_its_message():
    """C'est tout l'intérêt du format structuré : la trace ne part pas en miettes."""
    try:
        raise RuntimeError("discord is down")
    except RuntimeError:
        import sys

        rec = record(logging.ERROR, "publication en échec")
        rec.exc_info = sys.exc_info()
        payload = json.loads(RailwayFormatter().format(rec))

    assert payload["message"].startswith("publication en échec\n")
    assert "RuntimeError: discord is down" in payload["message"]


def test_the_logger_name_survives():
    assert emitted(logging.WARNING)["logger"] == "hm.state"


def test_extra_fields_are_carried_through():
    """Railway indexe les attributs : un `extra=` doit rester filtrable."""
    payload = emitted(logging.INFO, service="moddy-bot", incident_id="inc_1")
    assert payload["service"] == "moddy-bot"
    assert payload["incident_id"] == "inc_1"


def test_an_unserializable_extra_never_breaks_a_log_line():
    payload = emitted(logging.INFO, store=object())
    assert isinstance(payload["store"], str)


def test_configure_never_stacks_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure("INFO", "json")
        configure("DEBUG", "json")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, RailwayFormatter)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def test_text_format_stays_readable_for_local_work():
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure("INFO", "text")
        assert not isinstance(root.handlers[0].formatter, RailwayFormatter)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)


def test_the_ansi_variant_of_uvicorn_is_dropped():
    """`color_message` porte des codes d'échappement, illisibles dans l'explorateur."""
    payload = emitted(logging.INFO, color_message="Started process [\x1b[36m%d\x1b[0m]")
    assert "color_message" not in payload


def test_accents_are_not_escaped():
    line = RailwayFormatter().format(record(logging.INFO, "détecteur prêt"))
    assert "détecteur prêt" in line


def test_the_timestamp_is_iso_8601_utc():
    assert emitted(logging.INFO)["time"].endswith("+00:00")

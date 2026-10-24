"""Lecture des liens passés à `/status resend`."""

from __future__ import annotations

import pytest

from app.bot.commands import parse_message_link, parse_report_id


@pytest.mark.parametrize(
    "text",
    [
        "https://status.moddy.app/incident/1099029",
        "https://status.moddy.app/en/incident/1099029",
        "https://status.moddy.app/incident/1099029?foo=bar",
        "https://uptime.betterstack.com/team/1/status-pages/237745/status-reports/1099029",
        " 1099029 ",
    ],
)
def test_a_report_is_recognised_from_any_of_its_links(text):
    assert parse_report_id(text) == "1099029"


def test_a_link_that_is_not_an_incident_is_refused():
    assert parse_report_id("https://status.moddy.app/") is None
    assert parse_report_id("not a link") is None


@pytest.mark.parametrize(
    "text",
    [
        "https://discord.com/channels/1394001780148535387/1398625686301704323/1430000000000000000",
        "https://ptb.discord.com/channels/1394001780148535387/1398625686301704323/1430000000000000000",
        "https://discordapp.com/channels/1394001780148535387/1398625686301704323/1430000000000000000",
    ],
)
def test_a_message_link_gives_its_channel_and_message(text):
    assert parse_message_link(text) == ("1398625686301704323", "1430000000000000000")


def test_a_channel_link_is_not_a_message_link():
    assert parse_message_link("https://discord.com/channels/1394001780148535387/1398625686301704323") is None

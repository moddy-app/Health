"""Les deux renderers partent du même modèle et doivent produire la même chose.

Le bot passe par discord.py, le webhook par du JSON écrit à la main. Sans ce
test, les deux chemins divergent au premier changement de mise en forme — et
personne ne s'en aperçoit avant la panne où le webhook prend le relais.
"""

from __future__ import annotations

import pytest

from app.render.layout import build_layout_view
from app.render.model import IncidentPresentation
from app.render.raw import build_raw_components

from test_render import INCIDENT, NAMES

# Clés que discord.py ajoute toujours et que le JSON brut omet : Discord leur
# applique la même valeur par défaut, elles ne changent rien au rendu.
_IGNORED = {"spoiler", "disabled", "id"}


def normalize(node):
    if isinstance(node, dict):
        return {k: normalize(v) for k, v in node.items() if k not in _IGNORED}
    if isinstance(node, list):
        return [normalize(item) for item in node]
    return node


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"status": "open"},
        {"url": None},
        {"updates": []},
        {"type": "maintenance", "level": "maintenance", "status": "open"},
        {"affected": []},
    ],
    ids=["resolved", "ongoing", "no-url", "no-updates", "maintenance", "no-service"],
)
def test_both_renderers_agree(over):
    presentation = IncidentPresentation.from_incident({**INCIDENT, **over}, NAMES)
    assert normalize(build_layout_view(presentation).to_components()) == normalize(
        build_raw_components(presentation)
    )

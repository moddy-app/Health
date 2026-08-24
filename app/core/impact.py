"""Propagation d'impact entre services.

Un service qui tombe n'affecte pas que lui-même. Le bot est le produit : s'il
est down, tout l'écosystème est dégradé du point de vue utilisateur. L'API est
le socle : si elle tombe, le site, le dashboard et le bot fonctionnent mal.
Le dashboard, lui, ne fait tomber personne.

Deux règles fixent le comportement, et elles évitent les cascades :

  - **Seul un service `down` propage.** Un service `degraded` ne dégrade
    personne, sinon un hoquet se répandrait sur toute la page.
  - **La propagation ne produit que du `degraded`.** Elle n'invente jamais un
    `down`, et n'écrase jamais un `down` observé. Comme l'état dérivé plafonne
    à `degraded` et que seul `down` propage, il n'y a pas de chaîne possible :
    un seul saut, par construction.

L'état propagé est un état d'**expérience utilisateur**, pas un état de santé
technique : `/v1/status` expose les deux (`status` et `reported`).
"""

from __future__ import annotations

import logging

log = logging.getLogger("hm.impact")

DOWN = "down"
DEGRADED = "degraded"
UNKNOWN = "unknown"

# Joker : « ce service impacte tous les autres ».
WILDCARD = "*"


def parse_rules(raw: str) -> dict[str, list[str]]:
    """`a>b,c;d>*` -> {"a": ["b", "c"], "d": ["*"]}."""
    rules: dict[str, list[str]] = {}
    for entry in raw.replace("\n", ";").split(";"):
        source, sep, targets = entry.partition(">")
        source = source.strip()
        if not source or not sep:
            continue
        parsed = [t.strip() for t in targets.split(",") if t.strip()]
        if parsed:
            rules.setdefault(source, []).extend(parsed)
    return rules


class ImpactGraph:
    def __init__(
        self, raw: str, known: list[str], monitored: list[str] | None = None
    ) -> None:
        self.known = list(known)
        self._monitored = set(monitored if monitored is not None else known)
        self._rules = parse_rules(raw)

        unknown_sources = set(self._rules) - set(self.known)
        if unknown_sources:
            log.warning("HM_IMPACT_MAP : source(s) inconnue(s) %s", sorted(unknown_sources))

    def impacts(self, service: str) -> list[str]:
        """Services dégradés quand `service` tombe."""
        targets = self._rules.get(service)
        if not targets:
            return []
        if WILDCARD in targets:
            return [s for s in self.known if s != service]
        return [t for t in targets if t != service and t in self.known]

    def apply(self, observed: dict[str, str]) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Applique la propagation.

        `observed` ne contient que les services qui poussent un heartbeat ; les
        autres services connus (ressources Better Stack sans heartbeat, comme le
        site ou le dashboard) partent d'`unknown` et ne sortent de cet état que
        s'ils sont impactés.
        """
        effective = {service: observed.get(service, UNKNOWN) for service in self.known}
        impacted_by: dict[str, list[str]] = {}

        for source in self.known:
            if observed.get(source) != DOWN:
                continue
            for target in self.impacts(source):
                current = effective.get(target, UNKNOWN)
                if current == DOWN:
                    continue  # déjà pire que dégradé
                if current == UNKNOWN and target in self._monitored:
                    # Aucune donnée sur un service qu'on surveille pourtant :
                    # le déclarer dégradé serait une affirmation infondée.
                    continue
                effective[target] = DEGRADED
                impacted_by.setdefault(target, []).append(source)

        return effective, impacted_by

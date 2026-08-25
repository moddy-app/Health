"""Propagation d'impact entre services.

Un service qui tombe n'affecte pas que lui-même. Le bot est le produit : s'il
est down, tout l'écosystème est dégradé du point de vue utilisateur. L'API est
le socle : si elle tombe, le site, le dashboard et le bot fonctionnent mal.
Le dashboard, lui, ne fait tomber personne.

Trois règles fixent le comportement, et elles évitent les cascades :

  - **Seul un service `down` propage.** Un service `degraded` ne dégrade
    personne, sinon un hoquet se répandrait sur toute la page.
  - **La propagation produit `degraded` par défaut, `down` si la règle le dit.**
    Un dashboard privé de son backend n'affiche plus rien : le dégrader serait
    mentir à l'utilisateur. La sévérité se déclare par cible, avec `=down`.
  - **Un `down` observé n'est jamais écrasé.** L'état qu'un service constate sur
    lui-même prime toujours sur ce qu'on déduit de ses dépendances.

La propagation part des états **observés** et n'en relit jamais le résultat : un
seul saut, par construction, même quand une règle produit un `down`.

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


def parse_rules(raw: str) -> dict[str, dict[str, str]]:
    """`a>b,c=down;d>*` -> {"a": {"b": "degraded", "c": "down"}, "d": {"*": "degraded"}}.

    Une cible sans suffixe vaut `degraded` ; une sévérité inconnue aussi, plutôt
    que d'empêcher le démarrage sur une faute de frappe.
    """
    rules: dict[str, dict[str, str]] = {}
    for entry in raw.replace("\n", ";").split(";"):
        source, sep, targets = entry.partition(">")
        source = source.strip()
        if not source or not sep:
            continue
        for item in targets.split(","):
            target, _, severity = item.partition("=")
            target, severity = target.strip(), severity.strip().lower()
            if not target:
                continue
            if severity and severity != DOWN:
                log.warning("HM_IMPACT_MAP : sévérité inconnue %r sur %s", severity, target)
            rules.setdefault(source, {})[target] = DOWN if severity == DOWN else DEGRADED
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

    def effects(self, service: str) -> dict[str, str]:
        """`{cible: sévérité}` quand `service` tombe."""
        targets = self._rules.get(service)
        if not targets:
            return {}
        if WILDCARD in targets:
            severity = targets[WILDCARD]
            expanded = {s: severity for s in self.known if s != service}
            # Une cible nommée explicitement l'emporte sur le joker.
            expanded.update(
                {t: sev for t, sev in targets.items() if t != WILDCARD and t in self.known}
            )
            return expanded
        return {t: sev for t, sev in targets.items() if t != service and t in self.known}

    def impacts(self, service: str) -> list[str]:
        """Services affectés quand `service` tombe."""
        return list(self.effects(service))

    def apply(self, observed: dict[str, str]) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Applique la propagation.

        `observed` ne contient que les services qui poussent un heartbeat ou que
        le monitor sonde ; les autres services connus (ressources Better Stack
        seules) partent d'`unknown` et ne sortent de cet état que s'ils sont
        impactés.
        """
        effective = {service: observed.get(service, UNKNOWN) for service in self.known}
        impacted_by: dict[str, list[str]] = {}
        # Les états dérivés sont tenus à part : la propagation lit `observed` et
        # jamais son propre résultat, sinon un `down` dérivé propagerait à son
        # tour.
        derived: dict[str, str] = {}

        for source in self.known:
            if observed.get(source) != DOWN:
                continue
            for target, severity in self.effects(source).items():
                current = observed.get(target, UNKNOWN)
                if current == DOWN:
                    continue  # cause propre : elle prime sur ce qu'on déduit
                if current == UNKNOWN and target in self._monitored:
                    # Aucune donnée sur un service qu'on surveille pourtant :
                    # le déclarer affecté serait une affirmation infondée.
                    continue
                if severity == DOWN or derived.get(target) != DOWN:
                    derived[target] = severity
                impacted_by.setdefault(target, []).append(source)

        effective.update(derived)
        return effective, impacted_by

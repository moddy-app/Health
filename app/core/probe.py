"""Check HTTP actif pour les services sans process.

Un dashboard est un site statique : rien n'y tourne qui puisse pousser un
heartbeat toutes les 20s. Le modèle push ne s'y applique pas — c'est structurel.
Le monitor va donc chercher une URL publique et traduit la réponse en heartbeat
synthétique, écrit dans la même clé que l'ingestion : le détecteur ne sait pas —
et n'a pas à savoir — d'où vient l'information.

Ça n'écorne pas l'invariant « le monitor ne dépend de rien de ce qu'il
surveille » : on interroge une URL publique comme le ferait n'importe quel
navigateur, jamais une API interne, et l'échec de la sonde ne fait rien d'autre
qu'écrire un heartbeat `down`.

Le contrat de l'endpoint sondé tient en une ligne : **un 2xx signifie vivant**.
Le corps de la réponse n'est jamais lu.
"""

from __future__ import annotations

import logging
import time

import httpx

from .. import keys
from ..config import Settings
from ..state import Store
from ..util import iso

log = logging.getLogger("hm.probe")


class Probe:
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient) -> None:
        self._s = settings
        self._store = store
        self._http = client

    @property
    def targets(self) -> dict[str, str]:
        return self._s.probe_map

    async def run_once(self) -> None:
        """Sonde toutes les cibles. Ne lève jamais : une cible morte n'en bloque pas une autre."""
        for service, url in self.targets.items():
            try:
                await self.check(service, url)
            except Exception:
                log.exception("sonde %s : échec inattendu", service)

    async def check(self, service: str, url: str) -> str:
        """Sonde une URL et écrit le heartbeat correspondant. Renvoie l'état écrit."""
        started = time.monotonic()
        code: int | None = None
        error: str | None = None

        try:
            response = await self._http.get(url, timeout=self._s.hm_probe_timeout)
            code = response.status_code
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

        latency_ms = int((time.monotonic() - started) * 1000)
        ok = code is not None and 200 <= code < 300
        status = "ok" if ok else "down"

        if not ok:
            log.warning("sonde %s (%s) : %s", service, url, error or f"HTTP {code}")

        record = {
            "service": service,
            "status": status,
            "version": None,
            "uptime_s": None,
            # Même forme qu'un heartbeat poussé : `checks` reste un dictionnaire
            # à clés libres, que le monitor n'interprète pas.
            "checks": {
                "http": {
                    "ok": ok,
                    "status_code": code,
                    "latency_ms": latency_ms,
                    "error": error,
                }
            },
            # Trace l'origine : le dashboard ne s'est pas déclaré vivant, on l'a
            # constaté. Utile en lisant `/v1/status` ou un message Discord.
            "meta": {"source": "probe", "url": url},
            "received_at": iso(),
        }
        await self._store.set_json(keys.hb(service), record, ttl=self._s.probe_ttl)
        return status

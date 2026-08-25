"""Journalisation : texte en local, JSON structuré sur Railway.

Railway lit les logs émis en JSON sur une seule ligne et en tire deux champs :
`message`, affiché dans l'explorateur, et `level`, qui colore la ligne et sert
au filtrage. Il n'accepte que quatre niveaux — `debug`, `info`, `warn`, `error` —
là où Python en a cinq et les nomme autrement.

Sans cette traduction, tout ressort en `info` : un `WARNING` de perte de Redis
se noie dans le flux, et c'est précisément la ligne qu'on cherche pendant une
panne.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Python -> Railway. `CRITICAL` retombe sur `error` : Railway n'a rien au-dessus.
RAILWAY_LEVELS: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

# Attributs posés par `logging` sur chaque record : tout le reste vient d'un
# `extra=` de l'appelant et mérite d'être remonté tel quel.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info taskName thread threadName
    color_message""".split()
)
# `color_message` est la variante ANSI que pose uvicorn : des codes d'échappement
# dans un champ JSON, illisibles dans l'explorateur Railway.


class RailwayFormatter(logging.Formatter):
    """Un objet JSON par ligne — le multi-ligne casserait le parsing."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "time": _iso(record.created),
            "level": RAILWAY_LEVELS.get(record.levelno, "info"),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # La trace tient dans le champ `message` : c'est justement ce que le
        # format structuré permet de préserver, là où le texte brut la
        # découperait en une ligne de log par ligne de trace.
        if record.exc_info:
            payload["message"] += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            payload["message"] += "\n" + self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _jsonable(value)

        # `ensure_ascii=False` : les logs sont en français, inutile d'échapper
        # chaque accent en `\u00e9`.
        return json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)


def _iso(created: float) -> str:
    """ISO-8601 UTC, comme partout ailleurs dans le monitor."""
    return datetime.fromtimestamp(created, tz=timezone.utc).isoformat(timespec="milliseconds")


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def on_railway() -> bool:
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_SERVICE_ID"))


def configure(level: str = "INFO", fmt: str = "auto") -> None:
    """Pose l'unique handler racine. Idempotent : rappeler ne duplique rien."""
    if fmt == "auto":
        fmt = "json" if on_railway() else "text"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RailwayFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

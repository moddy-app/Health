"""Entrées authentifiées : heartbeats des services, commandes du bot."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import keys
from ..context import Context, get_ctx
from ..util import iso

log = logging.getLogger("hm.ingest")

router = APIRouter(prefix="/ingest", tags=["ingest"])


def require_token(
    request: Request, x_health_token: str | None = Header(default=None, alias="X-Health-Token")
) -> Context:
    ctx = get_ctx(request)
    expected = ctx.settings.hm_ingest_token
    if not expected:
        # Refuser plutôt qu'accepter en anonyme : un token vide est une erreur
        # de déploiement, pas un mode ouvert.
        log.error("HM_INGEST_TOKEN non configuré, ingestion refusée")
        raise HTTPException(status_code=503, detail="ingest token not configured")
    if not x_health_token or not secrets.compare_digest(x_health_token, expected):
        raise HTTPException(status_code=401, detail="invalid token")
    return ctx


class Heartbeat(BaseModel):
    service: str
    # Le service décide lui-même de son état : il connaît ses dépendances mieux
    # que le monitor.
    status: str = "ok"
    version: str | None = None
    uptime_s: int | None = None
    # Dictionnaire à clés libres : le monitor n'interprète jamais les noms de
    # clés, il itère dessus pour l'affichage.
    checks: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class Command(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/heartbeat")
async def heartbeat(body: Heartbeat, ctx: Context = Depends(require_token)) -> dict:
    if body.service not in ctx.settings.services:
        log.warning("heartbeat d'un service non déclaré dans HM_SERVICES: %s", body.service)

    received_at = iso()
    record = body.model_dump()
    record["received_at"] = received_at
    # TTL = interval x 3 (20s d'intervalle recommandé -> 60s).
    await ctx.store.set_json(
        keys.hb(body.service), record, ttl=ctx.settings.hm_heartbeat_ttl
    )

    incident = await ctx.incidents.get_active()
    return {
        "ok": True,
        "received_at": received_at,
        # Permet au service de dégrader son propre comportement pendant une crise.
        "incident_active": bool(incident and incident.get("status") != "resolved"),
    }


@router.post("/command")
async def command(
    body: Command, background: BackgroundTasks, ctx: Context = Depends(require_token)
) -> dict:
    """Repli du bot quand Redis est down : même contrat, autre transport."""
    background.add_task(ctx.incidents.handle_command, body.action, body.payload)
    return {"ok": True, "action": body.action}

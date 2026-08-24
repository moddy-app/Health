"""Webhook entrant Better Stack.

Better Stack ne signe pas ses webhooks et ne supporte aucun header d'auth : la
doc recommande explicitement l'authentification par URL, d'où le `?k=`.
On répond 2xx immédiatement (timeout 30s côté Better Stack) et on traite en
tâche de fond.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response

from ..context import get_ctx

log = logging.getLogger("hm.webhooks")

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/betterstack")
async def betterstack(
    request: Request,
    background: BackgroundTasks,
    k: str | None = None,
    x_betteruptime_event: str | None = Header(default=None, alias="X-BetterUptime-Event"),
) -> Response:
    ctx = get_ctx(request)
    secret = ctx.settings.betterstack_webhook_secret
    if secret and not (k and secrets.compare_digest(k, secret)):
        raise HTTPException(status_code=403, detail="invalid webhook key")

    try:
        payload = await request.json()
    except Exception:
        log.warning("webhook Better Stack au corps illisible")
        return Response(status_code=204)

    if not isinstance(payload, dict):
        return Response(status_code=204)

    payload.setdefault("event_type", x_betteruptime_event or "incident")
    background.add_task(ctx.incidents.handle_bs_payload, payload)
    return Response(status_code=202)

"""Endpoint public, sans authentification.

Le calcul se fait dans la boucle de check, pas à la requête : ces routes ne
font que servir une clé Redis, elles tiennent la charge et restent disponibles
même si tout le reste rame.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import keys
from ..context import Context, get_ctx

router = APIRouter(prefix="/v1", tags=["public"])


async def _payload(ctx: Context) -> dict:
    data = await ctx.store.get_json(keys.STATUS_PUBLIC)
    if isinstance(data, dict):
        return data
    # Secours : au tout premier démarrage la boucle de check n'a pas encore
    # tourné, ou le cache a expiré pendant un cycle lent.
    snapshot = ctx.detector.current_snapshot()
    incident = await ctx.incidents.get_active()
    data = ctx.detector.public_payload(snapshot, incident)
    await ctx.store.set_json(keys.STATUS_PUBLIC, data, ttl=ctx.settings.hm_public_cache_ttl)
    return data


@router.get("/status")
async def status(response: Response, ctx: Context = Depends(get_ctx)) -> dict:
    data = await _payload(ctx)
    response.headers["Cache-Control"] = f"public, max-age={ctx.settings.hm_public_cache_ttl}"
    return data


@router.get("/status/banner")
async def banner(response: Response, ctx: Context = Depends(get_ctx)) -> dict:
    """Payload minimal pour la bannière du dashboard."""
    data = await _payload(ctx)
    current = data.get("incident") or data.get("maintenance")
    response.headers["Cache-Control"] = f"public, max-age={ctx.settings.hm_public_cache_ttl}"
    if not current:
        return {"level": data.get("status", "operational"), "title": None, "url": None}
    return {
        "level": current.get("level"),
        "title": current.get("title"),
        "url": current.get("url"),
    }

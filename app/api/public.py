"""Endpoint public, sans authentification.

Le calcul se fait dans la boucle de check, pas à la requête : ces routes ne
font que servir une clé Redis, elles tiennent la charge et restent disponibles
même si tout le reste rame.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from .. import keys
from ..config import Settings
from ..context import Context, get_ctx
from ..render import colors

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
async def banner(
    response: Response, service: str | None = None, ctx: Context = Depends(get_ctx)
) -> dict:
    """Payload minimal pour la bannière du dashboard.

    `?service=<id>` identifie l'appelant. Concerné par l'incident, il est nommé
    dans `message` ; sinon le message reste générique — un consommateur n'a pas
    à savoir *quel* service interne est en cause s'il n'est pas le sien.
    """
    data = await _payload(ctx)
    current = data.get("incident") or data.get("maintenance")
    response.headers["Cache-Control"] = f"public, max-age={ctx.settings.hm_public_cache_ttl}"
    if not current:
        return {"level": data.get("status", "operational"), "title": None, "url": None, "message": None}
    return {
        "level": current.get("level"),
        "title": current.get("title"),
        "url": current.get("url"),
        "message": _banner_message(current, service, ctx.settings),
    }


def _banner_message(current: dict, service: str | None, settings: Settings) -> str:
    """Markdown prêt à afficher : en gras, avec le lien de l'incident.

    Générique par défaut. Le sujet ne se nomme que si l'appelant a désigné son
    propre service *et* qu'il figure dans les services affectés — le nommer
    sinon révélerait une panne qui ne le concerne pas.
    """
    affected = current.get("affected") or []
    tailored = bool(service) and service in affected
    subject = settings.display_name(service) if tailored else "Some Moddy services"
    verb = "is" if tailored else "are"

    if current.get("type") == "maintenance":
        text = f"**{subject}** {verb} undergoing scheduled maintenance."
    elif current.get("level") == colors.DEGRADED:
        text = f"**{subject}** {verb} experiencing degraded performance."
    else:
        text = f"**{subject}** {verb} currently unavailable."

    url = current.get("url")
    if url:
        text += f" [View status]({url})"
    return text

"""Liveness du monitor lui-même : 200 nu, sans aucune dépendance."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_class=PlainTextResponse, include_in_schema=False)
async def health() -> str:
    # Volontairement sans Redis, sans Better Stack, sans Discord : cet endpoint
    # répond même quand tout le reste est cassé.
    return "ok"

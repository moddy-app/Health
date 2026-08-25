"""Point d'entrée FastAPI + lancement des boucles asyncio."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__, keys
from .api import health, ingest, public, webhooks
from .config import get_settings
from .context import build_context, shutdown, startup

log = logging.getLogger("hm")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = app.state.ctx
    await startup(ctx)
    log.info("moddy-health-monitor %s démarré", __version__)
    try:
        yield
    finally:
        # Uvicorn appelle ce bloc sur SIGTERM : on flush l'état avant de sortir.
        log.info("arrêt demandé, flush de l'état")
        await shutdown(ctx)


def create_app() -> FastAPI:
    settings = get_settings()
    _configure_logging(settings.hm_log_level)

    app = FastAPI(
        title="Moddy Health Monitor",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = build_context(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    limit, window = settings.rate_limit

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """60 req/min par IP sur l'API publique (§10)."""
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)

        forwarded = request.headers.get("x-forwarded-for", "")
        client_ip = forwarded.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        hits = await app.state.ctx.store.incr_window(
            keys.PUBLIC_RATELIMIT.format(ip=client_ip), window
        )
        if hits > limit:
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(window)},
            )
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(webhooks.router)
    app.include_router(public.router)
    return app


app = create_app()


async def serve() -> None:  # pragma: no cover - chemin de production
    """FastAPI et le bot dans la même event loop, côte à côte.

    `uvicorn.Server.serve()` et non `uvicorn.run()` : ce dernier crée sa propre
    event loop et le bot n'aurait plus où tourner. Le `async with bot` de
    `bot.run` garantit la fermeture propre de la session HTTP de discord.py sur
    SIGTERM — une gateway mal fermée laisse un shard fantôme quelques minutes,
    et Railway redéploie souvent.
    """
    import asyncio

    import uvicorn

    from .bot.client import run as run_bot

    settings = get_settings()
    config = uvicorn.Config(
        app, host="0.0.0.0", port=settings.port, log_config=None, lifespan="on"
    )
    server = uvicorn.Server(config)

    bot = app.state.ctx.bot
    if bot is None:
        await server.serve()
        return
    await asyncio.gather(server.serve(), run_bot(bot, settings.discord_token))


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(serve())

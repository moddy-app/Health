"""Câblage des composants — un seul objet passé à l'API et aux boucles."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from fastapi import Request

from .config import Settings, get_settings
from .core.detector import Detector
from .core.incident import IncidentManager
from .core.notifier import Notifier
from .core.probe import Probe
from .core.scheduler import Scheduler
from .integrations.betterstack import BetterStack
from .integrations.discord_webhook import DiscordWebhook
from .integrations.redis_bus import RedisBus
from .state import Store

log = logging.getLogger("hm.context")


@dataclass
class Context:
    settings: Settings
    store: Store
    http: httpx.AsyncClient
    betterstack: BetterStack
    webhook: DiscordWebhook
    bus: RedisBus
    detector: Detector
    probe: Probe
    notifier: Notifier
    incidents: IncidentManager
    scheduler: Scheduler


def build_context(settings: Settings | None = None) -> Context:
    settings = settings or get_settings()
    # Un seul pool HTTP pour toutes les intégrations sortantes.
    # `follow_redirects` est indispensable : status.moddy.app renvoie un 302 de
    # /index.json vers /en/index.json.
    http = httpx.AsyncClient(timeout=15, follow_redirects=True)

    store = Store(settings.redis_url)
    betterstack = BetterStack(settings, store, client=http)
    webhook = DiscordWebhook(settings.discord_webhook_url, client=http)
    bus = RedisBus(store, ack_timeout=settings.discord_bot_ack_timeout)
    detector = Detector(settings, store)
    probe = Probe(settings, store, http)
    notifier = Notifier(settings, store, bus, webhook)
    incidents = IncidentManager(settings, store, betterstack, notifier)
    scheduler = Scheduler(settings, store, detector, incidents, notifier, betterstack, probe)

    return Context(
        settings=settings,
        store=store,
        http=http,
        betterstack=betterstack,
        webhook=webhook,
        bus=bus,
        detector=detector,
        probe=probe,
        notifier=notifier,
        incidents=incidents,
        scheduler=scheduler,
    )


async def startup(ctx: Context) -> None:
    await ctx.store.connect()
    await ctx.detector.load()
    await ctx.bus.start(command_handler=ctx.incidents.handle_command)
    ctx.scheduler.start()


async def shutdown(ctx: Context) -> None:
    """SIGTERM : Railway redéploie souvent, on flush avant de sortir."""
    await ctx.scheduler.stop()
    await ctx.bus.stop()
    await ctx.store.flush()
    await ctx.store.close()
    await ctx.http.aclose()


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx

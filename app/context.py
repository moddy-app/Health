"""Câblage des composants — un seul objet passé à l'API, aux boucles et au bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx
from fastapi import Request

from .bot.client import HealthBot
from .bot.publisher import IncidentPublisher
from .bot.sticky import StickyManager
from .config import Settings, get_settings
from .core.detector import Detector
from .core.incident import IncidentManager
from .core.notifier import Notifier
from .core.probe import Probe
from .core.scheduler import Scheduler
from .integrations.betterstack import BetterStack
from .integrations.discord_webhook import DiscordWebhook
from .state import Store

log = logging.getLogger("hm.context")


@dataclass
class Context:
    settings: Settings
    store: Store
    http: httpx.AsyncClient
    betterstack: BetterStack
    webhook: DiscordWebhook
    publisher: IncidentPublisher
    sticky: StickyManager
    bot: HealthBot | None
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

    # Le publisher et le sticky existent toujours ; sans token Discord ils
    # restent simplement désactivés et tout part par webhook.
    publisher = IncidentPublisher(settings)
    sticky = StickyManager(settings, store)

    detector = Detector(settings, store)
    probe = Probe(settings, store, http)
    notifier = Notifier(settings, store, publisher, webhook)
    incidents = IncidentManager(settings, store, betterstack, notifier)
    scheduler = Scheduler(settings, store, detector, incidents, notifier, betterstack, probe)

    bot = HealthBot(settings) if settings.bot_enabled else None

    ctx = Context(
        settings=settings,
        store=store,
        http=http,
        betterstack=betterstack,
        webhook=webhook,
        publisher=publisher,
        sticky=sticky,
        bot=bot,
        detector=detector,
        probe=probe,
        notifier=notifier,
        incidents=incidents,
        scheduler=scheduler,
    )

    if bot is not None:
        # Câblage croisé : le bot a besoin du contexte pour exécuter les
        # commandes, le publisher et le sticky ont besoin du bot pour parler.
        bot.bind(ctx, sticky)
        publisher.bind(bot, sticky)
        sticky.bind(bot)
    else:
        log.warning("DISCORD_TOKEN absent : le bot est désactivé, tout passe par le webhook")

    return ctx


async def startup(ctx: Context) -> None:
    await ctx.store.connect()
    await ctx.detector.load()
    await ctx.sticky.load()
    ctx.scheduler.start()


async def shutdown(ctx: Context) -> None:
    """SIGTERM : Railway redéploie souvent, on flush avant de sortir."""
    await ctx.scheduler.stop()
    await ctx.sticky.stop()
    await ctx.store.flush()
    await ctx.store.close()
    await ctx.http.aclose()


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx

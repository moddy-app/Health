"""Client discord.py du monitor.

Il vit dans la même event loop que FastAPI et que les boucles du scheduler.
Perdre la gateway ne doit rien bloquer : discord.py reconnecte seul en arrière-
plan, et le reste du monitor continue de tourner — c'est le comportement voulu.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands as ext_commands

from ..config import Settings
from .commands import StatusCommands, on_tree_error
from .views import StickyStatusView

log = logging.getLogger("hm.bot")


def _intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True  # résolution du salon, cache des guilds
    intents.guild_messages = True  # détecter qu'un tiers a poussé le sticky
    # Pas de `message_content` : le sticky ne lit jamais le contenu d'un
    # message, seulement son ID. Inutile de demander un intent privilégié.
    return intents


class HealthBot(ext_commands.Bot):
    def __init__(self, settings: Settings) -> None:
        super().__init__(command_prefix=ext_commands.when_mentioned, intents=_intents())
        self._s = settings
        self.ctx = None  # posé par `context.build_context`
        self.sticky = None

    def bind(self, ctx, sticky) -> None:
        self.ctx = ctx
        self.sticky = sticky

    # ------------------------------------------------------------------
    async def setup_hook(self) -> None:
        # Sans `add_view`, le bouton Refresh est mort après chaque
        # redéploiement — et Railway redéploie souvent.
        self.add_view(StickyStatusView())
        self.tree.add_command(StatusCommands())
        self.tree.on_error = on_tree_error

        guild_id = self._s.discord_guild_id
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("commandes synchronisées sur la guild %s", guild_id)

    async def on_ready(self) -> None:
        log.info("bot connecté en tant que %s", self.user)
        if self.sticky is not None:
            await self.sticky.load()
            await self.sticky.refresh({})

    async def on_message(self, message: discord.Message) -> None:
        """Un tiers a parlé dans le salon : le sticky doit redescendre."""
        if self.sticky is not None and message.channel.id == self._channel_id:
            self.sticky.on_channel_message(message.id)

    @property
    def _channel_id(self) -> int:
        try:
            return int(self._s.discord_status_channel_id)
        except (TypeError, ValueError):
            return 0

    async def on_error(self, event: str, *args, **kwargs) -> None:
        # Règle §11 : rien ne remonte, on log et on continue.
        log.exception("event %s en échec", event)


async def run(bot: HealthBot, token: str) -> None:
    """Lance le bot sans jamais faire tomber le reste du process.

    Un token invalide, une gateway refusée : le monitor doit continuer à
    détecter, à alerter par webhook et à servir `/v1/status`.
    """
    try:
        async with bot:
            await bot.start(token)
    except Exception:
        log.exception("le bot Discord s'est arrêté ; le monitor continue sans lui")

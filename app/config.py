"""Settings pydantic depuis l'environnement.

Toutes les listes sont lues en CSV brut puis exposées via des propriétés : les
variables Railway sont des chaînes simples, pas du JSON.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# `core.impact` ne dépend de rien : pas de cycle d'import.
from .core.impact import WILDCARD, parse_rules

# Noms d'affichage par défaut. Une dérivation automatique donnerait « Moddy Api »
# là où la status page dit « API » — d'où la table explicite, surchargeable par
# HM_SERVICE_NAMES.
DEFAULT_SERVICE_NAMES: dict[str, str] = {
    "moddy-bot": "Moddy Bot",
    "moddy-api": "API",
    "moddy-altguard": "AltGuard",
    "moddy-feeds": "Feeds",
    "moddy-dashboard": "Dashboard",
    "moddy-website": "Website",
}


def _csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _csv_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _csv(raw):
        key, _, value = item.partition(":")
        key, value = key.strip(), value.strip()
        if key and value:
            out[key] = value
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Serveur ---
    port: int = 8080
    hm_ingest_token: str = ""
    hm_log_level: str = "INFO"

    # --- Services surveillés ---
    hm_services: str = "moddy-bot,moddy-api,moddy-altguard,moddy-feeds"
    hm_critical_services: str = "moddy-bot,moddy-api"
    hm_service_names: str = ""
    # Propagation d'impact : `source>cible1,cible2`, entrées séparées par `;`,
    # `*` valant « tous les autres services connus ». `=down` sur une cible
    # propage un `down` au lieu du `degraded` par défaut : sans son backend, le
    # dashboard n'affiche plus rien, il est down.
    hm_impact_map: str = (
        "moddy-bot>*;moddy-api>moddy-website,moddy-dashboard=down,moddy-bot"
    )
    hm_heartbeat_ttl: int = 60
    hm_check_interval: int = 15
    hm_failure_threshold: int = 3
    hm_recovery_threshold: int = 2
    hm_startup_grace: int = 90
    # « 3 cycles consécutifs en échec ET minimum 60s de silence »
    hm_min_silence: int = 60
    # « max 1 message Discord par service par tranche de 5 min pour le même état »
    hm_notify_rate_limit: int = 300

    # --- Checks HTTP actifs ---
    # `service:url`, séparés par `,`. Pour les services sans process capable de
    # pousser un heartbeat (dashboard, site statique) : le monitor sonde l'URL et
    # en fait un heartbeat synthétique.
    hm_probe_map: str = ""
    hm_probe_interval: int = 30
    hm_probe_timeout: float = 10.0

    # --- Redis ---
    redis_url: str = ""

    # --- Better Stack ---
    betterstack_token: str = ""
    betterstack_api_base: str = "https://uptime.betterstack.com/api/v2"
    betterstack_status_page_id: str = ""
    betterstack_index_url: str = "https://status.moddy.app/index.json"
    betterstack_poll_interval: int = 300
    betterstack_webhook_secret: str = ""
    hm_bs_resource_map: str = ""
    hm_self_heartbeat_url: str = ""
    hm_self_heartbeat_interval: int = 60
    # Alerte si la souscription webhook semble coupée (10 échecs = désactivation
    # silencieuse côté Better Stack).
    hm_bs_webhook_silence_alert: int = 86_400

    # --- Discord ---
    discord_webhook_url: str = ""
    discord_status_channel_id: str = ""
    discord_guild_id: str = ""
    discord_status_page_url: str = "https://status.moddy.app"
    # Délai d'ACK du bot avant bascule sur le webhook.
    discord_bot_ack_timeout: float = 5.0
    # Rafraîchissement périodique du sticky message.
    discord_sticky_interval: int = 120

    # --- API publique ---
    hm_public_rate_limit: str = "60/minute"
    hm_cors_origins: str = "https://moddy.app,https://dashboard.moddy.app"
    hm_public_cache_ttl: int = 30

    # ------------------------------------------------------------------
    # Vues parsées
    # ------------------------------------------------------------------
    @property
    def services(self) -> list[str]:
        """Services suivis par le détecteur : ceux qui poussent, plus ceux qu'on sonde.

        Un service sondé produit des heartbeats comme les autres, à ceci près
        que c'est le monitor qui les écrit. Il n'y a donc pas de raison de le
        déclarer deux fois : `HM_PROBE_MAP` suffit à le faire surveiller.
        """
        monitored = _csv(self.hm_services)
        for service in self.probe_map:
            if service not in monitored:
                monitored.append(service)
        return monitored

    @property
    def probe_map(self) -> dict[str, str]:
        # `_csv_map` coupe à la première `:` — l'identifiant n'en contient pas,
        # le schéma de l'URL reste donc entier.
        return _csv_map(self.hm_probe_map)

    @property
    def probe_ttl(self) -> int:
        """TTL du heartbeat synthétique : trois sondes manquées, comme à l'ingestion."""
        return max(self.hm_heartbeat_ttl, self.hm_probe_interval * 3)

    @property
    def critical_services(self) -> list[str]:
        return [s for s in _csv(self.hm_critical_services) if s in self.services]

    @property
    def service_names(self) -> dict[str, str]:
        names = dict(DEFAULT_SERVICE_NAMES)
        names.update(_csv_map(self.hm_service_names))
        return names

    def display_name(self, service: str) -> str:
        return self.service_names.get(service, service.replace("-", " ").title())

    @property
    def bs_resource_map(self) -> dict[str, str]:
        return _csv_map(self.hm_bs_resource_map)

    @property
    def known_services(self) -> list[str]:
        """Tous les services que le monitor sait nommer, dans un ordre stable.

        Plus large que `services` : le site et le dashboard ne poussent pas de
        heartbeat mais existent comme ressources Better Stack, et peuvent être
        dégradés par ricochet.
        """
        ordered = list(self.services)
        for candidate in list(self.bs_resource_map):
            if candidate not in ordered:
                ordered.append(candidate)
        for source, targets in parse_rules(self.hm_impact_map).items():
            for candidate in [source, *targets]:
                if candidate != WILDCARD and candidate not in ordered:
                    ordered.append(candidate)
        return ordered

    @property
    def cors_origins(self) -> list[str]:
        return _csv(self.hm_cors_origins)

    @property
    def betterstack_enabled(self) -> bool:
        return bool(self.betterstack_token and self.betterstack_status_page_id)

    @property
    def rate_limit(self) -> tuple[int, int]:
        """`60/minute` -> (60, 60). Renvoie (limite, fenêtre en secondes)."""
        units = {"second": 1, "minute": 60, "hour": 3600, "day": 86_400}
        count, _, unit = self.hm_public_rate_limit.partition("/")
        try:
            return int(count), units.get(unit.strip().rstrip("s").lower(), 60)
        except ValueError:
            return 60, 60


@lru_cache
def get_settings() -> Settings:
    return Settings()

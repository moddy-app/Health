"""Envoi et édition directs via webhook Discord.

Deuxième maillon de la chaîne de redondance : si le bot est mort, c'est ce
chemin qui doit faire partir l'alerte — précisément le cas où on en a le plus
besoin. Il ne dépend donc que de `httpx` et de l'URL du webhook.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..render.raw import IS_COMPONENTS_V2

log = logging.getLogger("hm.discord")

_MAX_ATTEMPTS = 3


class DiscordWebhook:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self._url = url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        # Passe à False dès qu'un envoi Components V2 est refusé : inutile de
        # retenter un format que ce webhook ne supporte pas.
        self.components_v2 = True

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, payload: dict) -> httpx.Response | None:
        client = await self._http()
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.request(method, url, json=payload)
            except Exception as exc:
                log.warning(
                    "webhook %s échec réseau (%s/%s): %s", method, attempt + 1, _MAX_ATTEMPTS, exc
                )
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except Exception:
                    pass
                log.warning("webhook rate-limited, retry dans %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 30.0))
                continue

            if response.status_code >= 500:
                await asyncio.sleep(2**attempt)
                continue

            return response
        return None

    @staticmethod
    def _message_id(response: httpx.Response) -> str | None:
        try:
            return str(response.json().get("id")) or None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Envoi
    # ------------------------------------------------------------------
    async def send(self, components: list[dict], embed: dict | None = None) -> str | None:
        """Poste un message. Renvoie son ID, ou None si tout a échoué."""
        if not self.enabled:
            return None

        if self.components_v2:
            url = f"{self._url}?wait=true&with_components=true"
            response = await self._request(
                "POST", url, {"flags": IS_COMPONENTS_V2, "components": components}
            )
            if response is not None and response.is_success:
                return self._message_id(response)
            if response is not None:
                log.warning(
                    "webhook Components V2 refusé (%s): %s", response.status_code, response.text[:300]
                )
                if response.status_code == 400:
                    # Format non supporté : on bascule définitivement sur l'embed.
                    self.components_v2 = False

        if embed is None:
            return None
        response = await self._request("POST", f"{self._url}?wait=true", {"embeds": [embed]})
        if response is not None and response.is_success:
            log.info("message envoyé en repli embed")
            return self._message_id(response)
        if response is not None:
            log.error("webhook embed refusé (%s): %s", response.status_code, response.text[:300])
        return None

    # ------------------------------------------------------------------
    # Édition
    # ------------------------------------------------------------------
    async def edit(
        self, message_id: str, components: list[dict], embed: dict | None = None
    ) -> bool:
        """PATCH /webhooks/{id}/{token}/messages/{message_id}."""
        if not self.enabled or not message_id:
            return False

        base = f"{self._url}/messages/{message_id}"
        if self.components_v2:
            response = await self._request(
                "PATCH",
                f"{base}?with_components=true",
                {"flags": IS_COMPONENTS_V2, "components": components},
            )
            if response is not None and response.is_success:
                return True
            if response is not None:
                log.warning(
                    "édition Components V2 refusée (%s): %s",
                    response.status_code,
                    response.text[:300],
                )
                if response.status_code == 404:
                    # Message supprimé : l'appelant doit en reposter un.
                    return False
                if response.status_code == 400:
                    self.components_v2 = False

        if embed is None:
            return False
        response = await self._request("PATCH", base, {"embeds": [embed]})
        return bool(response is not None and response.is_success)

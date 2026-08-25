"""Modals V2 de gestion de crise.

Contraintes de l'API : 5 composants top-level maximum, chacun un `Label` ou un
`TextDisplay` ; `disabled` est refusé ; les valeurs se lisent sur `.component`.
Le texte affiché vient de `Label.text`, `TextInput.label` étant déprécié.
"""

from __future__ import annotations

import logging

import discord
from discord import ui

from ..config import Settings
from ..render import colors, theme
from ..render.layout import build_notice_view
from ..util import iso, parse_iso

log = logging.getLogger("hm.bot.modals")

# Format demandé au staff pour une fenêtre de maintenance.
WINDOW_HINT = "2026-08-25 02:00 -> 04:00"


def _service_options(settings: Settings) -> list[discord.CheckboxGroupOption]:
    """Les services viennent de la configuration, jamais d'une liste en dur.

    Ajouter un service reste une affaire de variables d'environnement (§6) —
    y compris dans les modals. `CheckboxGroup` plafonne à 10 options.
    """
    return [
        discord.CheckboxGroupOption(label=settings.display_name(service), value=service)
        for service in settings.known_services[:10]
    ]


def _affected_label(settings: Settings) -> ui.Label:
    """Le choix des services touchés.

    `max_values` ne peut pas être une constante : Discord exige au moins autant
    d'options que ce qu'on autorise à cocher, et refuse le modal entier sinon
    (`options: Must be 10 or more in length`). Avec six services configurés, un
    `max_values=10` en dur rendait toutes les commandes inutilisables.
    """
    options = _service_options(settings)
    return ui.Label(
        text="Affected services",
        component=ui.CheckboxGroup(
            options=options,
            min_values=1,
            max_values=max(len(options), 1),
            required=True,
        ),
    )


def _notify_label() -> ui.Label:
    return ui.Label(
        text="Notify subscribers",
        component=ui.CheckboxGroup(
            options=[discord.CheckboxGroupOption(label="Send email to subscribers")],
            min_values=0,
            max_values=1,
            required=False,
        ),
    )


def _severity_label() -> ui.Label:
    return ui.Label(
        text="Severity",
        component=ui.RadioGroup(
            options=[
                discord.RadioGroupOption(label="Degraded Performance", value=colors.DEGRADED),
                discord.RadioGroupOption(label="Partial Outage", value=colors.PARTIAL_OUTAGE),
                discord.RadioGroupOption(label="Major Outage", value=colors.MAJOR_OUTAGE),
            ],
        ),
    )


def parse_window(raw: str) -> tuple[str | None, str | None]:
    """`YYYY-MM-DD HH:MM -> HH:MM` (ou une seconde date complète) -> deux ISO-8601.

    Un seul champ pour la fenêtre : avec `starts_at` et `ends_at` séparés, le
    modal dépasserait les 5 composants top-level.
    """
    for arrow in ("->", "→", "..", " to "):
        if arrow in raw:
            left, right = raw.split(arrow, 1)
            break
    else:
        return None, None

    start = parse_iso(left.strip().replace(" ", "T"))
    if start is None:
        return None, None

    end_raw = right.strip()
    if len(end_raw) <= 5:  # « 04:00 » : même jour que le début
        end_raw = f"{start.date().isoformat()} {end_raw}"
    end = parse_iso(end_raw.replace(" ", "T"))
    return iso(start), iso(end) if end else None


class _StaffModal(ui.Modal):
    """Socle commun : défère, exécute la commande, répond en Components V2."""

    action = ""

    def __init__(self, ctx) -> None:
        super().__init__()
        self._ctx = ctx

    def build_payload(self, interaction: discord.Interaction) -> dict | None:
        raise NotImplementedError

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Obligatoire : créer un incident appelle Better Stack *et* publie sur
        # Discord, ce qui dépasse facilement la fenêtre de 3s d'une interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)

        payload = self.build_payload(interaction)
        if payload is None:
            await interaction.followup.send(
                view=build_notice_view(
                    f"{theme.EMOJI_ALERT} Invalid maintenance window. Expected `{WINDOW_HINT}`.",
                    accent=colors.ACCENT_MAJOR,
                ),
                ephemeral=True,
            )
            return

        incident = await self._ctx.incidents.handle_command(self.action, payload)
        if incident is None:
            await interaction.followup.send(
                view=build_notice_view(
                    f"{theme.EMOJI_ALERT} Nothing was published. Check the monitor logs.",
                    accent=colors.ACCENT_MAJOR,
                ),
                ephemeral=True,
            )
            return

        url = incident.get("url")
        detail = f"[View on the status page]({url})" if url else "Discord only — Better Stack is unavailable."
        await interaction.followup.send(
            view=build_notice_view(
                f"{theme.EMOJI_OK} **{incident.get('title')}**\n-# {detail}",
                accent=colors.ACCENT_RESOLVED,
            ),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("modal %s en échec", self.action, exc_info=error)
        view = build_notice_view(
            f"{theme.EMOJI_ALERT} The command failed. The monitor logged it.",
            accent=colors.ACCENT_MAJOR,
        )
        if interaction.response.is_done():
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)


class IncidentCreateModal(_StaffModal, title="Create Incident"):
    action = "incident.create"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.incident_title = ui.Label(
            text="Title",
            description="Shown as the incident headline",
            component=ui.TextInput(
                style=discord.TextStyle.short,
                max_length=100,
                placeholder="Moddy Bot is offline",
            ),
        )
        self.message = ui.Label(
            text="Message",
            description="Public description, shown on the status page",
            component=ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500),
        )
        self.level = _severity_label()
        self.affected = _affected_label(ctx.settings)
        self.notify = _notify_label()
        for item in (self.incident_title, self.message, self.level, self.affected, self.notify):
            self.add_item(item)

    def build_payload(self, interaction: discord.Interaction) -> dict:
        return {
            "title": self.incident_title.component.value,
            "message": self.message.component.value,
            "level": (self.level.component.values or [colors.PARTIAL_OUTAGE])[0],
            "affected": list(self.affected.component.values),
            "notify": bool(self.notify.component.values),
            "author": interaction.user.display_name,
        }


class IncidentUpdateModal(_StaffModal, title="Post an Update"):
    action = "incident.update"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.message = ui.Label(
            text="Message",
            description="Public update, shown on the status page",
            component=ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500),
        )
        self.notify = _notify_label()
        self.add_item(self.message)
        self.add_item(self.notify)

    def build_payload(self, interaction: discord.Interaction) -> dict:
        # L'incident concerné est déduit de `hm:incident:active`, jamais demandé
        # au staff : il n'y en a qu'un à la fois.
        return {
            "message": self.message.component.value,
            "notify": bool(self.notify.component.values),
            "author": interaction.user.display_name,
        }


class IncidentResolveModal(_StaffModal, title="Resolve Incident"):
    action = "incident.resolve"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.message = ui.Label(
            text="Resolution",
            description="Closing message, shown on the status page",
            component=ui.TextInput(
                style=discord.TextStyle.paragraph,
                max_length=1500,
                default="This incident has been resolved.",
            ),
        )
        self.notify = _notify_label()
        self.add_item(self.message)
        self.add_item(self.notify)

    def build_payload(self, interaction: discord.Interaction) -> dict:
        return {
            "message": self.message.component.value,
            "notify": bool(self.notify.component.values),
            "author": interaction.user.display_name,
        }


class MaintenanceModal(_StaffModal, title="Schedule Maintenance"):
    action = "maintenance.create"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self.maintenance_title = ui.Label(
            text="Title",
            component=ui.TextInput(
                style=discord.TextStyle.short, max_length=100, default="Scheduled Maintenance"
            ),
        )
        self.message = ui.Label(
            text="Message",
            component=ui.TextInput(style=discord.TextStyle.paragraph, max_length=1500),
        )
        self.window = ui.Label(
            text="Window (UTC)",
            description=f"Start and end, e.g. {WINDOW_HINT}",
            component=ui.TextInput(style=discord.TextStyle.short, placeholder=WINDOW_HINT),
        )
        self.affected = _affected_label(ctx.settings)
        for item in (self.maintenance_title, self.message, self.window, self.affected):
            self.add_item(item)

    def build_payload(self, interaction: discord.Interaction) -> dict | None:
        starts_at, ends_at = parse_window(self.window.component.value or "")
        # `ends_at` est obligatoire côté Better Stack pour un report de type
        # maintenance : sans lui, la création serait refusée.
        if not ends_at:
            return None
        return {
            "title": self.maintenance_title.component.value,
            "message": self.message.component.value,
            "affected": list(self.affected.component.values),
            "starts_at": starts_at,
            "ends_at": ends_at,
            "author": interaction.user.display_name,
        }

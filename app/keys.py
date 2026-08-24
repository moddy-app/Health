"""Noms des clés Redis (§2 de la doc d'implémentation)."""

from __future__ import annotations

# État des services
HB = "hm:hb:{service}"
STATE = "hm:state:{service}"

# Incidents
INCIDENT_ACTIVE = "hm:incident:active"
INCIDENT_HISTORY = "hm:incident:history"
INCIDENT_HISTORY_MAX = 100

# Better Stack
BS_OWNED = "hm:bs:owned"
BS_SEEN_UPDATES = "hm:bs:seen_updates"
BS_CURSOR = "hm:bs:cursor"
BS_LAST_EVENT = "hm:bs:last_event_at"

# API publique
STATUS_PUBLIC = "hm:status:public"

# Discord
STICKY_MESSAGE_ID = "hm:sticky:message_id"
NOTIFY_SENT = "hm:notify:sent"
NOTIFY_QUEUE = "hm:notify:queue"
NOTIFY_RATELIMIT = "hm:notify:rl:{service}:{status}"

# Divers
PUBLIC_RATELIMIT = "hm:ratelimit:{ip}"

# Canaux pubsub (bot <-> monitor)
CH_NOTIFY = "moddy:hm:notify"
CH_NOTIFY_ACK = "moddy:hm:notify:ack"
CH_COMMAND = "moddy:hm:command"


def hb(service: str) -> str:
    return HB.format(service=service)


def state(service: str) -> str:
    return STATE.format(service=service)

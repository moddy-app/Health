# Notification Discord

## Chaîne de redondance

```
1. Bot Moddy (pubsub Redis `moddy:hm:notify`)
       │ échec de publication, ou pas d'ACK sous 5s
       ▼
2. Webhook Discord direct (HTTP depuis le monitor)
       │ échec (Discord down, webhook non configuré)
       ▼
3. File de rattrapage `hm:notify:queue` + log ERROR
```

Le niveau 1 est préféré : le bot peut éditer ses propres messages, gérer le
sticky et les interactions. Le niveau 2 garantit que **si le bot est mort,
l'alerte part quand même** — précisément le cas où on en a le plus besoin.

`Notifier.deliver()` renvoie un booléen ; `dispatch()` empile dans la file si la
livraison a échoué. Cette séparation est nécessaire : un incident qui porte déjà
un `discord_message_id` d'un envoi précédent ne doit pas être compté comme livré
sur la base de ce champ.

### ACK du bot

Le monitor publie sur `moddy:hm:notify` un message portant un `nonce`. Le bot
répond sur `moddy:hm:notify:ack` avec `{"nonce": ..., "message_id": ...}`. Un ACK
sans `nonce` est accepté et sert la plus ancienne attente en cours (tolérance aux
relais simplifiés).

Sans ACK sous `DISCORD_BOT_ACK_TIMEOUT` (5s), bascule webhook.

### Idempotence

Clé de déduplication : `sha1(incident_id + nombre d'updates + canal)`, stockée
dans `hm:notify:sent`.

La vérification est faite **tous canaux confondus** avant d'essayer quoi que ce
soit : une version donnée d'un incident ne part qu'une fois. Sans ça, un incident
déjà relayé par le bot repartait une seconde fois par webhook au premier retry —
deux messages pour un seul événement.

### File de rattrapage

`hm:notify:queue`, liste Redis, plafonnée à 200 entrées. Vidée toutes les 30s par
la boucle `notify-queue`, **dans l'ordre** : un message qui échoue est remis en
tête (`lpush`) et le drain s'arrête là. Le rejouer plus tard dans le désordre
donnerait un fil d'incident incohérent.

Chaque entrée conserve son `queued_at` d'origine.

### Édition

| Transport d'origine | Nouvelle version |
|---|---|
| `bot` | `incident.edit` publié sur le bus |
| `webhook` | `PATCH /webhooks/{id}/{token}/messages/{message_id}` |
| `bot`, mais bot désormais muet | Le webhook ne peut pas éditer le message d'un autre auteur → **nouveau** message |

Un `404` sur l'édition (message supprimé à la main) déclenche aussi un repost.

## Format du message

Le monitor construit le **JSON brut** des Components V2. Le bot le relaie tel
quel, le webhook l'envoie directement : une seule fonction de rendu
(`render/components.py`), deux transports.

### Container 1 — en-tête

`accent_color` selon le niveau, une Section (`type: 9`) portant le titre et un
bouton lien.

```json
{
  "type": 17,
  "accent_color": 15280939,
  "components": [
    {
      "type": 9,
      "accessory": { "type": 2, "style": 5, "label": "View Incident",
                     "url": "https://status.moddy.app/en/incident/995593" },
      "components": [
        { "type": 10, "content": "### <:error_circle_white:...> Major Outage – Bot & API Unavailable" }
      ]
    },
    { "type": 10,
      "content": "**Created by:** Moddy Health Monitor\n**Affected services:** ``Moddy Bot``, ``API``\n**Status:** <:verified2:...>Resolved" }
  ]
}
```

Sans URL — incident `degraded`, ou Better Stack injoignable — la Section est
remplacée par un simple TextDisplay : l'API Discord refuse une Section sans
`accessory`.

### Container 2 — historique

`accent_color: null`, une entrée par update, séparées par un `type: 14`.

```json
{ "type": 17, "accent_color": null, "components": [
  { "type": 10, "content": "### **Updates:**" },
  { "type": 10, "content": "**Created** — <t:1785763800:F> :\n> ..." },
  { "type": 14, "divider": true, "spacing": 1 },
  { "type": 10, "content": "**Resolved** — <t:1785941143:F> :\n> ..." }
]}
```

Toujours `<t:unix:F>` : Discord affiche la date dans le fuseau de chaque lecteur.

Un message Discord plafonne à 40 composants ; seuls les **15 derniers** updates
sont rendus, précédés d'une ligne `-# N earlier update(s) not shown.` le cas
échéant. Un message multi-ligne est intégralement mis en blockquote (`> ` sur
chaque ligne), sans quoi seule la première ligne serait citée.

### Table de rendu

| Élément | Valeur |
|---|---|
| `accent_color` incident majeur / partiel | `15280939` (`#E93A3A`) |
| `accent_color` dégradé | `15774258` (`#F0B232`) |
| `accent_color` maintenance | `5793266` (`#5865F2`) |
| `accent_color` résolu | `5763719` (`#57F287`) |
| Emoji en cours | `<:error_circle_white:1534635025629319419>` |
| Emoji résolu | `<:verified2:1495440135163084870>` |

Le statut affiché a deux valeurs, comme les deux emojis : `Ongoing` et
`Resolved` (`Maintenance` pour ce type d'incident).

## Envoi par webhook

```python
POST {WEBHOOK_URL}?wait=true&with_components=true
{"flags": 32768, "components": [...]}
```

`32768` = `IS_COMPONENTS_V2` (`1 << 15`).

**Repli dégradé :** si le webhook renvoie `400` sur ce format, `components_v2`
passe à `False` pour la durée du process et tout repart en embed classique
(`build_incident_embed`). Titre, description, services affectés, statut et
dernier update y survivent ; l'historique complet et le bouton, non.

Ce maillon reste **le seul non validé contre le vrai Discord** : il demande une
URL de webhook réelle. C'est le premier test à faire au déploiement.

Retries : 3 tentatives, backoff `2^n`, respect du `retry_after` sur 429, retry
sur 5xx.

## Sticky message

Le sticky appartient au **bot** (`views/status_sticky.py` côté bot). Le monitor
ne fait que publier un signal :

```json
{ "action": "sticky.refresh",
  "payload": { "channel_id": "...", "status": { ...réponse /v1/status... } } }
```

Envoyé toutes les 2 minutes (`DISCORD_STICKY_INTERVAL`) et **immédiatement** dès
que le niveau global change.

Reste à faire côté bot : écoute `on_message` avec debounce 5s, repost quand le
sticky n'est plus le dernier message, persistance de l'ID dans
`hm:sticky:message_id`, et View persistante `custom_id: hm:sticky:refresh`
enregistrée au démarrage via `register_persistent` — sinon le bouton est mort
après chaque redéploiement.

La réponse éphémère du bouton lit `hm:status:public` et `hm:hb:{service}`
directement dans Redis : pas d'appel HTTP interne, ça doit répondre même en
pleine crise.

## Canal de commande

Le bot ne parle jamais à Better Stack. Il publie sur `moddy:hm:command`, le
monitor consomme et exécute. Toute la logique d'incident reste en un seul
endroit, et le token Better Stack n'existe que dans le monitor.

Si Redis est down, le bot bascule sur `POST /ingest/command` avec le
`X-Health-Token` : même contrat, autre transport.

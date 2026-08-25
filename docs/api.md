# Référence HTTP

| Route | Auth | Réponse |
|---|---|---|
| `POST /ingest/heartbeat` | `X-Health-Token` | 200 / 401 / 503 |
| `POST /ingest/betterstack` | `?k=<secret>` | 202 / 204 / 403 |
| `GET /v1/status` | — | 200 / 429 |
| `GET /v1/status/banner` | — | 200 / 429 |
| `GET /health` | — | 200 |

La documentation OpenAPI est désactivée (`docs_url=None`, `openapi_url=None`) :
le service est interne, sa surface est ici.

---

## `POST /ingest/heartbeat`

Voir [heartbeat.md](heartbeat.md) pour le contrat complet.

```json
{ "ok": true, "received_at": "2026-08-24T19:42:11Z", "incident_active": false }
```

| Code | Cause |
|---|---|
| `401` | Token absent ou invalide |
| `503` | `HM_INGEST_TOKEN` non configuré côté monitor |
| `422` | Corps invalide (`service` manquant) |

---

## `POST /ingest/betterstack`

Webhook Better Stack. Répond **202 immédiatement**, traite en tâche de fond.

| Code | Cause |
|---|---|
| `202` | Accepté, traitement en cours |
| `204` | Corps illisible ou non-objet — accepté sans traitement, pour ne pas déclencher les retries |
| `403` | `?k=` absent ou faux alors qu'un secret est configuré |

---

## `GET /v1/status`

Sans authentification. Destiné au dashboard, au site, et à tout service tiers.

```json
{
  "status": "partial_outage",
  "updated_at": "2026-08-24T19:42:11Z",
  "services": [
    { "id": "moddy-bot", "name": "Moddy Bot", "status": "down",
      "reported": "down", "impacted_by": [], "since": "2026-08-24T19:38:02Z" },
    { "id": "moddy-api", "name": "API", "status": "degraded",
      "reported": "operational", "impacted_by": ["moddy-bot"],
      "since": "2026-08-20T04:11:00Z" }
  ],
  "incident": {
    "id": "inc_20260824_1942",
    "type": "incident",
    "level": "partial_outage",
    "title": "Partial Outage – Moddy Bot Unavailable",
    "message": "We are currently experiencing a service outage affecting Moddy Bot...",
    "affected": ["moddy-bot", "moddy-api", "moddy-website", "moddy-dashboard"],
    "started_at": "2026-08-24T19:42:00Z",
    "resolved_at": null,
    "starts_at": null,
    "ends_at": null,
    "url": "https://status.moddy.app/en/incident/1019848",
    "updates_count": 2,
    "last_update": { "at": "2026-08-24T19:55:00Z",
                     "message": "We identified the issue and have deployed a fix." }
  },
  "maintenance": null
}
```

Une maintenance en cours remplit `maintenance` avec la même forme, `starts_at`
et `ends_at` compris — la fenêtre planifiée par `/status maintenance` :

```json
"maintenance": {
  "id": "inc_20260825_0130",
  "type": "maintenance",
  "level": "maintenance",
  "title": "Scheduled Maintenance – API",
  "message": "We are performing scheduled maintenance on the API.",
  "affected": ["moddy-api"],
  "started_at": "2026-08-25T01:30:00Z",
  "resolved_at": null,
  "starts_at": "2026-08-25T02:00:00Z",
  "ends_at": "2026-08-25T04:00:00Z",
  "url": "https://status.moddy.app/en/incident/1019849",
  "updates_count": 1,
  "last_update": { "at": "2026-08-25T01:30:00Z",
                   "message": "We are performing scheduled maintenance on the API." }
}
```

### Champs par service

| Champ | Sens |
|---|---|
| `status` | Ce que vit l'utilisateur, **propagation d'impact comprise** |
| `reported` | Ce que le service dit de lui-même |
| `impacted_by` | Les services dont la panne a dégradé celui-ci |
| `since` | Horodatage du dernier changement d'état observé |

`status` et `reported` diffèrent quand un service sain est dégradé par une
dépendance. Un consommateur qui veut la santé technique brute lit `reported` ;
la bannière utilisateur lit `status`.

`services[]` ne liste que les services **surveillés** (`HM_SERVICES`). Website et
Dashboard n'y figurent pas — ils ne poussent pas de heartbeat — mais peuvent
apparaître dans `incident.affected`.

`starts_at`/`ends_at` ne sont renseignés que pour une maintenance : ils portent
la fenêtre planifiée (§`/status maintenance`), pas l'horodatage de publication
— celui-là reste `started_at`. Pour un incident ordinaire, les deux valent
`null`. Une maintenance qui n'a pas encore été ouverte par le staff
n'apparaît nulle part : ce champ ne montre que ce qui est déjà publié.

### `incident` et `maintenance`

Les deux valent `null` quand il n'y a rien en cours. Un incident de type
`maintenance` remplit `maintenance` et laisse `incident` à `null`, jamais les
deux. Le dashboard n'affiche la bannière que si le champ est non-null et choisit
sa couleur selon `level`.

### Protections

| Mesure | Valeur |
|---|---|
| Rate limit | `HM_PUBLIC_RATE_LIMIT` (60/minute) par IP, middleware maison sur le store |
| Cache | `hm:status:public`, TTL 30s, calculé dans la boucle de check |
| CORS | `HM_CORS_ORIGINS`, méthodes `GET` uniquement, sans credentials |
| Header | `Cache-Control: public, max-age=30` |

L'IP est lue dans `X-Forwarded-For` (premier élément) puis `request.client.host` :
Railway est derrière un proxy.

Un dépassement renvoie `429` avec `Retry-After`. Le compteur vit dans le store,
donc il survit en mémoire quand Redis est down.

Si `hm:status:public` est absent — tout premier démarrage, cache expiré pendant
un cycle lent — la réponse est recalculée à la volée depuis l'état en mémoire du
détecteur, puis remise en cache.

---

## `GET /v1/status/banner`

Payload minimal pour la bannière du dashboard.

```json
{ "level": "partial_outage",
  "title": "Partial Outage – Moddy Bot Unavailable",
  "url": "https://status.moddy.app/en/incident/1019848" }
```

Sans incident ni maintenance :

```json
{ "level": "operational", "title": null, "url": null }
```

`level` porte le niveau de l'incident en cours s'il y en a un, sinon le niveau
global des services.

---

## `GET /health`

Liveness du monitor lui-même. Renvoie `200` avec le corps `ok`, en `text/plain`.

**Sans aucune dépendance** : pas de Redis, pas de Better Stack, pas de Discord.
Cet endpoint doit répondre même quand tout le reste est cassé — c'est ce qui le
rend utilisable comme healthcheck Railway.

Il n'est pas soumis au rate limit, qui ne s'applique qu'aux routes `/v1/`.

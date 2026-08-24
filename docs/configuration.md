# Configuration

Toutes les variables sont lues depuis l'environnement par `app/config.py`
(pydantic-settings, insensible à la casse, `.env` supporté en local). Les listes
sont lues en **CSV brut** puis exposées par des propriétés : les variables
Railway sont des chaînes, pas du JSON.

`.env.example` contient un fichier complet prêt à copier.

## Minimum vital

Pour un service qui alerte réellement :

```env
HM_INGEST_TOKEN=<secret partagé avec les services>
HM_SERVICES=moddy-bot,moddy-api,moddy-altguard,moddy-feeds
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Sans `HM_INGEST_TOKEN`, l'ingestion répond 503. Sans `DISCORD_WEBHOOK_URL` ni
Redis, aucune alerte ne peut partir : tout finit dans la file de rattrapage.

## Serveur

| Variable | Défaut | Rôle |
|---|---|---|
| `PORT` | `8080` | Port d'écoute (injecté par Railway) |
| `HM_INGEST_TOKEN` | — | Secret partagé avec les services. Vide ⇒ ingestion refusée |
| `HM_LOG_LEVEL` | `INFO` | Niveau de log racine |

## Services surveillés

| Variable | Défaut | Rôle |
|---|---|---|
| `HM_SERVICES` | `moddy-bot,moddy-api,moddy-altguard,moddy-feeds` | Liste exhaustive des services attendus |
| `HM_CRITICAL_SERVICES` | `moddy-bot,moddy-api` | Filtrée sur `HM_SERVICES` ; détermine partial vs major |
| `HM_SERVICE_NAMES` | — | `id:Nom d'affichage`, séparés par `,`. Des défauts existent pour les services Moddy |
| `HM_IMPACT_MAP` | `moddy-bot>*;moddy-api>moddy-website,moddy-dashboard=down,moddy-bot` | Propagation d'impact |
| `HM_HEARTBEAT_TTL` | `60` | TTL de `hm:hb:{service}` — trois fois l'intervalle d'émission |
| `HM_CHECK_INTERVAL` | `15` | Période de la boucle de détection |
| `HM_FAILURE_THRESHOLD` | `3` | Cycles d'échec avant bascule |
| `HM_RECOVERY_THRESHOLD` | `2` | Cycles OK avant retour |
| `HM_STARTUP_GRACE` | `90` | Secondes sans aucune alerte après démarrage |
| `HM_MIN_SILENCE` | `60` | Silence minimum avant d'alerter sur un heartbeat expiré |
| `HM_NOTIFY_RATE_LIMIT` | `300` | Fenêtre du rate-limit par service et par état |

### `HM_SERVICE_NAMES`

Une dérivation automatique donnerait « Moddy Api » là où la status page dit
« API », d'où la table explicite. Les défauts couvrent `moddy-bot`, `moddy-api`,
`moddy-altguard`, `moddy-feeds`, `moddy-dashboard`, `moddy-website` ; un service
inconnu retombe sur un `title case` de son identifiant.

### `HM_IMPACT_MAP`

`source>cible1,cible2`, entrées séparées par `;`, `*` valant « tous les autres
services connus ». Une entrée vide désactive toute propagation. Les cibles
inconnues sont ignorées, les sources inconnues loguent un warning au démarrage.

Une cible peut déclarer la sévérité qu'elle subit : `moddy-dashboard=down`
propage un `down` là où le défaut est `degraded`. Une sévérité illisible retombe
sur `degraded` avec un warning plutôt que d'empêcher le démarrage, et une cible
nommée l'emporte sur le joker (`moddy-bot>*,moddy-dashboard=down`).

Détail du modèle : [detection.md](detection.md#propagation-dimpact).

## Checks HTTP actifs

| Variable | Défaut | Rôle |
|---|---|---|
| `HM_PROBE_MAP` | — | `service:url`, séparés par `,` ; vide ⇒ boucle non démarrée |
| `HM_PROBE_INTERVAL` | `30` | Période de la sonde |
| `HM_PROBE_TIMEOUT` | `10` | Timeout d'une requête, en secondes |

Pour les services qui n'ont aucun process capable de pousser un heartbeat — un
dashboard est un site statique. Le monitor `GET` l'URL et en fait un heartbeat
synthétique : **un 2xx signifie vivant**, tout le reste vaut `down`.

```env
HM_PROBE_MAP=moddy-dashboard:https://dashboard.moddy.app/healthz
```

Le découpage se fait à la première `:`, ce qui laisse le schéma de l'URL intact.
Un service cité ici n'a pas à l'être aussi dans `HM_SERVICES` : `services` fait
l'union des deux.

Détail du modèle : [heartbeat.md](heartbeat.md#les-services-sans-process).

## Redis

| Variable | Défaut | Rôle |
|---|---|---|
| `REDIS_URL` | — | Vide ⇒ le monitor tourne en mémoire seule, avec un warning |

Le fallback mémoire est un mode dégradé fonctionnel, pas un mode de
fonctionnement : sans Redis, l'état ne survit pas à un redéploiement et le bot ne
reçoit rien (le pubsub passe par Redis).

## Better Stack

| Variable | Défaut | Rôle |
|---|---|---|
| `BETTERSTACK_TOKEN` | — | Bearer d'écriture |
| `BETTERSTACK_API_BASE` | `https://uptime.betterstack.com/api/v2` | Base de l'API |
| `BETTERSTACK_STATUS_PAGE_ID` | — | `{spid}` des URLs |
| `BETTERSTACK_INDEX_URL` | `https://status.moddy.app/index.json` | Poll de réconciliation ; vide ⇒ boucle non démarrée |
| `BETTERSTACK_POLL_INTERVAL` | `300` | Période du poll (secours uniquement) |
| `BETTERSTACK_WEBHOOK_SECRET` | — | Placé en query string sur `/ingest/betterstack` ; vide ⇒ endpoint ouvert |
| `HM_BS_RESOURCE_MAP` | — | `service:resource_id`, séparés par `,` |
| `HM_SELF_HEARTBEAT_URL` | — | URL de ping ; vide ⇒ boucle non démarrée |
| `HM_SELF_HEARTBEAT_INTERVAL` | `60` | Période du ping |
| `HM_BS_WEBHOOK_SILENCE_ALERT` | `86400` | Silence au-delà duquel la souscription est jugée coupée |

L'écriture n'est active que si `BETTERSTACK_TOKEN` **et**
`BETTERSTACK_STATUS_PAGE_ID` sont renseignés (`Settings.betterstack_enabled`).
Sinon les appels sont ignorés avec un log de debug : le monitor reste
parfaitement fonctionnel sur Discord seul.

## Discord

| Variable | Défaut | Rôle |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | Second maillon de la redondance |
| `DISCORD_STATUS_CHANNEL_ID` | — | Salon du sticky et des incidents |
| `DISCORD_GUILD_ID` | — | Serveur, transmis au bot |
| `DISCORD_STATUS_PAGE_URL` | `https://status.moddy.app` | Base des URLs d'incident reconstruites |
| `DISCORD_BOT_ACK_TIMEOUT` | `5` | Secondes d'attente de l'ACK avant bascule webhook |
| `DISCORD_STICKY_INTERVAL` | `120` | Période de rafraîchissement du sticky |

## API publique

| Variable | Défaut | Rôle |
|---|---|---|
| `HM_PUBLIC_RATE_LIMIT` | `60/minute` | `N/unité`, unités `second`, `minute`, `hour`, `day` |
| `HM_CORS_ORIGINS` | `https://moddy.app,https://dashboard.moddy.app` | Origines autorisées |
| `HM_PUBLIC_CACHE_TTL` | `30` | TTL de `hm:status:public` et `max-age` du header |

Une valeur `HM_PUBLIC_RATE_LIMIT` illisible retombe silencieusement sur
`60/minute` plutôt que d'empêcher le démarrage.

## Propriétés dérivées

`Settings` expose des vues parsées, à préférer aux chaînes brutes :

| Propriété | Contenu |
|---|---|
| `services` | `HM_SERVICES` ∪ clés de `HM_PROBE_MAP` |
| `critical_services` | Liste, filtrée sur `services` |
| `service_names`, `display_name(id)` | Noms d'affichage |
| `known_services` | `services` ∪ clés de `HM_BS_RESOURCE_MAP` ∪ noms de `HM_IMPACT_MAP` |
| `bs_resource_map` | `{service: resource_id}` |
| `probe_map` | `{service: url}` |
| `probe_ttl` | TTL du heartbeat synthétique : trois sondes |
| `cors_origins` | Liste |
| `betterstack_enabled` | Booléen |
| `rate_limit` | `(limite, fenêtre en secondes)` |

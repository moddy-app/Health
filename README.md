# Moddy Health Monitor

Service de monitoring interne de l'écosystème Moddy. Headless, déployé sur Railway,
autonome vis-à-vis du reste de l'infrastructure.

Le monitor **ne va pas chercher** l'état des services : ce sont les services qui
**poussent** leur état à intervalle régulier (dead man's switch). Il ne dépend de rien
de ce qu'il surveille — pas de PostgreSQL, pas d'appel vers l'API Moddy, Redis
uniquement pour la persistance, avec fallback mémoire.

La spécification complète est dans [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md).

```
moddy-bot      ─┐
moddy-api      ─┤
moddy-altguard ─┼──POST /ingest/heartbeat──▶  moddy-health-monitor
moddy-feeds    ─┘                                    │
                                                     ├─▶ Redis (état + TTL)
                                                     ├─▶ Better Stack (status page)
                                                     ├─▶ Discord (bot ou webhook)
                                                     └─▶ GET /v1/status (public)
```

## État d'avancement

| Étape | Contenu | État |
|---|---|---|
| 1 | Heartbeats, détection, notification Discord, `/v1/status`, self-heartbeat | ✅ |
| 2 | Better Stack en écriture (create / update / resolve, mapping ressources) | ✅ |
| 3 | Bus Redis vers le bot + bascule bot ↔ webhook + signal sticky | ✅ côté monitor |
| 4 | Commandes staff (`/ingest/command` + pubsub) et file de rattrapage | ✅ côté monitor |
| 5 | Webhook Better Stack entrant, anti-boucle, poll de réconciliation | ✅ |

Reste à faire **côté bot Moddy** (autre dépôt) : listener Redis + ACK, sticky message
et bouton `Refresh` persistant, commandes `/status *` et Modals V2. Le monitor publie
déjà tout ce dont le bot a besoin (JSON Components V2 brut, signal `sticky.refresh`).

## Démarrer

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # renseigner au minimum HM_INGEST_TOKEN
uvicorn app.main:app --reload --port 8080
```

Tests :

```bash
pip install pytest pytest-asyncio
pytest
```

Docker :

```bash
docker build -t moddy-health-monitor . && docker run -p 8080:8080 --env-file .env moddy-health-monitor
```

Un seul worker uvicorn : l'état de détection vit dans le process, la persistance est
dans Redis. Deux workers doubleraient les boucles de check et donc les alertes.

## Endpoints

| Route | Auth | Rôle |
|---|---|---|
| `POST /ingest/heartbeat` | `X-Health-Token` | Heartbeat d'un service |
| `POST /ingest/command` | `X-Health-Token` | Commande staff (repli si Redis est down) |
| `POST /ingest/betterstack` | `?k=<secret>` | Webhook Better Stack (répond 202, traite en fond) |
| `GET /v1/status` | — | État complet, 60 req/min par IP, cache 30s |
| `GET /v1/status/banner` | — | Payload minimal pour la bannière du dashboard |
| `GET /health` | — | Liveness du monitor, 200 nu, sans dépendance |

### Heartbeat

```bash
curl -X POST localhost:8080/ingest/heartbeat \
  -H "X-Health-Token: $HM_INGEST_TOKEN" -H 'Content-Type: application/json' \
  -d '{"service":"moddy-bot","status":"ok","version":"1.4.2","uptime_s":84213,
       "checks":{"discord_gateway":{"ok":true,"latency_ms":78}},
       "meta":{"shards":"3/3","guilds":3742}}'
```

`status` vaut `ok` | `degraded` | `down` : **le service décide lui-même de son état**,
il connaît ses dépendances mieux que le monitor. `checks` est un dictionnaire à clés
libres — le monitor n'interprète jamais les noms de clés, il itère dessus pour
l'affichage. La réponse porte `incident_active`, qui permet à un service de dégrader
son propre comportement pendant une crise.

Côté service émetteur, [`examples/heartbeat_client.py`](examples/heartbeat_client.py)
fournit la task asyncio fire-and-forget prête à coller, avec le cas particulier du bot
(ne pas se déclarer `ok` si `bot.is_ready()` est faux).

## Détection

| État | Condition |
|---|---|
| `operational` | Heartbeat frais + `status: ok` |
| `degraded` | Heartbeat frais + `status: degraded` |
| `down` | Heartbeat frais + `status: down`, **ou** clé `hm:hb:{service}` expirée |
| `unknown` | Aucun heartbeat jamais reçu depuis le démarrage du monitor |

Anti faux-positifs : 90s de grace period au démarrage, 3 cycles consécutifs en échec
et au moins 60s de silence pour déclencher, 2 cycles OK pour résoudre, et une
notification Discord au maximum par service et par état toutes les 5 minutes.

Le seuil de silence ne s'applique qu'au silence réel : quand un service déclare
lui-même `down`, ses heartbeats continuent d'arriver — exiger 60s de silence
n'alerterait jamais. Un service jamais vu finit par compter comme `down`, sinon un
service qui n'a jamais démarré n'est que du silence que personne ne remarque.

Sévérité agrégée : `degraded` (Discord seul) → `partial_outage` (+ Better Stack) →
`major_outage` (+ notify subscribers). Le `degraded` ne crée pas d'incident **public** :
sinon la status page passe au rouge à chaque hoquet de Redis.

## Redondance

| Panne | Comportement |
|---|---|
| Redis down | État en mémoire, alertes maintenues, resync automatique au retour |
| Bot Discord down | Bascule webhook, c'est le scénario nominal de la redondance |
| Discord down | Better Stack seul, messages empilés dans `hm:notify:queue` et rejoués |
| Better Stack down | Discord seul, backoff exponentiel plafonné à 5 min |
| Monitor down | Heartbeat sortant vers Better Stack, qui alerte directement |

Chaque version d'un incident ne part qu'une fois, tous canaux confondus
(`hm:notify:sent`, clé = `hash(incident_id + update_index + canal)`) : un retour de
retry ne produit jamais deux messages.

## Better Stack

Le webhook entrant est le chemin principal, le poll d'`index.json` toutes les 5 min
n'est qu'un filet de sécurité — après 10 échecs de livraison, Better Stack désactive
la souscription silencieusement, et il faut la recréer à la main depuis la status page.

Anti-boucle : le monitor écrit dans Better Stack, qui lui renvoie ses propres écritures
par webhook. L'ID d'un report est enregistré dans `hm:bs:owned` **avant** tout autre
traitement, et l'ID de chaque update créé l'est dans `hm:bs:seen_updates` dès la
réponse `201`. La déduplication par ID couvre aussi les livraisons multiples dues aux
retries.

## Configuration

Toutes les variables sont dans [`.env.example`](.env.example). Le strict minimum pour
démarrer : `HM_INGEST_TOKEN`, `HM_SERVICES`, et `DISCORD_WEBHOOK_URL` pour être
réellement alerté.

## Arborescence

```
app/
├── main.py                 # FastAPI, CORS, rate limit, lancement des boucles
├── config.py               # Settings pydantic depuis l'env
├── context.py              # Câblage des composants
├── keys.py                 # Noms des clés Redis
├── state.py                # Store Redis + fallback mémoire
├── util.py
├── api/                    # ingest, webhooks, public, health
├── core/                   # detector, incident, notifier, scheduler
├── integrations/           # betterstack, discord_webhook, redis_bus
└── render/                 # components (Components V2), colors
```

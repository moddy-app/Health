# Architecture

## Vue d'ensemble

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

Trois flux entrants, trois flux sortants.

| Entrant | Source | Rôle |
|---|---|---|
| Heartbeats | Les services Moddy | Détection automatique |
| Commandes | Staff via le bot (pubsub Redis, repli HTTP) | Gestion de crise manuelle |
| Webhook / poll Better Stack | `status.moddy.app` | Récupérer les actions faites hors du monitor |

| Sortant | Cible | Rôle |
|---|---|---|
| Status reports | API Better Stack v2 | Page de statut publique |
| Components V2 | Discord (bot → webhook) | Communication utilisateurs |
| JSON | Dashboard, site, tiers | Bannière d'incident |
| Self-heartbeat | Better Stack | Surveiller le surveillant |

## Arborescence du code

```
app/
├── main.py            FastAPI, CORS, rate limit, lifespan
├── context.py         Câblage : un objet Context porte tous les composants
├── config.py          Settings pydantic, parsing des variables CSV
├── keys.py            Noms des clés Redis, en un seul endroit
├── state.py           Store Redis + miroir mémoire
├── util.py            Horodatage ISO-8601, âge, identifiant d'incident
│
├── api/
│   ├── ingest.py      POST /ingest/heartbeat
│   ├── webhooks.py    POST /ingest/betterstack
│   ├── public.py      GET /v1/status, /v1/status/banner
│   └── health.py      GET /health
│
├── core/
│   ├── detector.py    Machine à états, seuils, sévérité agrégée
│   ├── impact.py      Propagation d'impact entre services
│   ├── incident.py    Cycle de vie, commandes staff, retour Better Stack
│   ├── notifier.py    Chaîne de redondance Discord, file de rattrapage
│   ├── probe.py       Sonde HTTP des services sans process
│   └── scheduler.py   Les six boucles asyncio
│
├── integrations/
│   ├── betterstack.py Écriture v2, parsing index.json, anti-boucle
│   └── discord_webhook.py  Envoi/édition directs, repli embed
│
├── bot/
│   ├── client.py      HealthBot : intents, setup_hook, on_ready, on_message
│   ├── publisher.py   Publication/édition d'incident par le bot
│   ├── sticky.py      Boucle sticky : debounce, verrou, persistance de l'ID
│   ├── views.py       StickyStatusView, DetailView — vues persistantes
│   ├── modals.py      Modals V2 : create / update / resolve / maintenance
│   └── commands.py    Groupe /status, check staff
│
└── render/
    ├── model.py       IncidentPresentation — le modèle commun
    ├── theme.py       Couleurs, icônes, libellés
    ├── layout.py      Rendu -> discord.ui.LayoutView (chemin bot)
    ├── raw.py         Rendu -> JSON brut (chemin webhook)
    └── colors.py      Palette et vocabulaire des niveaux
```

Le paquet `bot/` non plus n'était pas dans la spec d'origine : elle plaçait ces
quatre responsabilités dans le bot Moddy, relié par un pubsub Redis. Le bot vit
maintenant dans ce process, sous sa propre application Discord — voir
[discord.md](discord.md#pourquoi-une-application-dédiée).

Trois modules du cœur ne figuraient pas non plus dans la spec :

- **`core/impact.py`** — la propagation d'impact, ajoutée après coup.
- **`core/notifier.py`** — la chaîne de redondance méritait son fichier plutôt
  que d'être diluée dans `incident.py`.
- **`core/probe.py`** — le dashboard n'a aucun process capable de pousser un
  heartbeat ; le monitor va donc chercher son URL. Voir
  [heartbeat.md](heartbeat.md#les-services-sans-process).

Trois autres sont des commodités : `context.py` (câblage), `keys.py` (noms de
clés), `util.py` (dates).

## Câblage

`context.py` construit un objet `Context` unique et le pose sur
`app.state.ctx`. Les routes le récupèrent par `Depends(get_ctx)`, les boucles le
reçoivent à la construction. Aucun singleton, aucune variable globale : les
tests instancient leur propre `Context`.

Un seul `httpx.AsyncClient` sert toutes les intégrations sortantes, avec
`follow_redirects=True` — `status.moddy.app/index.json` répond un 302 vers
`/en/index.json`, et sans cette option le poll échoue silencieusement.

## Les boucles

Toutes vivent dans `core/scheduler.py`, toutes sont enveloppées : **aucune
exception ne remonte jusqu'à la boucle principale**, chaque itération log et
continue.

| Boucle | Période | Rôle |
|---|---|---|
| `probe` | `HM_PROBE_INTERVAL` (30s) | Sonde HTTP des services sans process |
| `check` | `HM_CHECK_INTERVAL` (15s) | Cycle de détection, réconciliation, calcul de `/v1/status` |
| `notify-queue` | 30s | Vide la file de rattrapage Discord |
| `sticky` | `HM_STICKY_REFRESH_INTERVAL` (120s) | Rafraîchit le contenu du sticky |
| `self-heartbeat` | `HM_SELF_HEARTBEAT_INTERVAL` (60s) | Ping Better Stack |
| `bs-poll` | `BETTERSTACK_POLL_INTERVAL` (300s) | Réconciliation Better Stack |

`probe` ne démarre que si `HM_PROBE_MAP` est renseignée, les deux dernières que
si leur URL l'est. `probe` est lancée avant `check` : ses heartbeats synthétiques
doivent exister avant qu'on ne les relise.

La boucle `check` tente aussi une reconnexion Redis à chaque tour quand le store
est dégradé : le resynchronisation est automatique, il n'y a pas de boucle
dédiée.

## Un seul worker

`uvicorn --workers 1`, volontairement. L'état de détection vit dans le process
(`Detector.states`), la persistance est dans Redis. Deux workers feraient tourner
deux boucles de check indépendantes : deux fois les alertes, deux fois les
reports Better Stack, et une course sur `hm:incident:active`.

## Ordre d'un cycle de check

```
1. store.connect()          si dégradé, tentative de reconnexion + resync
2. detector.run_cycle()     lit les heartbeats, applique les seuils,
                            calcule les transitions, propage l'impact,
                            persiste hm:state:{service}
3. incidents.reconcile()    ouvre / met à jour / résout l'incident actif
4. detector.public_payload()
   store.set(hm:status:public, ttl=30)
5. si le niveau global a changé -> notifier.refresh_sticky()
```

L'endpoint public ne fait que servir l'étape 4. Le calcul n'a jamais lieu à la
requête : `/v1/status` tient la charge et reste disponible même si tout le reste
rame.

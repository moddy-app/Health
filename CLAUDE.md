# CLAUDE.md

Repère pour travailler sur ce dépôt. La documentation détaillée est dans
[`docs/`](docs/README.md) — ce fichier ne fait qu'orienter.

## Le projet

`moddy-health-monitor` : service de monitoring interne de l'écosystème Moddy.
FastAPI, headless, déployé sur Railway. Les services Moddy **poussent** leur état
toutes les 20s. Seuls ceux qui n'ont aucun process pour le faire — un dashboard
est un site statique — sont sondés en HTTP (`HM_PROBE_MAP`). Il détecte les
pannes, alerte sur Discord, publie sur la status page Better Stack, et sert un
JSON public au dashboard.

## Commandes

```bash
uvicorn app.main:app --reload --port 8080    # lancer (REDIS_URL vide = mémoire seule)
pytest                                        # 102 tests, ~3s
python -m pyflakes app examples tests         # lint
```

Toujours faire tourner `pytest` **et** `pyflakes` avant de commiter.

## Carte du code

```
app/
├── main.py        FastAPI, CORS, rate limit, lifespan
├── context.py     Câblage — un objet Context porte tout, posé sur app.state.ctx
├── config.py      Settings pydantic ; les listes sont du CSV, pas du JSON
├── keys.py        Noms des clés Redis — jamais de nom en dur ailleurs
├── state.py       Store Redis + miroir mémoire ; ne lève jamais
├── api/           ingest, webhooks, public, health
├── core/          detector, impact, incident, notifier, probe, scheduler
├── integrations/  betterstack, discord_webhook, redis_bus
└── render/        components (Components V2), colors
```

Point d'entrée de la logique : `core/scheduler.py::_check_step` — détection,
réconciliation, calcul de `/v1/status`, signal sticky.

## Invariants à ne pas casser

1. **Le monitor ne dépend de rien de ce qu'il surveille.** Pas de PostgreSQL, pas
   d'appel vers l'API Moddy, aucun import du code du bot. La sonde de
   `core/probe.py` ne fait exception qu'en apparence : elle `GET` une URL
   publique, comme un navigateur, et son échec n'écrit qu'un `down`.
2. **Aucune exception ne remonte jusqu'à une boucle.** `core/scheduler.py`
   enveloppe chaque itération : log et continue.
3. **Le store ne lève jamais.** Un appelant n'a pas à gérer l'absence de Redis.
4. **Une panne Redis n'empêche jamais une alerte de partir.**
5. **Un seul worker uvicorn.** L'état de détection vit dans le process ; deux
   workers doubleraient alertes et reports.
6. **Aucune logique par service.** `checks` est un dictionnaire à clés libres,
   jamais interprété. Ajouter un service = ajouter des variables d'environnement,
   pas du code.
7. **Une version d'incident ne part qu'une fois**, tous canaux Discord confondus.

## Pièges déjà rencontrés

- **Le silence de 60s ne vaut que pour le silence réel.** Un service qui se
  déclare `down` continue d'émettre : appliquer la règle littéralement
  n'alerterait jamais. Voir `Detector._silence_ok`.
- **La file de rattrapage se remet en tête** (`lpush`), pas en queue : l'ordre
  des messages d'un incident doit être préservé.
- **Le webhook ne peut pas éditer un message posté par le bot.** L'échec
  d'édition déclenche un repost, pas une perte.
- **`status.moddy.app/index.json` renvoie un 302** vers `/en/index.json` : le
  client HTTP doit suivre les redirections.
- **Better Stack : `ends_at` reste `null` même sur un report résolu**, et
  `report_type` peut valoir `automatic`.
- **L'ID d'un report va dans `hm:bs:owned` avant tout autre traitement**, sinon
  la course avec le webhook entrant est perdue et le monitor adopte ses propres
  écritures.
- **Le rate-limit se remet à zéro à la reprise d'un service**, sinon toute
  résolution ouvre un angle mort de 5 minutes.
- **Une sonde en échec écrit un heartbeat `down`**, elle ne se contente pas de ne
  rien écrire : sinon la détection attend l'expiration du TTL.

## Conventions

- Commentaires et docstrings **en français**. Textes publics — Discord, status
  page, messages d'incident — **en anglais** : la status page l'est.
- Un commentaire explique un *pourquoi* non évident ; le *quoi* est dans le code.
- Les doublures de test sont des objets simples, pas des mocks. Aucun test ne
  sort sur le réseau.
- Un test nomme le comportement attendu, pas la fonction appelée.

## Branche et livraison

Développer sur `claude/dashboard-monitoring-duap2w`. Ne pas ouvrir de pull
request sans demande explicite.

## Où chercher

| Sujet | Fichier |
|---|---|
| Vue d'ensemble, boucles, câblage | [docs/architecture.md](docs/architecture.md) |
| Contrat d'ingestion | [docs/heartbeat.md](docs/heartbeat.md) |
| Seuils, propagation d'impact, sévérité | [docs/detection.md](docs/detection.md) |
| Cycle de vie d'un incident | [docs/incidents.md](docs/incidents.md) |
| Rendu et redondance Discord | [docs/discord.md](docs/discord.md) |
| API v2, anti-boucle, poll | [docs/betterstack.md](docs/betterstack.md) |
| Référence HTTP | [docs/api.md](docs/api.md) |
| Variables d'environnement | [docs/configuration.md](docs/configuration.md) |
| Clés Redis, store | [docs/redis.md](docs/redis.md) |
| Déploiement, runbook | [docs/operations.md](docs/operations.md) |
| Setup, tests, conventions | [docs/development.md](docs/development.md) |
| Spec d'origine (figée) | [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) |

## Reste à faire

Côté **bot Moddy**, dans son propre dépôt : listener Redis + ACK, sticky message
et bouton `Refresh` persistant, commandes `/status *` et Modals V2. Le monitor
publie déjà tout ce dont le bot a besoin.

Côté monitor : `BetterStack.poll_index()` parse les `status_page_resource` dans
`snapshot.resources`, mais `reconcile_betterstack()` n'en fait rien — l'état des
monitors Better Stack n'alimente donc pas `/v1/status`. Sans conséquence pour le
dashboard, qui a sa propre sonde, mais l'écart reste ouvert pour les ressources
qui n'en ont pas.

L'envoi Components V2 par webhook n'a jamais été confronté au vrai
Discord — c'est le premier test à faire au déploiement, avec le repli embed en
filet.

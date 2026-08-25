# CLAUDE.md

Repère pour travailler sur ce dépôt. La documentation détaillée est dans
[`docs/`](docs/README.md) — ce fichier ne fait qu'orienter.

## Le projet

`moddy-health-monitor` : service de monitoring interne de l'écosystème Moddy.
FastAPI **et un bot Discord dédié dans le même process**, déployé sur Railway.
Les services Moddy **poussent** leur état toutes les 20s. Seuls ceux qui n'ont aucun process pour le faire — un dashboard
est un site statique — sont sondés en HTTP (`HM_PROBE_MAP`). Il détecte les
pannes, alerte sur Discord, publie sur la status page Better Stack, et sert un
JSON public au dashboard.

## Commandes

```bash
python -m app.main                            # lancer (API + bot ; REDIS_URL vide = mémoire seule)
uvicorn app.main:app --reload --port 8080     # API seule, sans bot
pytest                                        # 180 tests, ~4s
python -m pyflakes app examples tests         # lint
```

Toujours faire tourner `pytest` **et** `pyflakes` avant de commiter.

## Carte du code

```
app/
├── main.py        FastAPI, CORS, rate limit, lifespan, gather serveur + bot
├── context.py     Câblage — un objet Context porte tout, posé sur app.state.ctx
├── config.py      Settings pydantic ; les listes sont du CSV, pas du JSON
├── keys.py        Noms des clés Redis — jamais de nom en dur ailleurs
├── logs.py        Formatteur Railway (JSON une ligne, niveaux `warn`/`error`)
├── state.py       Store Redis + miroir mémoire ; ne lève jamais
├── api/           ingest, webhooks, public, health
├── core/          detector, impact, incident, notifier, probe, scheduler
├── bot/           client, publisher, sticky, views, modals, commands
├── integrations/  betterstack, discord_webhook
└── render/        model, theme, layout (bot), raw (webhook), colors
```

Point d'entrée de la logique : `core/scheduler.py::_check_step` — détection,
réconciliation, calcul de `/v1/status`, rafraîchissement du sticky.

## Invariants à ne pas casser

1. **Le monitor ne dépend de rien de ce qu'il surveille.** Pas de PostgreSQL, pas
   d'appel vers l'API Moddy, aucun import du code du bot Moddy — le bot de ce
   dépôt est une application Discord distincte, qui ne surveille rien. La sonde de
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
8. **Le transport est collant.** Un message posté par webhook n'est pas éditable
   par le bot, et inversement : `discord_transport` est stocké à côté de
   `discord_message_id` et respecté à chaque édition.
9. **Le bot ne bloque jamais le reste.** Gateway perdue, token invalide, salon
   injoignable : le monitor continue de détecter, d'alerter par webhook et de
   servir `/v1/status`. `bot/client.py::run` avale tout.
10. **Un seul modèle de rendu, deux renderers.** Toucher `render/layout.py` sans
    toucher `render/raw.py` fait échouer `test_render_parity.py` — c'est le but.
11. **Le jeu d'icônes est fermé**, et tout message du bot est en Components V2
    — jamais de texte nu, jamais d'embed hors repli webhook. Trois familles qui
    ne se mélangent pas : l'état (`check_circle`, `down`, `degraded`,
    `maintenance`), la ligne « Status: » (`OnGoing`, `Resolved`, et nulle part
    ailleurs), et les réponses éphémères en blanc (`check_circle_white`,
    `exclamation`, `spinner`). Voir `render/colors.py`.

## Pièges déjà rencontrés

- **Le silence de 60s ne vaut que pour le silence réel.** Un service qui se
  déclare `down` continue d'émettre : appliquer la règle littéralement
  n'alerterait jamais. Voir `Detector._silence_ok`.
- **La file de rattrapage se remet en tête** (`lpush`), pas en queue : l'ordre
  des messages d'un incident doit être préservé.
- **Le webhook ne peut pas éditer un message posté par le bot.** L'échec
  d'édition déclenche un repost, pas une perte. Réciproquement, tant que le
  webhook répond, le bot ne touche pas à un fil qui lui appartient.
- **Un modal ne peut pas en ouvrir un second.** Discord ne l'autorise qu'en
  réponse à une commande ou à un composant : la sévérité par service se demande
  dans un panneau éphémère, pas dans un deuxième modal. Voir `bot/severity.py`.
- **La fenêtre de maintenance se saisit en heure française**, pas en UTC — le
  staff ne pense pas en UTC. `parse_window` convertit avant stockage ;
  `starts_at`/`ends_at` restent en UTC partout ailleurs.
- **L'icône de maintenance ne sort jamais de `starts_at`..`ends_at`.** Une
  maintenance ouverte à l'avance, ou pas encore résolue après coup, ne doit pas
  la porter hors de sa fenêtre — et sans les deux bornes, impossible d'affirmer
  qu'on est « pendant » : elle reste alors masquée.
- **La bannière publique annonce une maintenance en avance, mais au bon
  temps.** `/status maintenance` rend l'incident actif dès sa création, même
  programmée pour plus tard : le message dit `will undergo` avant `starts_at`,
  `is/are undergoing` pendant, `underwent` après `ends_at` si elle traîne sans
  être résolue. Voir `_maintenance_phase` dans `api/public.py`.
- **`/status cancel` n'agit que sur une maintenance active.** `/status resolve`
  clôt n'importe quel incident : un `cancel` qui ferait pareil sur un incident
  ordinaire prêterait à confusion sur ce qui vient d'être clos.
- **`send_modal` ne s'annule pas.** `/status update` et `/status resolve`
  vérifient l'incident actif *avant* d'ouvrir le modal.
- **Une vue persistante sans `add_view` est morte au redéploiement**, et Railway
  redéploie souvent. `StickyStatusView` et `DetailView` sont réenregistrées
  dans `setup_hook`.
- **Le sticky sans debounce prend un rate limit**, sans verrou fait des
  doublons, et sans mémoire de ses propres IDs se repost sur lui-même en boucle
  — la gateway livre le `MESSAGE_CREATE` avant que `send()` n'ait rendu l'ID.
- **Sans `Manage Messages`, le sticky ne peut rien effacer.** Poster quand même
  remplit le salon ; l'échec doit être en `warn`, pas en `debug`, et le repost
  doit retomber sur une édition.
- **L'identité du sticky ne peut pas dépendre de Redis.** Sans lui, l'ID est
  perdu à chaque redéploiement et le salon accumule un orphelin par déploiement.
  Il se retrouve dans le salon, à son `custom_id`.
- **Un update d'incident ne part que si `state_fingerprint` a bougé.** Comparer
  le niveau ou `affected` ne suffit pas : l'un n'est jamais réécrit hors `auto`,
  l'autre ne distingue pas `degraded` de `down`.
- **Une mention dans un embed ne prévient personne**, et un message Components
  V2 ne peut pas porter de `content` : le repli webhook est le seul endroit où
  `content` est rempli. Voir `_embed_payload`.
- **Les émojis doivent appartenir à l'application** (application emojis), sinon
  le rendu casse dans le chemin webhook.
- **Le `custom_id` du bouton du sticky ne se renomme pas.** C'est à lui que le
  sticky se reconnaît dans le salon : le changer abandonne tous les stickys déjà
  postés. Le libellé, lui, est libre — il dit `Details` depuis qu'il ouvre le
  panneau de diagnostic, mais l'identifiant reste `hm:sticky:refresh`.
- **Un `RadioGroup` rend `value`, un `CheckboxGroup` rend `values`.** Se
  tromper de nom ne casse qu'au submit, après que le staff a tout tapé :
  `tests/test_bot_views.py` lit les quatre modals pour cette raison.
- **`max_values` d'un `CheckboxGroup` ne peut pas être une constante.** Discord
  exige au moins autant d'options que ce qu'on autorise à cocher et refuse le
  modal entier sinon (`options: Must be 10 or more in length`) : toutes les
  commandes `/status` tombaient en 400.
- **Un incident adopté depuis Better Stack ne connaît que des ressources.**
  Sans le chemin inverse de `HM_BS_RESOURCE_MAP`, son message part avec
  « Affected services: — » ; et le republier vers Better Stack boucle.
- **`tree.sync()` global met jusqu'à une heure** à se propager : sync guild.
- **`status.moddy.app/index.json` renvoie un 302** vers `/en/index.json` : le
  client HTTP doit suivre les redirections.
- **`https://moddy.app` (sans `www`) répond `307`** vers `https://www.moddy.app`
  — et la sonde ne suit jamais les redirections (§`core/probe.py`), à raison :
  un site sondé sur son domaine nu partait `down` en boucle, un vrai faux
  positif constaté en production. L'URL de `HM_PROBE_MAP` doit répondre `2xx`
  directement, jamais après un saut. Même piège côté navigateur : c'est
  `https://www.moddy.app` que le fetch envoie comme origine, pas le domaine
  nu — d'où `HM_CORS_ORIGIN_REGEX`, qui autorise tout `*.moddy.app` plutôt que
  d'énumérer chaque sous-domaine dans `HM_CORS_ORIGINS` et de prendre du
  retard à chaque nouveau module.
- **Les URL d'incident que le monitor construit n'ont pas de segment de
  langue.** `_url_for` génère `/incident/{id}`, pas `/en/incident/{id}` — la
  status page choisit elle-même sa langue à l'affichage.
- **La bannière publique ne nomme un service que si l'appelant est ce
  service.** `/v1/status/banner?service=<id>` : générique par défaut, le
  message ne cite le service en cause que si `service` figure dans les
  affectés — sinon un consommateur non concerné apprendrait quel service
  interne est en panne.
- **Better Stack : `ends_at` reste `null` même sur un report résolu**, et
  `report_type` peut valoir `automatic`.
- **L'ID d'un report va dans `hm:bs:owned` avant tout autre traitement**, sinon
  la course avec le webhook entrant est perdue et le monitor adopte ses propres
  écritures.
- **Éditer le texte d'une update Better Stack ne change pas son ID** : l'anti-
  boucle ne marque que les ID vus, donc la correction ne repasse jamais par le
  webhook. `/status reload` resynchronise à la main, et réédite Discord par
  `Notifier.re_render()` — `dispatch()` ne verrait rien de nouveau, l'anti-
  doublon ne comptant que le nombre d'updates, pas leur contenu.
- **`/status reload` rouvre aussi le dernier incident clos.** Prolonger une
  maintenance ou rouvrir un incident se fait à la main sur Better Stack, après
  que le monitor a déjà résolu et archivé le sien — sans ce chemin, la
  correction n'aurait nulle part où aller. Ne rouvre que s'il y a du neuf
  depuis la clôture (`len(new_updates) > len(updates)`), sinon rejouerait ce
  qui était déjà affiché.
- **Le rate-limit se remet à zéro à la reprise d'un service**, sinon toute
  résolution ouvre un angle mort de 5 minutes.
- **`index.json` porte tout l'historique.** Au premier poll, `hm:bs:seen_updates`
  est vide et chaque update d'archive passe pour neuf : sans amorçage, le
  monitor adopte un incident résolu il y a des mois et le rejoue. Voir
  `reconcile_betterstack`.
- **Un incident non-`auto` ne voit jamais son niveau réécrit.** Comparer le
  niveau *observé* au sien rouvre la garde de sortie de `reconcile` à chaque
  cycle : un update toutes les 15 secondes.
- **Railway n'accepte que quatre niveaux de log** — `debug`, `info`, `warn`,
  `error`. `WARNING` et `CRITICAL` doivent être traduits, sinon tout ressort en
  `info` et les avertissements se noient. Voir `app/logs.py`.
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

Développer sur `claude/discord-bot-health-monitor-z71zme`. Ne pas ouvrir de pull
request sans demande explicite.

## Où chercher

| Sujet | Fichier |
|---|---|
| Vue d'ensemble, boucles, câblage | [docs/architecture.md](docs/architecture.md) |
| Contrat d'ingestion | [docs/heartbeat.md](docs/heartbeat.md) |
| Seuils, propagation d'impact, sévérité | [docs/detection.md](docs/detection.md) |
| Cycle de vie d'un incident | [docs/incidents.md](docs/incidents.md) |
| Bot, rendu et redondance Discord | [docs/discord.md](docs/discord.md) |
| API v2, anti-boucle, poll | [docs/betterstack.md](docs/betterstack.md) |
| Référence HTTP | [docs/api.md](docs/api.md) |
| Variables d'environnement | [docs/configuration.md](docs/configuration.md) |
| Clés Redis, store | [docs/redis.md](docs/redis.md) |
| Déploiement, runbook | [docs/operations.md](docs/operations.md) |
| Setup, tests, conventions | [docs/development.md](docs/development.md) |
| Spec d'origine (figée) | [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) |

## Reste à faire

Le bot Discord est complet : publication, sticky, bouton `Details` persistant,
commandes `/status *` et Modals V2. Il n'y a plus rien à faire côté bot Moddy —
le pubsub Redis qui les reliait a été retiré.

`BetterStack.poll_index()` parse les `status_page_resource` dans
`snapshot.resources`, mais `reconcile_betterstack()` n'en fait rien — l'état des
monitors Better Stack n'alimente donc pas `/v1/status`. Sans conséquence pour le
dashboard, qui a sa propre sonde, mais l'écart reste ouvert pour les ressources
qui n'en ont pas.

Rien du chemin Discord n'a été confronté au vrai Discord : ni la gateway, ni les
Modals V2, ni l'envoi Components V2 par webhook. C'est le premier test à faire
au déploiement, avec le repli embed en filet.

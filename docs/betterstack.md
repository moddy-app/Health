# Better Stack

`app/integrations/betterstack.py`. Synchronisation bidirectionnelle avec la
status page publique.

> Ne pas confondre avec `/api/v3/incidents` : c'est l'API **Incident Management**
> (on-call, escalade), un objet différent qui n'alimente pas la status page. Pour
> une status page, c'est `status-reports` en v2.

## Écriture

Base `BETTERSTACK_API_BASE`, header `Authorization: Bearer $BETTERSTACK_TOKEN`.

| Action | Appel |
|---|---|
| Créer un report | `POST /status-pages/{spid}/status-reports` |
| Ajouter un update | `POST /status-pages/{spid}/status-reports/{rid}/status-updates` |
| Résoudre | un update avec `status: "resolved"` sur chaque ressource affectée |
| Modifier un report | `PATCH /status-pages/{spid}/status-reports/{rid}` |

**Il n'existe pas d'endpoint `/resolve`.**

### Créer un report

```json
{
  "title": "Major Outage – Bot & API Unavailable",
  "message": "We are currently experiencing a service outage...",
  "report_type": "manual",
  "notify_subscribers": false,
  "published_at": "2026-08-24T19:42:00Z",
  "affected_resources": [
    { "status_page_resource_id": "8720238", "status": "downtime" },
    { "status_page_resource_id": "8720241", "status": "degraded" }
  ]
}
```

| Champ | Notes |
|---|---|
| `report_type` | `manual` ou `maintenance` |
| `notify_subscribers` | Défaut `false`. Le monitor ne passe `true` que sur `major_outage`, ou si le staff l'a coché |
| `affected_resources[].status` | `resolved`, `degraded`, `downtime` — ou `maintenance` si `report_type: "maintenance"` |
| `published_at` | Renseigné avec `created_at` de l'incident : après une panne, la file de rattrapage republie avec l'horodatage d'origine |
| `ends_at` | Obligatoire **uniquement** si `report_type: "maintenance"` |

L'ID revient dans `data.id`. Il est enregistré dans `hm:bs:owned` **avant tout
autre traitement** — sinon la course avec le webhook entrant est perdue.

### Poster un update

`affected_resources` est **obligatoire sur les updates**, contrairement à la
création où il est optionnel. Chaque update re-déclare l'état de chaque ressource
concernée. Un update sans ressource est refusé côté monitor, avant l'appel.

L'ID de l'update est enregistré dans `hm:bs:seen_updates` dès la réponse `201`,
pour que l'écho webhook soit ignoré.

### Mapping des ressources

```env
HM_BS_RESOURCE_MAP=moddy-bot:8720238,moddy-website:8720239,moddy-dashboard:8720240,moddy-api:8720241
```

**Un service sans `resource_id` est purement ignoré** — mieux vaut ne rien
publier que salir la barre journalière d'un service voisin.

`resources_for()` traduit l'état effectif de chaque service en état Better
Stack : le service tombé est `downtime`, ceux dégradés par ricochet sont
`degraded`, les autres `resolved`.

C'est la réponse au constat de la spec (§15) : Website et Dashboard apparaissaient
`degraded` alors que leur `availability` valait 1.0. Désormais ils ne sont marqués
`degraded` que quand une dépendance réelle est tombée — voir
[detection.md](detection.md#propagation-dimpact). Une dégradation déclarée par le
modèle d'impact est une affirmation vraie, pas un report manuel approximatif.

### Backoff

5 tentatives, délais `2, 4, 8, 16, 32, 64, 128, 300` plafonnés à 5 minutes, sur
erreur réseau, 429 et 5xx. Une 4xx autre que 429 est définitive : log et abandon.

## Lecture

Deux mécanismes. **Le webhook est le chemin principal, le poll est le filet de
sécurité.**

### A. Webhook (principal)

```
POST /ingest/betterstack?k=<BETTERSTACK_WEBHOOK_SECRET>
```

Better Stack **ne signe pas** ses webhooks et ne supporte aucun header d'auth :
sa doc recommande explicitement l'authentification par URL. Le secret est
comparé en temps constant ; s'il n'est pas configuré, l'endpoint est ouvert.

Le monitor répond **202 immédiatement** et traite en tâche de fond — Better Stack
coupe à 30s.

Trois `event_type` : `incident`, `maintenance`, `component_update`. L'en-tête
`X-BetterUptime-Event` sert de valeur de repli quand le corps ne porte pas
`event_type`.

Points structurants du payload :

- `incident.id` **est** le `status_report_id` — la clé de jointure avec ce que le
  monitor a créé ;
- `incident_updates` arrive **du plus récent au plus ancien** ; le monitor les
  retourne pour traiter dans l'ordre chronologique ;
- `shortlink` donne l'URL publique : utilisée telle quelle pour le bouton
  Discord, plutôt que reconstruite.

**Mise en place manuelle, une seule fois :** `status.moddy.app` → « Get
updates » → type Webhook → URL → confirmation par email. Il n'y a pas d'API pour
créer la souscription.

**Risque à connaître :** retries 30s → 8min, jusqu'à 10 tentatives, puis
**désactivation silencieuse** de la souscription. Un monitor down plus d'une
quinzaine de minutes peut perdre son webhook sans que rien ne le signale. D'où
le poll, et d'où la surveillance décrite plus bas.

### B. Poll `index.json` (secours)

`GET $BETTERSTACK_INDEX_URL`, sans authentification, toutes les 5 minutes.

L'URL répond un **302 vers `/en/index.json`** : le client HTTP suit les
redirections, sans quoi le poll échoue silencieusement.

Parsing (`parse_index`) :

- `data.attributes.aggregate_state` → `operational` | `degraded` | `downtime` | `maintenance`
- `included[] type == "status_report"` → incidents
- `included[] type == "status_update"` → updates, reliés via `relationships.status_updates`
- `included[] type == "status_page_resource"` → état par ressource, nom nettoyé de ses espaces parasites

**Trois pièges, tous vérifiés contre la vraie page :**

1. **`ends_at` reste `null` même sur les reports résolus.** Un parseur naïf voit
   tous les incidents historiques comme ouverts en permanence. Le code ne le lit
   jamais.
2. **`report_type` peut valoir `automatic`** — incidents créés par les monitors
   Better Stack eux-mêmes. Troisième origine, distincte de `manual` et
   `maintenance`.
3. **`availability` (monitors) et `status_history` (reports) sont deux sources de
   vérité distinctes** et peuvent diverger. Elles ne sont jamais croisées.

États de ressource possibles : `operational`, `degraded`, `downtime`,
`maintenance`, `not_monitored`.

## Anti-boucle

Le monitor écrit dans Better Stack, qui lui renvoie ses propres écritures par
webhook. Sans garde-fou : boucle infinie.

`process_report()` est le chemin commun au webhook et au poll :

```python
owned = await bs.is_owned(report_id)
for update in updates:                    # du plus ancien au plus récent
    if await bs.update_seen(update_id):
        continue                          # écho de notre écriture, ou retry
    await bs.mark_update_seen(update_id)
    await (on_owned_update if owned else on_foreign_incident)(report, update)
```

Deux règles absolues :

- le `data.id` d'un report créé va dans `hm:bs:owned` **avant** tout autre
  traitement ;
- l'ID de chaque update créé va dans `hm:bs:seen_updates` **dès la réponse
  `201`**.

La déduplication par ID couvre aussi les livraisons multiples dues aux retries,
que la doc Better Stack signale comme possibles.

### L'amorçage du premier poll

`index.json` porte **tout l'historique** de la status page, pas seulement les
incidents en cours. Au premier poll — déploiement neuf, ou Redis vidé —
`hm:bs:seen_updates` est vide : chaque update d'archive passe alors pour
nouveau.

Constaté en production : le monitor a adopté un incident de facturation résolu
depuis des mois, en a rejoué les trois updates sur Discord, puis s'est mis à
l'enrichir à chaque cycle de détection.

Deux gardes, désormais :

1. **Amorçage.** Tant que `hm:bs:cursor` est absent, le poll marque tout comme
   vu sans rien traiter. Le monitor prend l'historique pour acquis et ne réagit
   qu'à ce qui arrive ensuite. Le prix : un incident Better Stack déjà ouvert au
   moment d'un redémarrage sans Redis n'est pas adopté. C'est le bon échange —
   manquer une adoption coûte moins cher que rejouer une archive.
2. **Âge.** Un report inconnu dont l'update le plus récent a plus de
   `HM_BS_ADOPT_MAX_AGE` (1h) n'est jamais adopté. `ends_at` restant `null` même
   sur un report résolu, l'âge du dernier mot est le seul indice fiable qu'un
   incident est clos.

Un update relayé vers Discord n'est **jamais** republié vers Better Stack
(`publish_betterstack=False`) : il y existe déjà, le republier serait la boucle.
Un incident **adopté** ne l'est pas davantage : il vient de là-bas, son message
d'ouverture y est déjà.

## Une update éditée ne repasse jamais par le webhook

L'anti-boucle ne marque que les **ID** vus (`hm:bs:seen_updates`) : un ID déjà
connu est ignoré, `owned` ou pas. Éditer le *texte* d'une update déjà postée
sur Better Stack ne change pas son ID — le webhook ne livre donc jamais cette
correction, et le message Discord reste figé sur l'ancien texte, silencieusement.

`/status reload` (`IncidentManager.sync_updates`) répare ça à la main :
relit `index.json` via `poll_index()`, retrouve le report de l'incident actif
par `bs_report_id`, et **remplace** entièrement `incident["updates"]` par ce
que dit Better Stack — la première update devient `created`, les suivantes
`updated`. La réédition Discord passe par `Notifier.re_render()`, pas par
`dispatch()` : l'anti-doublon (`_dedup_key`) ne compte que le *nombre*
d'updates, pas leur contenu — après une correction sans changement de compte,
`dispatch()` prendrait le message pour déjà à jour et n'éditerait rien.

### Rouvrir le dernier incident clos

Prolonger une maintenance, ou rouvrir un incident, se fait à la main sur
Better Stack — après que le monitor a déjà résolu et archivé le sien. Sans
incident actif, la correction n'aurait nulle part où aller. `/status reload`
regarde donc aussi `hm:incident:history` : si le dernier incident archivé a un
`bs_report_id` dont Better Stack montre plus d'updates qu'on n'en a gardé, il
redevient l'incident actif (`status: "updating"`, `resolved_at` effacé, retiré
de l'historique) et le même message Discord repasse en cours plutôt que
d'en repartir un nouveau.

Sans update supplémentaire depuis la clôture, rien ne rouvre — rejouer ce
qui était déjà affiché n'aurait aucun sens.

`ends_at` n'étant pas exposé par `index.json` (même piège que d'habitude),
`/status reload` ne resynchronise que le texte des updates — la fenêtre
affichée (`starts_at`..`ends_at`, qui borne l'icône de maintenance, §CLAUDE.md)
reste celle saisie à l'ouverture et n'est pas recalculée depuis Better Stack.

## Services affectés d'un incident adopté

Un incident ouvert à la main sur la status page ne connaît que des ressources
Better Stack. Sans traduction inverse, le message Discord annonçait
« Affected services: — ». `BetterStack.services_for()` fait le chemin retour de
`resources_for()` via `HM_BS_RESOURCE_MAP`, et le statut de chaque ressource
(`downtime`, `degraded`) donne au passage la sévérité de l'incident adopté. Une
ressource non mappée reste invisible côté monitor — comme à l'écriture.

## Surveiller le surveillant

### Heartbeat sortant

```
GET https://uptime.betterstack.com/api/v1/heartbeat/<token>
```

Toutes les 60s (`HM_SELF_HEARTBEAT_INTERVAL`). Suffixer `/fail` pour signaler un
échec explicite. Créer le monitor avec `POST /api/v2/heartbeats` (`name`,
`period`, `grace`) ; l'URL de ping est dans `data.attributes.url`.

Réglage conseillé : `period=120`, `grace=60`.

Sans ça, on aurait un système de surveillance dont la mort n'est constatée par
personne.

### Souscription webhook

`hm:bs:last_event_at` porte la date du dernier event reçu. Si l'écart dépasse
`HM_BS_WEBHOOK_SILENCE_ALERT` (24h), la boucle de poll log une erreur explicite :
la souscription a probablement été désactivée après 10 échecs de livraison et
doit être recréée à la main.

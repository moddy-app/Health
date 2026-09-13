# Cycle de vie d'un incident

`app/core/incident.py`. **Plusieurs incidents peuvent être actifs à la fois**,
dans `hm:incident:active` (un dictionnaire `{id: incident}`). Un nouveau
service qui tombe n'enrichit un incident existant que s'il lui est *relié* —
par le graphe d'impact (`HM_IMPACT_MAP`) pour la détection auto, par ses
`affected` déjà déclarés pour un incident manuel ou Better Stack. Un service
qui n'a rien à voir en ouvre un second, géré indépendamment : `/status update`
et `/status resolve` demandent alors lequel viser.

## Les trois origines

```
DÉTECTION (auto)          COMMANDE (staff)          BETTER STACK
      │                        │                          │
      └───────────┬────────────┴──────────────────────────┘
                  ▼
         ┌──────────────┐
         │  open        │──▶ create BS report ──▶ message Discord (nouveau)
         └──────┬───────┘
                │
         ┌──────▼───────┐
         │  updating    │──▶ post BS update  ──▶ message Discord (édité)
         └──────┬───────┘
                │
         ┌──────▼───────┐
         │  resolved    │──▶ update resolved ──▶ message Discord (édité, final)
         └──────┬───────┘
                │
                └──▶ hm:incident:history (trim 100), hm:incident:active supprimé
```

| `origin` | Déclencheur | Entrée |
|---|---|---|
| `auto` | `reconcile()` sur un snapshot | Boucle de check |
| `discord` | `/status *` du staff | Appel direct de `handle_command` depuis le bot |
| `betterstack` | Incident créé hors du monitor | Webhook ou poll `index.json` |

## Structure

```json
{
  "id": "inc_20260824_1942",
  "bs_report_id": "995593",
  "discord_message_id": "1409...",
  "discord_channel_id": "1398625686301704323",
  "discord_transport": "bot",
  "title": "Major Outage – Bot & API Unavailable",
  "message": "We are currently experiencing...",
  "type": "incident",
  "level": "major_outage",
  "origin": "auto",
  "affected": ["moddy-bot", "moddy-api", "moddy-website", "moddy-dashboard"],
  "status": "resolved",
  "created_by": "Moddy Health Monitor",
  "created_at": "2026-08-24T19:42:00Z",
  "resolved_at": "2026-08-24T20:15:00Z",
  "url": "https://status.moddy.app/incident/995593",
  "updates": [
    { "kind": "created",  "at": "...", "message": "...", "author": "Moddy Health Monitor" },
    { "kind": "updated",  "at": "...", "message": "...", "author": "Jules" },
    { "kind": "resolved", "at": "...", "message": "...", "author": "Jules" }
  ]
}
```

`type` : `incident` | `maintenance` | `degraded_performance`
`status` : `open` | `updating` | `resolved`

Trois champs ne figuraient pas dans la spec et sont nécessaires au
fonctionnement : `discord_transport` (savoir si le message est éditable par
webhook), `created_by` (l'auteur affiché) et `url` (le lien du bouton Discord,
issu du `shortlink` Better Stack quand il est disponible, reconstruit sinon).

## Réconciliation automatique

`reconcile(snapshot)`, appelé à chaque cycle : les causes racines qui tombent
(`snapshot.failing`) sont d'abord regroupées en **clusters** — les services
reliés entre eux par `HM_IMPACT_MAP` (directement, ou en chaîne). Chaque
cluster est traité indépendamment :

```
grace period               -> ne rien faire
maintenance active         -> ne rien faire (toute la détection auto, pas
                               seulement les services qu'elle couvre)
un incident auto par cluster dont plus aucune racine ne dure -> le résoudre
pour chaque cluster de causes racines encore en panne :
    aucun incident ne le couvre -> en ouvrir un, si le rate-limit le permet
    un incident le couvre déjà  -> le mettre à jour si l'état a changé,
                                    et si le rate-limit le permet
```

Un cluster « couvre » un incident auto si ses racines recoupent les `roots`
déjà suivis par cet incident ; il couvre un incident manuel ou Better Stack si
ses services recoupent ses `affected` déjà déclarés. Sans recoupement, un
nouveau cluster ouvre un **nouvel** incident — deux pannes sans rapport ne se
mélangent jamais, même actives en même temps.

Un incident ouvert à la main ou venu de Better Stack est **enrichi** par la
détection quand elle retombe sur ses propres services (sa liste `affected`
suit l'état réel) mais n'est jamais résolu automatiquement ni requalifié en
niveau : c'est un humain qui l'a ouvert, c'est un humain qui le ferme.

Le rate-limit porte sur les **causes racines** d'un cluster, pas sur les
services dégradés par ricochet : sinon un seul incident consommerait la fenêtre
de tous les services à la fois.

### Un update par changement réel, jamais un de plus

**La règle :** un incident ouvert ne reçoit un update que si l'état observé a
bougé — un service qui tombe, un service qui revient, une sévérité qui change.
Tant qu'il ne bouge pas, il n'y a rien de neuf à dire.

Elle est portée par une **signature de l'état**, stockée dans l'incident sous
`state_fingerprint` :

```
major_outage|moddy-api=down,moddy-bot=down,moddy-dashboard=down,...
```

`reconcile` la recalcule à chaque cycle et sort immédiatement si elle est
inchangée. Le fingerprint est réécrit **seulement** quand l'update part
réellement : un update bloqué ne doit pas faire oublier le changement.

Constaté en production sans elle : un incident adopté depuis Better Stack
prenait un update toutes les 15 secondes, sur Discord *et* sur la status page.
Deux comparaisons plus faibles avaient été essayées d'abord, et aucune ne
suffit :

- **Comparer le niveau** ne marche pas : celui d'un incident non-`auto` n'est
  jamais réécrit, donc l'observé en diffère en permanence et la garde ne se
  referme jamais.
- **Comparer `affected`** ne marche pas non plus : la liste ne distingue pas un
  service `degraded` d'un service `down`, et rate donc une aggravation.

Le rate-limit, lui, ne garde plus que l'**ouverture** d'un incident. L'étendre
aux updates suffisait à faire disparaître une reprise de service pendant cinq
minutes — l'inverse du but recherché.

En filet de sécurité, un update automatique identique au précédent n'est pas
publié (`dedupe=True`). Un membre du staff, lui, a le droit de répéter.

### Textes générés

| Niveau | Titre |
|---|---|
| `major_outage` | `Major Outage – {causes} Unavailable` |
| `partial_outage` | `Partial Outage – {causes} Unavailable` |
| `degraded` | `Degraded Performance – {causes}` |
| `maintenance` | `Scheduled Maintenance – {causes}` |

Le corps nomme la cause, puis les dégâts collatéraux :

> We are currently experiencing a service outage affecting API. Our team has
> been alerted and is investigating. Moddy Bot, Website & Dashboard may be
> degraded as a result.

Les textes publics sont en anglais : la status page l'est.

Le titre est figé à l'ouverture. Un incident qui s'aggrave voit son `level`
monter et un update s'ajouter, mais garde son titre — le renommer en cours de
route désoriente les lecteurs qui suivent le fil.

## `degraded` est public aussi

Un niveau `degraded` publie un report Better Stack comme n'importe quel autre
niveau — le cacher de la status page laisserait un vrai ralentissement sans la
moindre trace publique. Les seuils de détection (`HM_FAILURE_THRESHOLD`,
`HM_MIN_SILENCE`) et le rate-limit par service suffisent à filtrer le bruit ;
il n'y a pas besoin d'un filtre de plus ici.

Un service **franchement `down`**, lui, n'est jamais `degraded` : même
non-critique, il vaut `partial_outage` (§[detection.md](detection.md#sévérité-agrégée)).

## Résolution

1. `status: resolved`, `resolved_at` renseigné, update `kind: resolved` ajouté ;
2. status update Better Stack avec `status: "resolved"` sur chaque ressource
   affectée — il n'existe pas d'endpoint `/resolve` ;
3. dernière édition du message Discord (bandeau vert, emoji `verified2`) ;
4. `rpush` dans `hm:incident:history`, `ltrim` à 100, suppression de
   `hm:incident:active`.

L'ordre compte : la dernière édition Discord a lieu **avant** l'archivage, sinon
le message resterait figé sur son dernier état intermédiaire.

## Maintenances planifiées

`type: maintenance`, `report_type: "maintenance"` côté Better Stack, avec
`starts_at` et **`ends_at` obligatoire** — une maintenance sans `ends_at` est
refusée avant même l'appel API, Better Stack la rejetterait. Les ressources
affectées portent `status: "maintenance"`.

Couleur neutre (`#5865F2`), pas d'alerte urgente, mais bien présente dans
`/v1/status` sous la clé `maintenance` pour la bannière du dashboard.

### Une maintenance absorbe la détection, elle ne la subit pas

Tant que la maintenance est active et que `ends_at` n'est pas dépassé,
`reconcile()` ne touche à rien : ni update, ni résolution. Un service qui tombe
pendant sa propre maintenance est l'objet de l'opération, pas une panne à
annoncer — y empiler « We are currently experiencing a service outage » dit au
public le contraire de ce que le staff vient d'annoncer.

**Une fois `ends_at` passé, la maintenance est close automatiquement** (« The
scheduled maintenance window has ended. ») et le cycle suivant reprend la
détection normale. Sans ça, une maintenance oubliée rendrait le monitor aveugle
à la première vraie panne qui suit. Une maintenance sans `ends_at` — adoptée
depuis Better Stack — n'est jamais close d'office : elle reste au staff.

### Le type du report décide de l'état des ressources

`report_type: "maintenance"` n'accepte que `status: "maintenance"` sur ses
ressources affectées, et le refuse partout ailleurs. Le champ `bs_report_type`
retient donc le type du report **à sa création** et c'est lui, jamais le niveau
de l'incident, qui décide de l'état publié (`colors.bs_status_for`). Y compris
pour clore une maintenance : elle se termine par sa fenêtre, pas par un
`resolved` que l'API rejetterait.

## Commandes du staff

`IncidentManager.handle_command(action, payload)` :

| Action | Payload | Effet |
|---|---|---|
| `incident.create` | `title`, `message`, `level`, `affected`, `notify`, `author` | Ouvre un nouvel incident |
| `incident.update` | `incident_id`, `message`, `level?`, `affected?`, `notify`, `author` | Ajoute un update à cet incident |
| `incident.resolve` | `incident_id`, `message`, `notify`, `author` | Résout cet incident |
| `maintenance.create` | + `starts_at`, `ends_at` | Ouvre une maintenance |

`incident.create` ouvre **toujours** un incident distinct, même si d'autres
sont déjà actifs — plusieurs peuvent l'être à la fois, gérés indépendamment.
Enrichir un incident existant se fait via `/status update`, qui exige
`incident_id` : le bot ne le demande au staff (un `RadioGroup` dans le modal)
que s'il y a plus d'un incident actif à ce moment-là.

## Retour Better Stack

`handle_bs_payload()` (webhook) et `reconcile_betterstack()` (poll) partagent le
même traitement, décrit dans [betterstack.md](betterstack.md#anti-boucle).

- update sur un report que **nous** avons créé → relayé vers Discord, sans
  réécriture vers Better Stack (ce serait la boucle) ;
- report créé **hors** du monitor → adopté comme incident local d'origine
  `betterstack`, indépendant de tout autre incident déjà actif — plusieurs
  peuvent l'être à la fois.

## Historique

`hm:incident:history`, liste Redis, `ltrim` à 100 entrées. `history(limit)` les
renvoie du plus récent au plus ancien. Aucune route publique ne l'expose
aujourd'hui.

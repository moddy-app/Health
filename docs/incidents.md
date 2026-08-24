# Cycle de vie d'un incident

`app/core/incident.py`. **Un seul incident actif à la fois**, dans
`hm:incident:active`. Si un nouveau service tombe pendant un incident en cours,
on met à jour l'existant plutôt que d'en créer un second.

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
| `discord` | `/status *` du staff | Pubsub `moddy:hm:command` ou `POST /ingest/command` |
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
  "url": "https://status.moddy.app/en/incident/995593",
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

`reconcile(snapshot)`, appelé à chaque cycle :

```
grace period          -> ne rien faire
niveau operational    -> résoudre l'incident actif s'il est d'origine `auto`
aucun incident actif  -> en ouvrir un, si le rate-limit le permet
incident actif        -> le mettre à jour si `affected` ou `level` a changé
```

Un incident ouvert à la main ou venu de Better Stack est **enrichi** par la
détection (sa liste `affected` suit l'état réel) mais n'est jamais résolu
automatiquement ni requalifié en niveau : c'est un humain qui l'a ouvert, c'est
un humain qui le ferme.

Le rate-limit porte sur les **causes racines** (`snapshot.failing`), pas sur les
services dégradés par ricochet : sinon un seul incident consommerait la fenêtre
de tous les services à la fois.

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

## `degraded` n'est pas public

Un niveau `degraded` crée bien un incident local — message Discord, bannière
dashboard, `hm:incident:active` — mais **aucun status report Better Stack**.
C'est la lecture de « le `degraded` ne crée pas d'incident public » : public =
la status page. Sinon elle passerait au rouge à chaque hoquet de Redis.

Si l'incident s'aggrave ensuite en `partial_outage`, le report Better Stack est
créé à ce moment-là sur le même incident, et `bs_report_id` se remplit.

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

## Commandes du staff

`IncidentManager.handle_command(action, payload)` :

| Action | Payload | Effet |
|---|---|---|
| `incident.create` | `title`, `message`, `level`, `affected`, `notify`, `author` | Ouvre, ou enrichit l'incident actif |
| `incident.update` | `message`, `level?`, `affected?`, `notify`, `author` | Ajoute un update |
| `incident.resolve` | `message`, `notify`, `author` | Résout |
| `maintenance.create` | + `starts_at`, `ends_at` | Ouvre une maintenance |

Une commande `incident.create` alors qu'un incident est déjà actif **enrichit**
l'existant : la règle « un seul incident à la fois » ne souffre pas d'exception,
même manuelle.

## Retour Better Stack

`handle_bs_payload()` (webhook) et `reconcile_betterstack()` (poll) partagent le
même traitement, décrit dans [betterstack.md](betterstack.md#anti-boucle).

- update sur un report que **nous** avons créé → relayé vers Discord, sans
  réécriture vers Better Stack (ce serait la boucle) ;
- report créé **hors** du monitor → adopté comme incident local d'origine
  `betterstack`, sauf si un incident est déjà actif.

## Historique

`hm:incident:history`, liste Redis, `ltrim` à 100 entrées. `history(limit)` les
renvoie du plus récent au plus ancien. Aucune route publique ne l'expose
aujourd'hui.

# Détection

Le cœur du service : `app/core/detector.py` et `app/core/impact.py`.

## Liste des services attendus

```env
HM_SERVICES=moddy-bot,moddy-api,moddy-altguard,moddy-feeds
```

La liste est **exhaustive et obligatoire**. Sans elle, un service qui n'a jamais
démarré n'est que du silence, et personne ne le remarque.

## Machine à états

| État | Condition |
|---|---|
| `operational` | Heartbeat frais + `status: ok` |
| `degraded` | Heartbeat frais + `status: degraded` |
| `down` | Heartbeat frais + `status: down`, **ou** clé `hm:hb:{service}` expirée |
| `unknown` | Aucun heartbeat jamais reçu depuis le démarrage du monitor |

L'état est recalculé à chaque cycle (`HM_CHECK_INTERVAL`, 15s) et persisté dans
`hm:state:{service}`, ce qui permet à un redéploiement de ne pas repartir de
zéro.

## Anti faux-positifs

| Garde-fou | Valeur | Variable |
|---|---|---|
| Grace period au démarrage | 90s | `HM_STARTUP_GRACE` |
| Seuil de déclenchement | 3 cycles consécutifs en échec | `HM_FAILURE_THRESHOLD` |
| Silence minimum | 60s | `HM_MIN_SILENCE` |
| Seuil de résolution | 2 cycles consécutifs OK | `HM_RECOVERY_THRESHOLD` |
| Rate-limit notification | 1 par service, par état, par 5 min | `HM_NOTIFY_RATE_LIMIT` |

### La grace period

Pendant 90 secondes après le démarrage du monitor, `Snapshot.in_grace` vaut
`true` et `IncidentManager.reconcile()` sort immédiatement. Les états continuent
d'être calculés et persistés, mais aucune alerte ne part. Sans ça, chaque
redéploiement du monitor déclencherait une alerte par service.

### Le silence minimum ne s'applique qu'au silence réel

C'est la seule divergence assumée avec la lettre de la spec, qui demandait
« 3 cycles consécutifs en échec **ET** minimum 60s de silence ».

Quand un service déclare lui-même `down` ou `degraded`, ses heartbeats
**continuent d'arriver** : il n'y a jamais 60 secondes de silence, et la règle
appliquée littéralement n'alerterait jamais. `Detector._silence_ok()` distingue
donc les deux cas :

- heartbeat présent (le service se déclare en panne) → seul le seuil de cycles compte ;
- heartbeat absent (clé expirée) → il faut en plus 60s depuis le dernier signal reçu ;
- jamais aucun heartbeat → les 60s sont comptées depuis le démarrage du monitor.

### Un service jamais vu finit par tomber

Un service en `unknown` accumule ses cycles d'échec comme les autres et bascule
en `down` une fois les seuils atteints. C'est tout l'intérêt de `HM_SERVICES` :
sans ce comportement, un service qui n'a jamais démarré resterait invisible.

`unknown` n'entre pas dans le calcul de la sévérité agrégée tant que les seuils
ne sont pas atteints.

### Le rate-limit se remet à zéro à la reprise

`hm:notify:rl:{service}:{status}`, TTL 5 min. Un service qui repasse
`operational` voit ses clés effacées (`Notifier.reset()`), appelé depuis
`reconcile()` sur chaque transition vers `operational`.

Sans ce reset, toute résolution ouvrirait un angle mort : le service resterait
muet jusqu'à la fin de sa fenêtre, même en retombant pour de bon juste après.
Les rechutes rapprochées sont déjà absorbées par les seuils de détection, ce
n'est pas le rôle de ce compteur.

## Propagation d'impact

Un service qui tombe n'affecte pas que lui-même. `app/core/impact.py` traduit
les dépendances réelles du produit.

```env
HM_IMPACT_MAP=moddy-bot>*;moddy-api>moddy-website,moddy-dashboard,moddy-bot
```

Syntaxe : `source>cible1,cible2`, entrées séparées par `;`, `*` valant « tous les
autres services connus ».

### Les règles en vigueur

| Ce qui tombe | Conséquence |
|---|---|
| **Bot** | Tous les autres services passent `degraded` — le bot *est* le produit |
| **API / backend** | Website, Dashboard et Bot passent `degraded` |
| **Dashboard** | Aucun impact sur les autres |
| **AltGuard, Feeds** | Aucun impact sur les trois gros |

### Les deux garde-fous

- **Seul un service `down` propage.** Un `degraded` ne dégrade personne : sinon
  un hoquet du bot repeindrait toute la status page.
- **La propagation ne produit que du `degraded`.** Elle n'invente jamais un
  `down` et n'écrase jamais un `down` observé.

Ces deux règles suffisent à interdire les cascades : l'état dérivé plafonne à
`degraded`, et seul `down` propage. Un seul saut, par construction — il n'y a pas
de propagation transitive à craindre.

Cas particulier : un service **surveillé** dont on n'a aucune donnée (`unknown`)
n'est jamais marqué `degraded` par ricochet. Le déclarer dégradé serait une
affirmation qu'on ne peut pas étayer. Les services **non surveillés** (Website,
Dashboard : aucun heartbeat attendu) partent au contraire d'`unknown` et n'en
sortent que par l'impact — c'est exactement leur cas d'usage.

### Services connus, services surveillés

Deux ensembles distincts :

- `HM_SERVICES` — ceux qui poussent un heartbeat, listés dans `/v1/status`.
- `Settings.known_services` — l'union de `HM_SERVICES`, des clés de
  `HM_BS_RESOURCE_MAP` et des noms cités dans `HM_IMPACT_MAP`.

Website et Dashboard n'appartiennent qu'au second : ils n'ont pas de heartbeat,
mais existent comme ressources Better Stack et peuvent être dégradés par
ricochet. Ils apparaissent donc dans `affected` d'un incident et sur la status
page, **pas** dans `services[]` de `/v1/status`.

### Ce que le public voit

`/v1/status` expose les deux lectures, jamais l'une au détriment de l'autre :

```json
{ "id": "moddy-api", "status": "degraded", "reported": "operational",
  "impacted_by": ["moddy-bot"], "since": "..." }
```

`status` est l'expérience utilisateur, `reported` la santé technique déclarée par
le service, `impacted_by` la raison de l'écart.

## Sévérité agrégée

| Niveau | Condition | Action |
|---|---|---|
| `operational` | Tout OK | Rien |
| `degraded` | ≥1 `degraded`, ou un service non critique `down` | Discord seulement |
| `partial_outage` | ≥1 service critique `down`, mais pas tous | Discord + Better Stack |
| `major_outage` | **Tous** les services critiques `down` | Discord + Better Stack + notify subscribers |

Services critiques : `HM_CRITICAL_SERVICES`, par défaut `moddy-bot,moddy-api`.
La spec écrivait « Bot **et** API down » ; le code généralise à « tous les
services critiques », ce qui redonne le même résultat avec la configuration par
défaut tout en restant correct si la liste change.

La sévérité est calculée sur les états **observés**, pas sur les états propagés :
« un service critique down » doit rester une affirmation exacte. Le résultat
serait de toute façon identique, la propagation ne produisant que du `degraded`
et jamais sans qu'un `down` l'ait déclenchée.

Le `degraded` ne crée **pas** d'incident sur la status page — voir
[incidents.md](incidents.md#degraded-nest-pas-public).

## Le Snapshot

`Detector.run_cycle()` renvoie un `Snapshot`, l'objet que consomme tout le reste.

| Attribut | Contenu |
|---|---|
| `level` | Sévérité agrégée |
| `services` | `ServiceState` par service surveillé |
| `effective` | État après propagation, sur tous les services connus |
| `impacted_by` | Qui a dégradé qui |
| `transitions` | `(service, avant, après)` de ce cycle |
| `in_grace` | Grace period en cours |
| `failing` | Causes racines : ce que les heartbeats déclarent |
| `affected` | Causes racines **et** dégradés par ricochet |
| `collateral` | Uniquement les dégradés par ricochet |

`failing` alimente le titre de l'incident et le rate-limit ; `affected` alimente
la liste publiée et les ressources Better Stack. Un incident s'intitule donc
« Partial Outage – API Unavailable » et non « Partial Outage – API, Website,
Dashboard & Moddy Bot Unavailable » : la cause dans le titre, les dégâts dans le
corps du message.

# Contrat de heartbeat

Le monitor ne va jamais chercher l'état d'un service. Les services poussent, et
l'absence de signal *est* le signal (dead man's switch).

## Endpoint

```
POST /ingest/heartbeat
X-Health-Token: <HM_INGEST_TOKEN>
Content-Type: application/json
```

Le token est comparé en temps constant (`secrets.compare_digest`). S'il n'est
pas configuré côté monitor, l'endpoint répond **503**, pas 200 : un token vide
est une erreur de déploiement, pas un mode ouvert.

## Corps

```json
{
  "service": "moddy-bot",
  "status": "ok",
  "version": "1.4.2",
  "uptime_s": 84213,
  "checks": {
    "discord_gateway": { "ok": true, "latency_ms": 78 },
    "postgres":        { "ok": true, "latency_ms": 3 },
    "redis":           { "ok": true, "latency_ms": 1 }
  },
  "meta": { "shards": "3/3", "guilds": 3742 }
}
```

| Champ | Requis | Notes |
|---|---|---|
| `service` | oui | Un service absent de `HM_SERVICES` est accepté mais log un warning |
| `status` | non, défaut `ok` | `ok` \| `degraded` \| `down` |
| `version` | non | Affiché tel quel |
| `uptime_s` | non | Affiché tel quel |
| `checks` | non | Dictionnaire à clés **libres** |
| `meta` | non | Libre, affiché tel quel |

**Le service décide lui-même de son état.** Il connaît ses dépendances mieux que
le monitor, qui n'a aucune logique par service. Une valeur `status` inconnue est
traitée comme `down` — mieux vaut une fausse alerte qu'un silence.

**Le monitor n'interprète jamais les noms de clés de `checks`.** Il les stocke et
itère dessus pour l'affichage. Ajouter un check dans un service ne demande aucun
changement ici.

## Réponse

```json
{ "ok": true, "received_at": "2026-08-24T19:42:11Z", "incident_active": false }
```

`incident_active` permet au service de dégrader son propre comportement pendant
une crise — couper les notifications non critiques, par exemple. Il vaut `true`
dès qu'un incident non résolu existe, quelle que soit son origine.

## Stockage

Le corps est stocké tel quel sous `hm:hb:{service}`, augmenté de `received_at`,
avec un TTL de `HM_HEARTBEAT_TTL` (60s par défaut, soit trois fois l'intervalle
recommandé). L'expiration de cette clé *est* la détection de panne.

## Côté service émetteur

Une task asyncio isolée, **fire-and-forget**, timeout 5s, jamais bloquante : un
échec ne fait que logger. Le service ne doit jamais tomber parce que le monitor
est indisponible.

`examples/heartbeat_client.py` fournit l'implémentation prête à coller :

```python
from examples.heartbeat_client import HeartbeatClient

hb = HeartbeatClient("moddy-api", url=HM_URL, token=TOKEN,
                     version=__version__, build=build_checks)
hb.start()
...
await hb.stop()
```

`build` est une coroutine qui renvoie `{"status": ..., "checks": {...},
"meta": {...}}`. Le client y ajoute `service`, `version` et `uptime_s`.

Intervalle : **20 secondes**. Trois heartbeats manqués font expirer la clé.

### Cas particulier du bot

Un event loop vivant dont la connexion gateway est morte ne doit pas se déclarer
`ok`. `build_bot_checks()` applique la règle :

```python
"status": "ok" if ready and connected == total else ("degraded" if ready else "down")
```

et remonte `is_ready`, la latence gateway et le ratio de shards connectés dans
`checks`. Un bot prêt mais avec des shards manquants se déclare `degraded`, pas
`ok`.

## Les services sans process

Un dashboard est un site statique : rien n'y tourne qui puisse pousser un
heartbeat toutes les 20s. Le modèle push ne s'y applique pas — c'est structurel,
pas un oubli d'implémentation.

Pour ces services, `app/core/probe.py` fait le chemin inverse : le monitor
interroge une URL publique et écrit lui-même le heartbeat.

```env
HM_PROBE_MAP=moddy-dashboard:https://dashboard.moddy.app/healthz
HM_PROBE_INTERVAL=30
```

Le contrat de l'endpoint sondé tient en une ligne : **un 2xx signifie vivant.**
Le corps de la réponse n'est jamais lu, tout le reste (3xx compris) vaut `down`,
et une URL injoignable écrit un heartbeat `down` plutôt que de laisser la clé
expirer en silence.

Le résultat est un heartbeat comme un autre :

```json
{
  "service": "moddy-dashboard",
  "status": "ok",
  "checks": { "http": { "ok": true, "status_code": 200, "latency_ms": 63, "error": null } },
  "meta": { "source": "probe", "url": "https://dashboard.moddy.app/healthz" }
}
```

Il part dans `hm:hb:{service}` comme un heartbeat poussé, avec un TTL de trois
sondes. **La détection ne sait pas — et n'a pas à savoir — d'où vient
l'information** : mêmes seuils, mêmes transitions, mêmes alertes. `meta.source`
n'est là que pour l'humain qui lit la clé.

Un service cité dans `HM_PROBE_MAP` est surveillé sans qu'on ait à le déclarer
aussi dans `HM_SERVICES` : `Settings.services` fait l'union des deux.

### Et l'invariant « le monitor ne va chercher personne » ?

Il tient. Ce qu'il interdit, c'est de *dépendre* de ce qu'on surveille : pas de
PostgreSQL partagé, pas d'appel à l'API interne du bot. Ici on interroge une URL
publique comme le ferait n'importe quel navigateur, et l'échec de la sonde ne
fait rien d'autre qu'écrire `down`. C'est exactement ce que fait déjà un monitor
Better Stack de son côté — à ceci près que le résultat alimente aussi
`/v1/status`.

Un état propre l'emporte toujours sur un état déduit : si la sonde du dashboard
échoue alors que l'API va bien, le dashboard est `down` **de son propre chef**,
et son `impacted_by` reste vide.

## Vérifier à la main

```bash
curl -X POST localhost:8080/ingest/heartbeat \
  -H "X-Health-Token: $HM_INGEST_TOKEN" -H 'Content-Type: application/json' \
  -d '{"service":"moddy-bot","status":"ok","checks":{"redis":{"ok":true}}}'
```

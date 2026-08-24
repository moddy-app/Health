# Modèle de données Redis

Aucune base relationnelle. Tout tient dans Redis, avec un miroir mémoire. Les
noms de clés sont centralisés dans `app/keys.py` — ne jamais écrire un nom de clé
en dur ailleurs.

## Les clés

| Clé | Type | TTL | Contenu |
|---|---|---|---|
| `hm:hb:{service}` | string (JSON) | `HM_HEARTBEAT_TTL` (60s) | Dernier heartbeat reçu, augmenté de `received_at` |
| `hm:state:{service}` | string (JSON) | — | État calculé : `status`, `since`, compteurs, dernier heartbeat |
| `hm:incident:active` | string (JSON) | — | Incident en cours, un seul à la fois |
| `hm:incident:history` | list | trim 100 | Incidents clos, du plus ancien au plus récent |
| `hm:bs:owned` | set | — | IDs des status_report créés par le monitor |
| `hm:bs:seen_updates` | set | — | IDs des status_update déjà traités |
| `hm:bs:cursor` | string | — | Horodatage du dernier poll réussi |
| `hm:bs:last_event_at` | string | — | Dernier event Better Stack reçu (surveillance de la souscription) |
| `hm:status:public` | string (JSON) | `HM_PUBLIC_CACHE_TTL` (30s) | Réponse de `/v1/status` pré-calculée |
| `hm:sticky:message_id` | string | — | ID du sticky Discord (écrit par le bot) |
| `hm:notify:sent` | set | — | Anti-doublon : `sha1(incident_id + nb_updates + canal)` |
| `hm:notify:queue` | list | — | File de rattrapage, plafonnée à 200 |
| `hm:notify:rl:{service}:{status}` | string | `HM_NOTIFY_RATE_LIMIT` (300s) | Rate-limit par service et par état |
| `hm:ratelimit:{ip}` | string | fenêtre du rate limit | Compteur de l'API publique |

Trois clés ne figuraient pas dans la spec : `hm:bs:last_event_at`,
`hm:notify:queue` (nommée mais non tabulée) et `hm:notify:rl:*`.

## Canaux pubsub

| Canal | Sens | Contenu |
|---|---|---|
| `moddy:hm:notify` | monitor → bot | `{nonce, action, payload}` — `incident.post`, `incident.edit`, `sticky.refresh` |
| `moddy:hm:notify:ack` | bot → monitor | `{nonce, message_id}` |
| `moddy:hm:command` | bot → monitor | `{action, payload}` — commandes du staff |

## Le store

`app/state.py`. Une façade unique sur Redis et la mémoire. **Aucune méthode ne
lève** : un échec Redis bascule silencieusement — avec un warning — sur la
mémoire.

### Le miroir

Chaque écriture est systématiquement dupliquée en mémoire, que Redis soit
disponible ou non. Le volume de données est minuscule (quelques dizaines de
clés) et cela rend le fallback instantané : aucune reconstruction, la lecture
mémoire est toujours à jour.

### Le resync

Les clés écrites pendant une coupure sont notées dans un ensemble `_dirty`. À la
reconnexion — tentée à chaque cycle de check, avec un backoff de 5s —
`_resync()` les rejoue vers Redis : `SET` avec le TTL restant, `SADD` des
membres, `DEL` + `RPUSH` pour les listes. Une clé qui échoue pendant le resync
retourne dans `_dirty` et la connexion est marquée perdue.

### Ce que le fallback ne couvre pas

- **Le pubsub.** `publish()` renvoie `False` quand Redis est down, ce qui fait
  basculer le notifier sur le webhook. Le bot ne reçoit rien pendant la coupure.
- **La persistance entre process.** Un redéploiement pendant une panne Redis
  perd l'état en mémoire. C'est le rôle de `flush()` à l'arrêt, qui tente une
  dernière reconnexion et un resync avant de sortir.

### Opérations disponibles

`get`, `set`, `delete`, `get_json`, `set_json`, `sadd`, `sismember`, `smembers`,
`lpush`, `rpush`, `lpop`, `lrange`, `ltrim`, `llen`, `incr_window`, `claim`,
`publish`, `pubsub`, `flush`.

`claim(key, ttl)` est un `SET NX` : renvoie `True` si la clé vient d'être posée.
Sert au rate-limit des notifications. `incr_window(key, ttl)` incrémente et pose
le TTL au premier hit — le rate limit de l'API publique.

## Inspecter en production

```bash
redis-cli -u "$REDIS_URL" --scan --pattern 'hm:*'
redis-cli -u "$REDIS_URL" get hm:incident:active | python -m json.tool
redis-cli -u "$REDIS_URL" get hm:status:public   | python -m json.tool
redis-cli -u "$REDIS_URL" ttl hm:hb:moddy-bot
redis-cli -u "$REDIS_URL" lrange hm:notify:queue 0 -1
redis-cli -u "$REDIS_URL" scard hm:bs:seen_updates
```

Une file `hm:notify:queue` qui ne se vide pas signale que ni le bot ni le webhook
ne répondent. Un `ttl hm:hb:{service}` à `-2` signifie que la clé a expiré : le
service n'émet plus.

## Nettoyage

`hm:bs:owned` et `hm:bs:seen_updates` grossissent indéfiniment — un identifiant
par report et par update, soit quelques dizaines par an. Aucun nettoyage n'est
prévu : les purger reviendrait à rouvrir la porte aux boucles sur d'anciens
incidents.

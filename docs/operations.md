# Exploitation

## Déploiement Railway

Le `Dockerfile` suffit : Python 3.12 slim, `requirements.txt`, `uvicorn` sur
`$PORT`, **un seul worker**.

```
Healthcheck path : /health
Healthcheck timeout : 10s
```

`/health` ne touche à aucune dépendance : il répond même quand Redis, Discord et
Better Stack sont tous injoignables. C'est voulu — un healthcheck qui teste les
dépendances redémarre le service au pire moment.

### Avant le premier démarrage

1. Renseigner `HM_INGEST_TOKEN` et le propager aux services émetteurs.
2. Attacher un Redis (`REDIS_URL` en `redis.railway.internal`).
3. Créer l'application Discord dédiée « Moddy Health Monitor », l'inviter dans
   la guild, renseigner `DISCORD_TOKEN`, `DISCORD_GUILD_ID`,
   `DISCORD_STATUS_CHANNEL_ID` et `DISCORD_STAFF_ROLE_ID`.
4. Uploader les trois icônes en **application emojis** sur cette application
   (portail développeur). Sans ça, le rendu casse côté webhook — un émoji
   appartenant à un serveur que l'app ne connaît pas s'affiche cassé.
5. Créer le webhook **à la main** dans les paramètres du salon (surtout pas
   depuis l'application) et renseigner `DISCORD_WEBHOOK_URL` — **puis tester
   l'envoi Components V2**, c'est le maillon critique de la redondance et le
   seul non validé automatiquement.
6. Créer le Heartbeat monitor Better Stack, renseigner `HM_SELF_HEARTBEAT_URL`.
7. Créer la souscription webhook depuis `status.moddy.app` (manuel, avec
   confirmation par email).

Les permissions Discord nécessaires dans le salon de statut : `View Channel`,
`Send Messages`, `Embed Links`, `Manage Messages` (supprimer l'ancien sticky) et
`Read Message History` (le retrouver après un redéploiement).

### À corriger sur la status page

Constats relevés sur `index.json`, toujours valables au dernier poll :

- **`Moddy Bot` est en `not_monitored`** alors que l'`aggregate_state` de la page
  est `operational`. La page affiche publiquement que tout va bien pendant que la
  ressource principale n'est plus surveillée.
- **Timezone `America/Adak`** alors que les updates sont publiés en heure de
  Paris : les barres journalières sont décalées d'une dizaine d'heures.
- **AltGuard et Feeds n'existent pas** comme ressources. Les créer avant de
  compléter `HM_BS_RESOURCE_MAP`, sinon leurs pannes ne seront jamais publiées
  (le monitor ignore silencieusement un service non mappé).
- **Nommage** : espaces en fin de `"Moddy Bot "`, l'API rangée dans la section
  « Moddy Website », `"Internal API"` comme nom public.

## Modes dégradés

| Panne | Comportement | Ce qu'on perd |
|---|---|---|
| Redis down | État en mémoire, alertes maintenues, resync au retour | Le pubsub vers le bot, la persistance entre redéploiements |
| Bot Discord down | Bascule webhook automatique | L'édition du message existant, le sticky |
| Discord entièrement down | Better Stack seul, messages empilés dans `hm:notify:queue` | Rien définitivement : la file est rejouée |
| Better Stack down | Discord seul, backoff jusqu'à 5 min | La status page publique, temporairement |
| Monitor down | Le heartbeat sortant cesse, Better Stack alerte | Toute la détection |
| Railway down | Tout est down | Tout |

Le seul scénario sans filet est « Railway down » : le heartbeat sortant vers
Better Stack le rend au moins visible.

## Runbook

### Aucune alerte n'est partie alors qu'un service est tombé

1. Le service est-il dans `HM_SERVICES` ? Sinon il n'est pas surveillé.
2. `redis-cli ttl hm:hb:{service}` — si la clé vit encore, le service émet
   toujours et se déclare `ok`.
3. Le monitor était-il dans sa grace period (90s après un redéploiement) ?
4. `redis-cli get hm:notify:rl:{service}:down` — le rate-limit peut différer
   l'alerte de 5 minutes maximum.
5. `redis-cli lrange hm:notify:queue 0 -1` — l'alerte a-t-elle été produite mais
   jamais livrée ?
6. Chercher `aucun canal Discord disponible` dans les logs.

### Le message Discord ne s'édite plus

Le message a probablement été posté par le bot, qui est ensuite tombé : un
webhook ne peut pas éditer le message d'un autre auteur. Le monitor poste alors
un **nouveau** message. Vérifier `discord_transport` dans
`hm:incident:active`.

### La status page ne reflète pas l'incident

1. `BETTERSTACK_TOKEN` et `BETTERSTACK_STATUS_PAGE_ID` sont-ils renseignés ?
   Sans les deux, l'écriture est ignorée avec un simple log de debug.
2. Le niveau est-il `degraded` ? C'est volontaire : pas de report public.
3. Les services affectés sont-ils dans `HM_BS_RESOURCE_MAP` ? Un service non
   mappé est ignoré silencieusement.
4. `redis-cli get hm:incident:active` — `bs_report_id` est-il rempli ?

### Un incident fantôme se rouvre en boucle

Symptôme d'un anti-boucle contourné. Vérifier que le report est bien dans
`hm:bs:owned` (`sismember`) et que les updates arrivent dans
`hm:bs:seen_updates`. Un `hm:bs:owned` vidé — ou un Redis réinitialisé sans
resync — fait réadopter nos propres incidents.

### Le webhook Better Stack ne délivre plus

`redis-cli get hm:bs:last_event_at`. Un écart de plus de 24h log une erreur
explicite. Après 10 échecs de livraison, Better Stack désactive la souscription
**silencieusement** : la recréer à la main depuis la status page, avec
reconfirmation par email. Le poll toutes les 5 min limite les dégâts en
attendant.

### Redis est revenu mais l'état semble figé

Le resync a peut-être échoué en cours. Chercher `resync redis` dans les logs ;
un `redis perdu` juste après signale une reconnexion instable. Un redéploiement
recharge l'état depuis Redis (`Detector.load()`).

## Arrêt

Le lifespan FastAPI intercepte SIGTERM : annulation des boucles, arrêt du sticky,
`store.flush()` — une dernière tentative de reconnexion et de resync — puis
fermeture du client HTTP. Railway redéploie souvent, ce flush évite de perdre
l'état accumulé pendant une coupure Redis.

## Ce qui n'existe pas encore

- Toute la partie bot : listener Redis + ACK, sticky message, commandes
  `/status *` et Modals V2. Le monitor publie déjà tout ce qu'il faut.
- L'envoi Components V2 par webhook n'a jamais été confronté au vrai Discord.
- Aucune route n'expose `hm:incident:history`.
- Le poll de `railway.statuspage` évoqué dans la spec, pour afficher la cause
  d'une panne d'hébergeur.

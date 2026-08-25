# Bot et notification Discord

Le client Discord vit **dans le même process** que le monitor : le serveur
FastAPI, les boucles du scheduler et le client discord.py tournent dans la même
event loop asyncio. Ce n'est pas un service séparé, et ce n'est plus le bot
Moddy.

## Pourquoi une application dédiée

Le monitor doit rester capable de communiquer quand Moddy est down —
précisément le cas où on en a besoin. `Moddy Health Monitor` est donc une
application Discord distincte : son propre token, sa propre connexion gateway,
ses propres pannes. Le sticky, le bouton `Refresh` et les commandes `/status`
restent fonctionnels pendant une panne complète de Moddy.

## Lancement

```python
config = uvicorn.Config(app, host="0.0.0.0", port=settings.port,
                        log_config=None, lifespan="on")
await asyncio.gather(uvicorn.Server(config).serve(), run_bot(bot, token))
```

- `uvicorn.Server.serve()`, **pas** `uvicorn.run()` : ce dernier crée sa propre
  event loop et le bot n'aurait plus où tourner.
- `async with bot` (dans `bot.client.run`) ferme proprement la session HTTP de
  discord.py sur SIGTERM. Railway redéploie souvent, une gateway mal fermée
  laisse un shard fantôme quelques minutes.
- `run_bot` avale toute exception : un token invalide ne doit pas empêcher la
  détection, l'alerte par webhook et `/v1/status` de fonctionner.

### Intents

`guilds` et `guild_messages`. Pas de `message_content` : le sticky ne lit jamais
le contenu d'un message, seulement son ID et son salon — inutile de demander un
intent privilégié.

## Chaîne de redondance

```
1. Bot Health Monitor (discord.py, même process)
       │ gateway perdue, salon injoignable, ou envoi au-delà de HM_BOT_ACK_TIMEOUT
       ▼
2. Webhook Discord direct (HTTP depuis le monitor)
       │ échec (Discord down, webhook non configuré)
       ▼
3. File de rattrapage `hm:notify:queue` + log ERROR
```

Le niveau 1 est préféré : le bot édite ses propres messages et porte les
interactions. Le niveau 2 garantit que **si l'application est suspendue ou la
gateway perdue, l'alerte part quand même**.

**Le webhook de repli doit être créé à la main** depuis les paramètres du salon,
et surtout pas par l'application Health Monitor : si son token est compromis ou
l'app suspendue, les deux canaux tomberaient ensemble et la redondance ne
servirait à rien.

`Notifier.deliver()` renvoie un booléen ; `dispatch()` empile dans la file si la
livraison a échoué. Cette séparation est nécessaire : un incident qui porte déjà
un `discord_message_id` d'un envoi précédent ne doit pas être compté comme livré
sur la base de ce champ.

### Le transport est collant

`discord_transport` est stocké à côté de `discord_message_id`, et respecté à
chaque édition. C'est l'erreur la plus facile à commettre ici.

| Transport d'origine | Nouvelle version |
|---|---|
| `bot` | Le bot `fetch_message` puis `edit` |
| `webhook` | `PATCH /webhooks/{id}/{token}/messages/{message_id}` |
| `bot`, bot désormais muet | Le webhook ne peut pas éditer le message d'un autre auteur → **nouveau** message |
| `webhook`, webhook désormais mort | Le bot poste un **nouveau** message plutôt que de perdre l'information |

Tant que le webhook répond, le bot ne touche pas à un fil qui lui appartient :
il n'en ferait qu'un doublon. Un `404` sur l'édition (message supprimé à la
main) déclenche aussi un repost.

### Idempotence

Clé de déduplication : `sha1(incident_id + nombre d'updates + canal)`, stockée
dans `hm:notify:sent`.

La vérification est faite **tous canaux confondus** avant d'essayer quoi que ce
soit : une version donnée d'un incident ne part qu'une fois. Sans ça, un
incident déjà posté par le bot repartait une seconde fois par webhook au premier
retry — deux messages pour un seul événement.

### File de rattrapage

`hm:notify:queue`, liste Redis, plafonnée à 200 entrées. Vidée toutes les 30s
par la boucle `notify-queue`, **dans l'ordre** : un message qui échoue est remis
en tête (`lpush`) et le drain s'arrête là. Le rejouer plus tard dans le désordre
donnerait un fil d'incident incohérent.

## Un modèle, deux rendus

Le bot et le webhook envoient le même message par deux APIs incompatibles :
discord.py veut des objets `ui.*`, le webhook veut du JSON brut. Écrire deux
fois la mise en forme est la garantie qu'elles divergeront.

```
render/model.py    IncidentPresentation — le modèle commun
render/theme.py    couleurs, icônes, libellés
render/layout.py   -> discord.ui.LayoutView   (chemin bot)
render/raw.py      -> JSON brut               (chemin webhook)
```

`tests/test_render_parity.py` compare les deux sorties sur le même modèle, pour
chaque cas de bord (résolu, sans URL, sans update, maintenance). C'est dix
lignes qui empêchent les deux chemins de dériver.

`render/layout.py` porte aussi `BaseView`, le socle de toutes les vues :
`timeout=None` et un handler d'erreurs central. Sans lui, une exception dans un
callback laisse l'interaction sans réponse et affiche « L'application ne répond
pas » au staff en pleine crise.

### Sobriété du rendu

Tout message du bot est un message **Components V2** — jamais de contenu texte
nu, jamais d'embed (sauf le repli webhook). Les réponses éphémères des commandes
elles-mêmes passent par `build_notice_view`.

Un jeu d'icônes fermé, en trois familles qui ne se mélangent jamais.

**État d'un service ou d'un niveau** — titre d'incident, sticky, panneau :

| Icône | Usage |
|---|---|
| `<:check_circle:1541801328584433664>` | opérationnel, incident résolu |
| `<:down:1541799254807543808>` | service down, panne partielle ou majeure |
| `<:degraded:1541799158938083430>` | performance dégradée |
| `<:maintenance:1541798162833080320>` | maintenance |

**Ligne « Status: » d'un incident**, et elle seule — jamais dans un titre,
jamais dans une liste de services :

| Icône | Usage |
|---|---|
| `<:OnGoing:1541798161599828038>` | `Status: On Going` |
| `<:Resolved:1541798160278749244>` | `Status: Resolved` |

**Réponses éphémères du bot et détail des checks**, en blanc : un accusé de
réception n'est pas un état public.

| Icône | Usage |
|---|---|
| `<:check_circle_white:1541799862859989052>` | confirmation, check qui passe |
| `<:exclamation:1541799657829568582>` | refus, erreur, check en échec, état inconnu |
| `<a:spinner:1541617132104843264>` | chargement |

Le liseré (`accent_color`) suit le niveau : `#379057` résolu, `#FF8C00`
dégradé, `#E92B2B` down, `#5985E1` maintenance.

Ils doivent être uploadés en **application emojis** sur l'application Health
Monitor, via le portail développeur. C'est la seule option qui garantisse un
rendu correct dans les deux chemins d'envoi : un émoji appartenant à un serveur
que l'application ne connaît pas s'affiche cassé côté webhook.

### Mentions

Le message d'incident prévient lui-même, en petit, sous l'en-tête :

```
**Status:** <:OnGoing:...>On Going
-# <@&1424466344832925847> / @here
```

`DISCORD_ALERT_ROLE_ID` est mentionné à chaque incident ; le `@here` ne
s'ajoute que si l'incident touche un service de `HM_ESCALATE_SERVICES` — le
bot, le dashboard ou l'API, ce qu'un utilisateur voit tomber. Une panne de
Feeds ne réveille pas le salon. Aucune de ces deux listes n'est écrite dans le
rendu : `Settings.mention_line()` rend une chaîne, le renderer l'affiche (§6).

- **La mention vit dans le message, pas à côté.** Discord ne repingue pas à
  l'édition : l'alerte part une fois, à la publication, et les updates qui
  suivent réutilisent le même message sans re-notifier.
- **Le repli embed la porte en `content`.** Une mention placée dans un embed ne
  prévient personne, et un message Components V2 n'a pas le droit d'avoir de
  `content` : c'est donc uniquement dans le repli que `content` est rempli.
- **Permission requise.** Sans `Mention @everyone, @here and All Roles`, le
  `@here` s'affiche sans prévenir personne — et le rôle aussi, s'il n'est pas
  lui-même mentionnable.

### Container 1 — en-tête

`accent_color` selon le niveau, une Section (`type: 9`) portant le titre et un
bouton lien.

```json
{
  "type": 17,
  "accent_color": 15280939,
  "components": [
    {
      "type": 9,
      "accessory": { "type": 2, "style": 5, "label": "View Incident",
                     "url": "https://status.moddy.app/en/incident/995593" },
      "components": [
        { "type": 10, "content": "### <:error:...> Major Outage – Bot & API Unavailable" }
      ]
    },
    { "type": 10,
      "content": "**Created by:** Moddy Health Monitor\n**Affected services:** ``Moddy Bot``, ``API``\n**Status:** <:check_circle:...>Resolved" }
  ]
}
```

Sans URL — incident `degraded`, ou Better Stack injoignable — la Section est
remplacée par un simple TextDisplay : l'API refuse une Section sans `accessory`,
et un bouton lien avec une URL vide lève à l'envoi.

### Container 2 — historique

`accent_color: null`, une entrée par update, séparées par un `type: 14`.

```json
{ "type": 17, "accent_color": null, "components": [
  { "type": 10, "content": "### **Updates:**" },
  { "type": 10, "content": "**Created** — <t:1785763800:F> :\n> ..." },
  { "type": 14, "divider": true, "spacing": 1 },
  { "type": 10, "content": "**Resolved** — <t:1785941143:F> :\n> ..." }
]}
```

Toujours `<t:unix:F>` : chaque lecteur voit la date dans son propre fuseau.
C'est aussi ce qui évite le décalage subi par la status page, réglée sur
`America/Adak`.

Un message Discord plafonne à 40 composants ; seuls les **15 derniers** updates
sont rendus, précédés d'une ligne `-# N earlier update(s) not shown.` le cas
échéant. Un message multi-ligne est intégralement mis en blockquote (`> ` sur
chaque ligne), sans quoi seule la première ligne serait citée.

### Table de rendu

| Élément | Valeur |
|---|---|
| `accent_color` incident majeur / partiel | `15280939` (`#E93A3A`) |
| `accent_color` dégradé | `15774258` (`#F0B232`) |
| `accent_color` maintenance | `5793266` (`#5865F2`) |
| `accent_color` résolu | `5763719` (`#57F287`) |

Le statut affiché a trois valeurs : `Ongoing`, `Resolved`, et `Maintenance` pour
ce type d'incident tant qu'il n'est pas clos.

## Envoi par webhook

```python
POST {WEBHOOK_URL}?wait=true&with_components=true
{"flags": 32768, "components": [...]}
```

`32768` = `IS_COMPONENTS_V2` (`1 << 15`).

**Repli dégradé :** si le webhook renvoie `400` sur ce format, `components_v2`
passe à `False` pour la durée du process et tout repart en embed classique
(`build_raw_embed`). Titre, description, services affectés, statut et dernier
update y survivent ; l'historique complet et le bouton, non.

Ce maillon reste **le seul non validé contre le vrai Discord** : il demande une
URL de webhook réelle. C'est le premier test à faire au déploiement.

Retries : 3 tentatives, backoff `2^n`, respect du `retry_after` sur 429, retry
sur 5xx.

## Sticky message

Un message permanent en bas du salon, avec une ligne par service :

```
### <:check_circle:...> All Systems Operational
-# Last updated <t:1787000000:R>

<:check_circle:...> ``Moddy Bot``  Operational
<:check_circle:...> ``Dashboard``  Operational
<:degraded:...>     ``API      ``  Degraded
──────────────────────────
[ Details ]  [ Status Page ]
```

Trois déclencheurs le font bouger, et ils peuvent tomber ensemble :

| Déclencheur | Effet |
|---|---|
| Message d'un tiers dans le salon | Repost après `HM_STICKY_DEBOUNCE` |
| Rafraîchissement passif (`HM_STICKY_REFRESH_INTERVAL`) | **Édition**, pas repost |
| Changement du niveau global (boucle `check`) | Édition immédiate |
| `/status sticky` | Repost forcé |

- **Le sticky ne se repost jamais sur lui-même.** `channel.send()` n'a pas
  encore rendu l'ID que la gateway a déjà livré le `MESSAGE_CREATE`
  correspondant : le sticky ne se reconnaissait pas, se croyait poussé par un
  tiers et repostait — ce qui produisait le message déclenchant le suivant. Dix
  stickys empilés dans le salon, constaté en production. Deux gardes : un
  historique court des IDs qu'on vient de poster, et surtout une vérification
  qu'il n'est pas *déjà* le dernier message du salon avant tout repost.
- **Debounce.** Sans lui, une rafale de dix messages produit dix reposts et un
  rate limit Discord immédiat.
- **Verrou asyncio.** Les trois déclencheurs peuvent arriver en même temps ;
  sans verrou, on se retrouve avec plusieurs stickys en double.
- **Supprimer l'ancien avant de poster le nouveau**, en tolérant son absence.
- **Le salon est la source de vérité, pas Redis.** `hm:sticky:message_id` n'est
  qu'un raccourci. Sans Redis — mémoire seule, ou instance vidée — l'ID est
  perdu à chaque redémarrage : le monitor postait alors un sticky neuf en
  abandonnant le précédent, soit un cadavre par redéploiement. Et Railway
  redéploie souvent. Au démarrage, si l'ID manque, le sticky **se retrouve dans
  le salon** : il adopte le dernier message qui lui appartient et supprime les
  autres.
- **Un sticky se reconnaît à son bouton.** `custom_id: hm:sticky:refresh`, plus
  l'auteur du message. C'est ce qui le distingue des messages d'incident, que le
  bot poste dans le même salon et qu'il ne faut surtout pas supprimer : ceux-ci
  ne portent aucun `custom_id` — leur seul bouton est un lien. Un test le
  verrouille sur le vrai rendu : ajouter un composant interactif au message
  d'incident le fera tomber.
- **Jamais un sticky de plus si l'ancien a résisté.** Sans la permission
  `Manage Messages`, la suppression échoue : poster quand même laisserait un
  sticky mort à chaque repost, et le salon se remplirait en silence — l'échec
  était journalisé en `debug`, invisible en production. Il est désormais en
  `warn` et nomme la permission manquante, et le sticky est **édité sur place**
  plutôt que reposté. Un sticky qui n'est pas tout en bas vaut mieux qu'un salon
  saturé.

## Bouton Details

Vue persistante, `custom_id` fixe `hm:sticky:refresh`, réenregistrée au
démarrage par `add_view` dans `setup_hook`. Sans ça le bouton est mort après
chaque redéploiement — et Railway redéploie souvent. Le libellé est passé de
`Refresh` à `Details` ; le `custom_id`, lui, ne bouge pas : c'est à lui que le
sticky se reconnaît dans le salon, et le changer abandonnerait tous les stickys
déjà postés.

Sa réponse est **éphémère et plus détaillée que le sticky** : un bloc par
service — version, uptime, âge du dernier heartbeat, services impactés par
ricochet — séparés les uns des autres, et son propre bouton `Refresh` qui
rejoue le panneau sur place (vue persistante elle aussi).

```
### <:check_circle:...> All Systems Operational
-# Last updated <t:1787000000:R>
──────────────────────────
<:check_circle:...> **Moddy Bot** · Operational
-# `1.4.2` · up 2h01 · heartbeat 13s ago
-# <:check_circle_white:...> 3 checks passing
──────────────────────────
<:degraded:...> **API** · Degraded
-# `2.0.0` · up 0h05 · heartbeat 11s ago
-# <:exclamation:...> redis · 1/2 passing
```

Le bouton `Details` est bleu et porte `<:info:1541808220610363423>` ; le
`Refresh` du panneau est vert et porte `<:refresh:1541808218760544376>`. Ce
sont des icônes de bouton : elles n'apparaissent jamais dans du texte.

**Le panneau ne se construit pas à l'écran.** À l'ouverture comme au refresh,
l'éphémère part avec un simple loader — `<a:spinner:...> Loading…` — remplacé
par le panneau complet après 0 à 1 s. Ce délai n'attend rien : lire Redis prend
quelques millisecondes, il laisse seulement le loader s'afficher au lieu de
clignoter. Une édition qui échoue (éphémère fermé, token expiré) ne casse rien,
le bouton reste cliquable.

**Les checks se résument, ils ne se dumpent pas.** Le panneau affichait
`postgres: {'ok': True, 'latency_ms': 4} · redis: {...}` — illisible. En régime
normal un compteur suffit ; en panne, seuls les checks en échec sont nommés.
Seule la clé `ok`, qui fait partie du contrat de heartbeat, est lue : les noms
de clés restent libres (§6).

L'ordre des services vient de `HM_SERVICE_ORDER`, pas de l'ordre de
surveillance : on montre d'abord ce que voit un utilisateur.

Elle lit `hm:status:public` et `hm:hb:{service}` **directement dans Redis**,
jamais par un appel HTTP à `/v1/status` : viser son propre process ajouterait un
point de panne pour rien.

`HM_REFRESH_COOLDOWN` (5s par utilisateur) — sans ça, le bouton est un vecteur
de spam sur un salon public.

## Sévérité par service

`/status incident` ne publie pas en sortant du modal. Le modal tient dans cinq
composants top-level et les cinq sont pris (titre, message, sévérité globale,
services affectés, notification) : impossible d'y demander en plus l'état de
*chaque* service. Et un modal ne peut pas en ouvrir un second — Discord ne
l'autorise qu'en réponse à une commande ou à un composant.

Le brouillon part donc dans `hm:incident:draft:{user}` (TTL 15 min) et un
panneau éphémère prend la suite : un select pour désigner les services
franchement down, un bouton `Publish`. Les services non cochés sont publiés en
`degraded`. Sans cette étape, tout service affecté partait en `downtime` sur la
status page, y compris ceux qui ne faisaient que ralentir.

- **L'état vit dans le store, pas dans la View.** C'est ce qui permet de la
  réenregistrer vide au démarrage : les callbacks relisent le brouillon de
  l'utilisateur qui clique, et un redéploiement au mauvais moment ne perd pas ce
  que le staff vient d'écrire.
- **Le brouillon est supprimé avant publication**, pas après : deux clics sur
  `Publish` ne doivent pas ouvrir deux incidents.

## Commandes

Groupe `/status`, synchronisé sur la guild uniquement : le sync global met
jusqu'à une heure à se propager, celui d'une guild est instantané.

| Commande | Effet |
|---|---|
| `/status incident` | Modal de création |
| `/status update` | Modal d'update sur l'incident actif |
| `/status resolve` | Modal de résolution |
| `/status maintenance` | Modal de maintenance planifiée |
| `/status check` | État détaillé (éphémère) |
| `/status sticky` | Force le repost du sticky |

Le bot ne parle jamais à Better Stack : il appelle
`IncidentManager.handle_command`, dans ce process. Toute la logique d'incident
reste en un seul endroit, et le token Better Stack n'existe que côté monitor.

### Permissions

`DISCORD_STAFF_ROLE_ID` dans la guild `DISCORD_GUILD_ID`. Sans rôle configuré,
le repli est la permission `manage_guild` — jamais l'ouverture à tout le
serveur. Un `on_error` sur le `CommandTree` répond au refus : une check qui lève
sans réponse laisse l'interaction en échec visible.

### Modals V2

| Point | Règle |
|---|---|
| Composants top-level | 5 maximum, chacun `Label` ou `TextDisplay` |
| `TextInput.label` | Déprécié — le texte affiché vient de `Label.text` |
| `disabled` | **Interdit** dans un modal, erreur API |
| Lecture des valeurs | `.component.value` / `.component.values` |
| `row` | Peu fiable en V2, l'ordre suit la déclaration |
| `RadioGroup`, `CheckboxGroup` | discord.py ≥ 2.7 |
| Décorateurs | `@ui.label` n'existe pas — attributs ou `add_item` |

- **`defer(ephemeral=True, thinking=True)` est obligatoire.** Créer un incident
  appelle Better Stack *et* publie sur Discord : ça dépasse facilement la
  fenêtre de 3 secondes d'une interaction.
- **La liste des services affectés vient de `known_services`**, jamais d'une
  liste en dur : ajouter un service reste une affaire de variables
  d'environnement (invariant §6). `CheckboxGroup` plafonne à 10 options.
- **`update` et `resolve` vérifient l'incident actif *avant* d'ouvrir le
  modal** : `send_modal` ne peut pas être annulé une fois envoyé. L'incident
  concerné vient de `hm:incident:active`, il n'est jamais demandé au staff.
- **La fenêtre de maintenance tient en un seul champ**
  (`2026-08-25 02:00 -> 04:00`) : deux champs séparés porteraient le modal à six
  composants. `ends_at` est obligatoire côté Better Stack pour un
  `report_type: "maintenance"`.

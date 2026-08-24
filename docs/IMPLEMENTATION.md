# Moddy Health Monitor — Documentation d'implémentation

Service de monitoring interne de l'écosystème Moddy. Headless, déployé sur Railway, autonome vis-à-vis du reste de l'infrastructure.

**Nom du service :** `moddy-health-monitor`
**Nom d'affichage (Discord / Better Stack) :** `Moddy Health Monitor`

---

## 1. Principe général

Le monitor **ne va pas chercher** l'état des services. Ce sont les services qui **poussent** leur état vers lui à intervalle régulier (`push` / dead man's switch).

```
moddy-bot      ─┐
moddy-api      ─┤
moddy-altguard ─┼──POST /ingest/heartbeat──▶  moddy-health-monitor
moddy-feeds    ─┘                                    │
                                                     ├─▶ Redis (état + TTL)
                                                     ├─▶ Better Stack (status page)
                                                     ├─▶ Discord (bot ou webhook)
                                                     └─▶ GET /v1/status (public)
```

Trois flux entrants :

| Flux | Source | Rôle |
|---|---|---|
| Heartbeats | Les services Moddy | Détection automatique |
| Commandes Discord | Staff via le bot | Gestion de crise manuelle |
| Poll Better Stack | `status.moddy.app/index.json` | Récupérer les actions manuelles faites depuis Better Stack |

Trois flux sortants :

| Flux | Cible | Rôle |
|---|---|---|
| Status reports | API Better Stack | Page de statut publique |
| Messages Components V2 | Discord (bot → fallback webhook) | Communication utilisateurs |
| JSON public | Dashboard, site, services tiers | Bannière d'incident |

**Principe de conception directeur :** le monitor ne doit dépendre de rien de ce qu'il surveille. Pas de PostgreSQL. Redis uniquement pour la persistance, avec fallback mémoire. Aucun appel vers l'API Moddy.

---

## 2. Modèle de données (Redis)

Aucune base relationnelle. Tout tient dans Redis, avec un fallback mémoire si Redis est injoignable.

| Clé | Type | TTL | Contenu |
|---|---|---|---|
| `hm:hb:{service}` | string (JSON) | `interval × 3` | Dernier heartbeat reçu |
| `hm:state:{service}` | string (JSON) | — | État calculé : `status`, `since`, `consecutive_failures` |
| `hm:incident:active` | string (JSON) | — | Incident en cours (un seul à la fois) |
| `hm:incident:history` | list | trim 100 | Incidents clos (rolling) |
| `hm:bs:owned` | set | — | IDs des status_report créés par le monitor |
| `hm:bs:seen_updates` | set | — | IDs des status_update déjà traités |
| `hm:bs:cursor` | string | — | `updated_at` du dernier poll Better Stack |
| `hm:status:public` | string (JSON) | 30s | Réponse de `/v1/status` pré-calculée |
| `hm:sticky:message_id` | string | — | ID du sticky message Discord |
| `hm:notify:sent` | set | — | Anti-doublon des notifications (clé = hash) |

**Fallback mémoire :** au démarrage, le monitor tente de recharger son état depuis Redis. Si Redis est down, il travaille en mémoire, log un warning, continue d'alerter, et resynchronise dès que Redis revient. Une panne Redis ne doit jamais empêcher une alerte de partir.

---

## 3. Contrat de heartbeat

### Endpoint

```
POST /ingest/heartbeat
Header: X-Health-Token: <HEALTH_INGEST_TOKEN>
Content-Type: application/json
```

### Body

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

- `status` : `ok` | `degraded` | `down` — **le service décide lui-même de son état**, il connaît ses dépendances mieux que le monitor.
- `checks` : dictionnaire à clés libres. Le monitor **n'interprète jamais les noms de clés** — il itère dessus pour l'affichage. Zéro logique par service côté monitor.
- `meta` : libre, affiché tel quel.

### Réponse

```json
{ "ok": true, "received_at": "2026-08-24T19:42:11Z", "incident_active": false }
```

Le champ `incident_active` permet à un service de savoir qu'un incident est en cours (utile pour dégrader son propre comportement, ex. couper les notifications non critiques).

### Côté service émetteur

Une task asyncio isolée, **fire-and-forget**, timeout 5s, jamais bloquante, un échec ne fait que logger. Intervalle recommandé : **20s**, donc TTL de 60s côté monitor.

```python
async def _heartbeat_loop(self):
    while True:
        try:
            payload = await self._build_health_payload()
            await self._http.post(HM_URL, json=payload,
                                  headers={"X-Health-Token": TOKEN}, timeout=5)
        except Exception as e:
            log.warning("heartbeat failed: %s", e)
        await asyncio.sleep(20)
```

**Cas particulier du bot :** n'envoyer le heartbeat que si `bot.is_ready()` est vrai. Un event loop vivant dont la connexion gateway est morte ne doit pas se déclarer `ok`. Inclure `is_ready`, la latence gateway et le ratio de shards connectés dans `checks`.

---

## 4. Détection

### Liste des services attendus

**Obligatoire :** le monitor a en configuration la liste exhaustive des services attendus. Sans ça, un service qui n'a jamais démarré est simplement du silence — et personne ne le remarque.

```env
HM_SERVICES=moddy-bot,moddy-api,moddy-altguard,moddy-feeds
```

### Machine à états par service

| État | Condition |
|---|---|
| `operational` | Heartbeat frais + `status: ok` |
| `degraded` | Heartbeat frais + `status: degraded` |
| `down` | Heartbeat frais + `status: down`, **ou** clé `hm:hb:{service}` expirée |
| `unknown` | Aucun heartbeat jamais reçu depuis le démarrage du monitor |

### Anti faux-positifs

- **Grace period au démarrage** du monitor : 90s pendant lesquelles aucune alerte ne part (sinon chaque redéploiement du monitor déclenche une alerte par service).
- **Seuil de déclenchement** : 3 cycles consécutifs en échec **ET** minimum 60s de silence.
- **Seuil de résolution** : 2 cycles consécutifs OK.
- **Rate-limit notification** : max 1 message Discord par service par tranche de 5 min pour le même état.

### Sévérité agrégée

Calculée à partir des états individuels, avec une pondération configurable :

| Niveau | Condition | Couleur | Action |
|---|---|---|---|
| `operational` | Tout OK | — | Rien |
| `degraded` | ≥1 service en `degraded`, ou un service non critique down | `#F0B232` | Discord seulement |
| `partial_outage` | ≥1 service critique down, mais pas tous | `#E93A3A` | Discord + Better Stack |
| `major_outage` | Bot **et** API down | `#E93A3A` | Discord + Better Stack + notify subscribers |

Services critiques par défaut : `moddy-bot`, `moddy-api`. Configurable via `HM_CRITICAL_SERVICES`.

Le `degraded` **ne crée pas** d'incident public. Sinon la status page passe au rouge à chaque hoquet de Redis.

---

## 5. Cycle de vie d'un incident

Un seul incident actif à la fois (`hm:incident:active`). Si un nouveau service tombe pendant un incident en cours, on **met à jour** l'incident existant plutôt que d'en créer un second.

```
DÉTECTION (auto)          COMMANDE (/incident create)      POLL BETTER STACK
      │                            │                              │
      └────────────┬───────────────┴──────────────────────────────┘
                   ▼
          ┌─────────────────┐
          │  OPEN            │──▶ create BS report ──▶ Discord message
          └────────┬─────────┘                          (nouveau)
                   │
          ┌────────▼─────────┐
          │  UPDATING        │──▶ post BS update  ──▶ Discord message
          └────────┬─────────┘                          (édité)
                   │
          ┌────────▼─────────┐
          │  RESOLVED        │──▶ resolve BS      ──▶ Discord message
          └──────────────────┘                          (édité, final)
                   │
                   └──▶ push vers hm:incident:history
```

### Structure d'un incident

```json
{
  "id": "inc_20260824_1942",
  "bs_report_id": "995593",
  "discord_message_id": "1409...",
  "discord_channel_id": "1398625686301704323",
  "title": "Major Outage – Bot & API Unavailable",
  "type": "incident",
  "level": "major_outage",
  "origin": "auto",
  "affected": ["moddy-bot", "moddy-api", "moddy-dashboard"],
  "status": "resolved",
  "created_at": "2026-08-24T19:42:00Z",
  "resolved_at": "2026-08-24T20:15:00Z",
  "updates": [
    { "kind": "created",  "at": "...", "message": "...", "author": "Moddy Health Monitor" },
    { "kind": "updated",  "at": "...", "message": "...", "author": "Jules" },
    { "kind": "resolved", "at": "...", "message": "...", "author": "Jules" }
  ]
}
```

`type` : `incident` | `maintenance` | `degraded_performance`
`origin` : `auto` | `discord` | `betterstack`

**Un seul message Discord par incident**, édité à chaque update. C'est ce que reflète le payload de référence : le premier container contient le titre et l'état courant, le second l'historique complet des updates.

---

## 6. Better Stack — synchronisation bidirectionnelle

Tous les endpoints ci-dessous ont été vérifiés sur la documentation officielle Better Stack.

### Écriture (monitor → Better Stack)

Base : `https://uptime.betterstack.com`, header `Authorization: Bearer $BETTERSTACK_TOKEN`.

| Action | Appel |
|---|---|
| Créer un report | `POST /api/v2/status-pages/{spid}/status-reports` |
| Ajouter un update | `POST /api/v2/status-pages/{spid}/status-reports/{rid}/status-updates` |
| Résoudre | un update avec `status: "resolved"` sur chaque ressource affectée |
| Modifier un report | `PATCH /api/v2/status-pages/{spid}/status-reports/{rid}` |

> Ne pas confondre avec `/api/v3/incidents` : c'est l'API **Incident Management** (on-call, escalade), un objet différent qui n'alimente pas la status page. Pour une status page, c'est bien `status-reports` en v2.

**Il n'existe pas d'endpoint `/resolve`.** La résolution se fait en postant un status update dont les `affected_resources` portent `status: "resolved"`.

#### Créer un report

```json
{
  "title": "Major Outage – Bot & API Unavailable",
  "message": "We are currently experiencing a service outage...",
  "report_type": "manual",
  "notify_subscribers": false,
  "affected_resources": [
    { "status_page_resource_id": "8720238", "status": "downtime" },
    { "status_page_resource_id": "8720241", "status": "downtime" }
  ]
}
```

| Champ | Notes |
|---|---|
| `report_type` | `manual` ou `maintenance`. Défaut `manual`. |
| `notify_subscribers` | Booléen, **défaut `false`**. Explique pourquoi tous les updates historiques de la page sont à `false`. |
| `affected_resources[].status` | `resolved`, `degraded` ou `downtime`. Doit valoir `maintenance` si `report_type: "maintenance"`. |
| `published_at` | ISO-8601, défaut maintenant. **Utile pour la file de rattrapage** : permet de publier un incident avec son horodatage d'origine après une panne. |
| `starts_at` | ISO-8601, défaut maintenant. |
| `ends_at` | ISO-8601. **Obligatoire uniquement si `report_type: "maintenance"`.** |

Réponse `201`, l'ID du report est dans `data.id` — à stocker immédiatement.

#### Poster un update

```json
{
  "message": "We identified the issue and have deployed a fix.",
  "notify_subscribers": false,
  "affected_resources": [
    { "status_page_resource_id": "8720238", "status": "resolved" }
  ]
}
```

**`affected_resources` est obligatoire sur les updates** (contrairement à la création où il est optionnel). Chaque update doit re-déclarer l'état de chaque ressource concernée.

#### Mapping service → resource_id

```env
HM_BS_RESOURCE_MAP=moddy-bot:8720238,moddy-website:8720239,moddy-dashboard:8720240,moddy-api:8720241
```

**Ne marquer que les ressources réellement affectées.** L'historique actuel montre Website et Dashboard en `degraded` du 3 au 5 août alors que leur `availability` est à 1.0 — les reports manuels ont sali la barre journalière de services qui allaient bien.

---

### Lecture (Better Stack → monitor)

Deux mécanismes complémentaires. **Le webhook est le chemin principal, le poll est le filet de sécurité.**

#### A. Webhook subscription (principal)

Better Stack pousse un POST vers le monitor à chaque changement. Pas de polling, latence quasi nulle.

**Mise en place manuelle, une seule fois :** aller sur `status.moddy.app` → « Get updates » → type **Webhook** → URL `https://<monitor>/ingest/betterstack` → confirmer par email. Il n'y a pas d'API pour créer la souscription, ça passe par l'interface publique.

Trois `event_type` :

| Event | Objet transporté | Usage |
|---|---|---|
| `incident` | `incident` | Incident créé ou mis à jour |
| `maintenance` | `maintenance` (avec `starts_at`/`ends_at`) | Maintenance planifiée |
| `component_update` | `component_update` + `component` | Un monitor a changé d'état |

Payload d'incident :

```json
{
  "event_type": "incident",
  "page": {
    "id": 237745,
    "status_indicator": "downtime",
    "status_description": "Some services are down"
  },
  "incident": {
    "id": 98765,
    "name": "Database connection issues",
    "created_at": "2026-01-15T10:30:00Z",
    "updated_at": "2026-01-15T11:45:00Z",
    "shortlink": "https://status.moddy.app/98765/incidents",
    "incident_updates": [
      { "id": 11111, "status_report_id": 98765, "body": "...", "created_at": "..." }
    ]
  }
}
```

Points importants :

- `incident.id` **est** le `status_report_id` — c'est la clé de jointure avec ce que le monitor a créé.
- `incident_updates` arrive **du plus récent au plus ancien**.
- `shortlink` fournit l'URL publique de l'incident : à utiliser tel quel pour le bouton Discord, plutôt que de reconstruire l'URL.
- Header `X-BetterUptime-Event: <event_type>`, User-Agent `BetterStack-StatusPage/1.0`.
- **Pas de signature ni d'authentification.** Si tu veux protéger l'endpoint, mets un secret dans l'URL (`/ingest/betterstack?k=<secret>`) — la doc recommande explicitement l'auth par URL, aucun header d'auth n'est supporté.
- **Répondre 2xx immédiatement**, traiter en tâche de fond. Timeout à 30s côté Better Stack.
- Retry avec backoff : 30s, 1min, 2min, 4min, 8min… jusqu'à 10 tentatives. **Après 10 échecs, la souscription est désactivée** et il faut la recréer manuellement avec reconfirmation par email.

Ce dernier point est un vrai risque : si le monitor est down plus de ~15 minutes, Better Stack peut couper le webhook silencieusement. D'où le poll de secours.

#### B. Poll `index.json` (secours et réconciliation)

`GET https://status.moddy.app/index.json`, sans authentification. Fréquence réduite à **5 min** puisque ce n'est plus le chemin principal — juste de quoi détecter qu'on a raté un webhook.

Parsing :

- `data.attributes.aggregate_state` → `operational` | `degraded` | `downtime` | `maintenance`
- `included[]` `type == "status_report"` → incidents
- `included[]` `type == "status_update"` → updates, reliés via `relationships.status_updates`
- `included[]` `type == "status_page_resource"` → état par ressource

**Trois pièges confirmés :**

1. **`ends_at` reste `null` même sur les reports résolus.** Se fier à `aggregate_state`, jamais à `ends_at`. Un parseur naïf verra tous les incidents historiques comme ouverts en permanence.
2. **`report_type` peut valoir `automatic`**, pas seulement `manual` et `maintenance` — c'est le cas des incidents créés par les monitors Better Stack eux-mêmes. À traiter comme une troisième origine, distincte des incidents du monitor.
3. **`availability` (monitors) et `status_history` (reports) sont deux sources de vérité distinctes** et peuvent diverger. Ne pas les croiser.

Statuts de ressource possibles : `operational`, `degraded`, `downtime`, `maintenance`, `not_monitored`.

#### Anti-boucle — indispensable dans les deux cas

Le monitor écrit dans Better Stack, et Better Stack lui renvoie ses propres écritures par webhook. Sans garde-fou, boucle infinie.

```python
async def handle_bs_event(payload):
    inc = payload.get("incident") or payload.get("maintenance")
    if not inc:
        return await handle_component_update(payload)
    report_id = str(inc["id"])
    owned = await redis.sismember("hm:bs:owned", report_id)
    for upd in reversed(inc.get("incident_updates", [])):   # du plus ancien au plus récent
        upd_id = str(upd["id"])
        if await redis.sismember("hm:bs:seen_updates", upd_id):
            continue                                        # déjà traité (retry ou écho)
        await redis.sadd("hm:bs:seen_updates", upd_id)
        if owned:
            await relay_to_discord(upd)                     # update manuel sur notre incident
        else:
            await adopt_incident(inc, upd)                  # incident créé hors du monitor
```

Deux règles absolues :

- Enregistrer le `data.id` retourné par Better Stack dans `hm:bs:owned` **avant** tout autre traitement, sinon la course avec le webhook entrant est perdue.
- Enregistrer l'ID de chaque update **que le monitor vient de créer** dans `hm:bs:seen_updates` dès la réponse `201`, pour que l'écho webhook soit ignoré.

La déduplication par `id` couvre aussi les livraisons multiples dues aux retries, que la doc signale explicitement comme possibles.

### Maintenances planifiées

`report_type: "maintenance"`, avec `starts_at` **et `ends_at` obligatoires**, et `status: "maintenance"` sur chaque ressource affectée. Le monitor les traite comme un incident de type `maintenance` : couleur neutre, pas d'alerte urgente, mais présent dans `/v1/status` pour la bannière du dashboard.

### Heartbeat sortant (surveiller le surveillant)

Créer un Heartbeat monitor dans Better Stack (`POST /api/v2/heartbeats` avec `name`, `period`, `grace`). La réponse contient l'URL de ping dans `data.attributes.url` :

```
https://uptime.betterstack.com/api/v1/heartbeat/<HEARTBEAT_TOKEN>
```

Un simple GET ou POST sur cette URL vaut « je suis vivant ». Pour signaler explicitement un échec, suffixer `/fail`. On peut aussi transmettre un code de sortie et une sortie texte : `POST .../heartbeat/<TOKEN>/<exit_code>` avec le corps en payload.

Réglage conseillé : `period=120`, `grace=60` — soit un ping toutes les 60s côté monitor, avec de la marge.

---

## 7. Notification Discord

### Chaîne de redondance

Trois niveaux, essayés dans l'ordre, avec bascule automatique :

```
1. Bot Moddy (via Redis pubsub `moddy:hm:notify`)
       │ échec ou pas d'ACK sous 5s
       ▼
2. Webhook Discord direct (HTTP depuis le monitor)
       │ échec (Discord down)
       ▼
3. Better Stack seul + log + retry en file d'attente
```

Le niveau 1 est préféré parce que le bot peut éditer ses propres messages, gérer le sticky et les interactions. Le niveau 2 garantit que **si le bot est mort, l'alerte part quand même** — c'est précisément le cas où on en a le plus besoin.

**File de rattrapage :** tout message qui n'a pu partir sur aucun canal est empilé dans `hm:notify:queue` (Redis list). Dès qu'un canal redevient disponible, la file est vidée dans l'ordre, avec les timestamps d'origine. Un incident survenu pendant une panne Discord n'est jamais perdu.

**ACK bot :** le bot publie sur `moddy:hm:notify:ack` avec le `message_id` obtenu. Le monitor le stocke dans l'incident pour pouvoir éditer plus tard. Si l'ACK n'arrive pas en 5s, on bascule sur le webhook.

### Format du message (Components V2)

Le monitor construit le **JSON brut** des components. Le bot le relaie tel quel, le webhook aussi. Une seule fonction de rendu, deux transports.

Structure : deux containers.

**Container 1 — en-tête**, `accent_color` selon le niveau, une Section (`type: 9`) avec le titre et un bouton lien vers l'incident.

```json
{
  "type": 17,
  "accent_color": 15280939,
  "components": [
    {
      "type": 9,
      "accessory": {
        "type": 2, "style": 5, "label": "View Incident",
        "url": "https://status.moddy.app/en/incident/995593"
      },
      "components": [
        { "type": 10, "content": "### <:error_circle_white:1534635025629319419> Major Outage – Bot & API Unavailable" }
      ]
    },
    {
      "type": 10,
      "content": "**Created by:** Moddy Health Monitor\n**Affected services:** ``Moddy Bot``, ``API``, ``Dashboard``\n**Status:** <:verified2:1495440135163084870>Resolved"
    }
  ]
}
```

**Container 2 — historique des updates**, `accent_color: null`, une entrée par update séparée par un `type: 14`.

```json
{
  "type": 17,
  "accent_color": null,
  "components": [
    { "type": 10, "content": "### **Updates:**" },
    { "type": 10, "content": "**Created** — <t:1785763800:F> :\n> ..." },
    { "type": 14, "divider": true, "spacing": 1 },
    { "type": 10, "content": "**Updated** — <t:1785940859:F> :\n> ..." },
    { "type": 14, "divider": true, "spacing": 1 },
    { "type": 10, "content": "**Resolved** — <t:1785941143:F> :\n> ..." }
  ]
}
```

Toujours utiliser `<t:unix:F>` pour les dates — Discord les affiche dans le fuseau de chaque lecteur.

### Table de rendu

| Élément | Valeur |
|---|---|
| `accent_color` incident majeur | `15280939` (`#E93A3A`) |
| `accent_color` dégradé | `15774258` (`#F0B232`) |
| `accent_color` maintenance | `5793266` (`#5865F2`) |
| `accent_color` résolu | `5763719` (`#57F287`) |
| Emoji en cours | `<:error_circle_white:1534635025629319419>` |
| Emoji résolu | `<:verified2:1495440135163084870>` |

### Envoi via webhook

Pour les Components V2 en webhook, il faut le flag `IS_COMPONENTS_V2` (`1 << 15` = `32768`) et le paramètre `?with_components=true` :

```python
await http.post(
    f"{WEBHOOK_URL}?wait=true&with_components=true",
    json={"flags": 32768, "components": components}
)
```

> À valider en premier au moment de l'implémentation : c'est le maillon critique de la redondance, il doit être testé avant tout le reste. Si le webhook ne supporte pas Components V2, replier sur un embed classique en fallback dégradé.

Pour éditer un message envoyé par webhook : `PATCH /webhooks/{id}/{token}/messages/{message_id}`. Stocker `discord_message_id` à la création.

---

## 8. Sticky message de statut

Salon : `1398625686301704323` (serveur `1394001780148535387`).

### Comportement

Un message permanent en bas du salon affichant l'état global, réposté automatiquement dès qu'un autre message apparaît en dessous.

- Le bot écoute `on_message` dans ce salon.
- Si le message n'est pas le sticky et que le sticky n'est plus le dernier message : supprimer l'ancien sticky, en reposter un nouveau.
- **Debounce de 5s** pour éviter le spam en cas de rafale de messages.
- Rafraîchissement automatique du contenu toutes les 2 min, ou immédiatement sur changement d'état.
- `hm:sticky:message_id` persiste l'ID pour survivre à un redémarrage.

### Contenu

Container avec l'état agrégé, une ligne par service, et une ActionRow avec deux boutons.

```
### <:verified2:...> All Systems Operational
-# Last checked <t:1787000000:R>
Moddy Bot        Operational
API              Operational
AltGuard         Operational
Feeds            Operational
[ Refresh ]  [ Status Page ]
```

### Bouton "Refresh"

View persistante, `custom_id` fixe : `hm:sticky:refresh`. Le bot doit l'enregistrer au démarrage via `register_persistent` (convention `PERSISTENT_VIEWS.md`), sinon le bouton est mort après chaque redéploiement.

Réponse **éphémère et détaillée** : au-delà de l'état agrégé, on déroule le contenu de `checks` de chaque service, la latence, la version, l'uptime, et l'âge du dernier heartbeat. Données lues directement depuis `hm:status:public` via Redis — pas d'appel HTTP interne, ça doit répondre même en cas de crise.

```python
class StickyStatusView(BaseView):
    def __init__(self):
        super().__init__()  # timeout=None hérité de BaseView
        container = ui.Container()
        container.add_item(ui.TextDisplay(header))
        container.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(ui.TextDisplay(services_block))
        row = ui.ActionRow()
        row.add_item(ui.Button(label="Refresh", custom_id="hm:sticky:refresh",
                               style=discord.ButtonStyle.secondary))
        row.add_item(ui.Button(label="Status Page", style=discord.ButtonStyle.link,
                               url="https://status.moddy.app"))
        container.add_item(row)
        self.add_item(container)
```

Rappel des conventions Moddy : hériter de `BaseView`, jamais de `ui.LayoutView` directement. Les selects spécialisés (`RoleSelect`, `ChannelSelect`…) doivent être dans un `ui.ActionRow`.

---

## 9. Commandes du bot

Groupe `/status`, réservé au staff (Discord Linked Roles ou rôle serveur).

| Commande | Effet |
|---|---|
| `/status incident` | Ouvre le modal de création d'incident |
| `/status update` | Ouvre le modal d'update sur l'incident actif |
| `/status resolve` | Ouvre le modal de résolution |
| `/status maintenance` | Ouvre le modal de maintenance planifiée |
| `/status check` | Affiche l'état détaillé (éphémère) |
| `/status sticky` | Force le repost du sticky message |

### Modal de création (Modals V2)

Maximum 5 composants top-level, chacun `Label` ou `TextDisplay`.

```python
class IncidentCreateModal(discord.ui.Modal, title="Create Incident"):
    incident_title = discord.ui.Label(
        text="Title",
        description="Shown as the incident headline",
        component=discord.ui.TextInput(
            style=discord.TextStyle.short,
            max_length=100,
            placeholder="Major Outage – Bot & API Unavailable",
        ),
    )
    message = discord.ui.Label(
        text="Message",
        description="Public description of the incident",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=1500,
        ),
    )
    level = discord.ui.Label(
        text="Severity",
        component=discord.ui.RadioGroup(
            options=[
                discord.SelectOption(label="Degraded Performance", value="degraded"),
                discord.SelectOption(label="Partial Outage",       value="partial_outage"),
                discord.SelectOption(label="Major Outage",         value="major_outage"),
            ],
        ),
    )
    affected = discord.ui.Label(
        text="Affected services",
        component=discord.ui.CheckboxGroup(
            options=[
                discord.CheckboxGroupOption(label="Moddy Bot", value="moddy-bot"),
                discord.CheckboxGroupOption(label="API",       value="moddy-api"),
                discord.CheckboxGroupOption(label="Dashboard", value="moddy-dashboard"),
                discord.CheckboxGroupOption(label="Website",   value="moddy-website"),
                discord.CheckboxGroupOption(label="AltGuard",  value="moddy-altguard"),
                discord.CheckboxGroupOption(label="Feeds",     value="moddy-feeds"),
            ],
            min_values=1, max_values=6, required=True,
        ),
    )
    notify = discord.ui.Label(
        text="Notify subscribers",
        component=discord.ui.CheckboxGroup(
            options=[discord.CheckboxGroupOption(label="Send email to subscribers")],
            min_values=0, max_values=1, required=False,
        ),
    )
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        payload = {
            "title":    self.incident_title.component.value,
            "message":  self.message.component.value,
            "level":    self.level.component.values[0],
            "affected": self.affected.component.values,
            "notify":   bool(self.notify.component.values),
            "author":   str(interaction.user),
        }
        await redis.publish("moddy:hm:command", json.dumps(
            {"action": "incident.create", "payload": payload}))
        await interaction.followup.send("Incident created.", ephemeral=True)
```

Points de vigilance issus de la doc Modals V2 :

- `TextInput.label` est déprécié — le texte affiché vient de `Label.text`.
- `disabled` est **interdit** dans les modals, erreur API.
- Récupération : `self.champ.component.value` (TextInput) / `.values` (Select, CheckboxGroup, RadioGroup).
- Le paramètre `row` est peu fiable en V2 : l'ordre suit l'ordre de déclaration.
- `RadioGroup` et `CheckboxGroup` nécessitent **discord.py ≥ 2.7**.

Les modals `update` et `resolve` sont plus courts (message + notify uniquement, l'incident actif est déduit du contexte).

### Canal de commande

Le bot ne parle jamais directement à Better Stack. Il publie sur Redis `moddy:hm:command`, le monitor consomme et exécute. Ça garde toute la logique d'incident en un seul endroit, et le token Better Stack n'existe que dans le monitor.

**Fallback si Redis est down :** le bot bascule sur un `POST /ingest/command` avec le `X-Health-Token`. Même contrat, autre transport.

---

## 10. Endpoint public

```
GET /v1/status
```

Sans authentification. Destiné au dashboard, au site, et à tout service tiers.

### Protections

| Mesure | Valeur |
|---|---|
| Rate limit | 60 req/min par IP (`slowapi` ou middleware maison sur Redis) |
| Cache | Réponse pré-calculée dans `hm:status:public`, TTL 30s |
| CORS | `moddy.app`, `dashboard.moddy.app`, `*` en lecture seule |
| Headers | `Cache-Control: public, max-age=30` |

Le calcul se fait dans la boucle de check, **pas à la requête**. L'endpoint ne fait que servir une clé Redis — il tient la charge et reste disponible même si tout le reste ramait.

### Réponse

```json
{
  "status": "partial_outage",
  "updated_at": "2026-08-24T19:42:11Z",
  "services": [
    { "id": "moddy-bot",      "name": "Moddy Bot", "status": "down",        "since": "2026-08-24T19:38:02Z" },
    { "id": "moddy-api",      "name": "API",       "status": "operational", "since": "2026-08-20T04:11:00Z" },
    { "id": "moddy-altguard", "name": "AltGuard",  "status": "operational", "since": "2026-08-20T04:11:00Z" },
    { "id": "moddy-feeds",    "name": "Feeds",     "status": "operational", "since": "2026-08-20T04:11:00Z" }
  ],
  "incident": {
    "id": "inc_20260824_1942",
    "type": "incident",
    "level": "partial_outage",
    "title": "Moddy Bot is offline",
    "message": "We are investigating an issue affecting the bot.",
    "affected": ["moddy-bot"],
    "started_at": "2026-08-24T19:42:00Z",
    "resolved_at": null,
    "url": "https://status.moddy.app/en/incident/1019848",
    "updates_count": 2,
    "last_update": {
      "at": "2026-08-24T19:55:00Z",
      "message": "We identified the issue and have deployed a fix."
    }
  },
  "maintenance": null
}
```

`incident` et `maintenance` valent `null` quand il n'y a rien en cours — le dashboard n'affiche la bannière que si le champ est non-null, et choisit sa couleur selon `level`.

### Variantes

| Route | Usage |
|---|---|
| `GET /v1/status` | Complet |
| `GET /v1/status/banner` | Minimal (`level`, `title`, `url`) — payload le plus léger pour le dashboard |
| `GET /health` | Liveness du monitor lui-même, 200 nu, sans dépendance |

---

## 11. Redondance et modes dégradés

Matrice de comportement, à traiter explicitement dans le code :

| Panne | Comportement attendu |
|---|---|
| Redis down | État en mémoire, alertes maintenues, resync au retour. Le monitor ne crash pas. |
| Bot Discord down | Bascule webhook automatique. C'est le scénario nominal de la redondance. |
| Discord entièrement down | Better Stack seul. Gestion de crise depuis l'interface Better Stack, récupérée par le poll. Messages Discord empilés dans `hm:notify:queue` et rejoués au retour. |
| Better Stack down | Discord seul. Retry avec backoff exponentiel. L'incident reste dans `hm:incident:active`, `bs_report_id` renseigné dès que possible. |
| Monitor down | Rien ne le détecte — **sauf** le heartbeat sortant vers Better Stack (ci-dessous). |
| Railway down | Tout est down. Rien à faire, mais le poll de `railway.statuspage` permet d'afficher la cause. |

### Surveiller le surveillant

Le monitor pousse un heartbeat vers un **Heartbeat monitor Better Stack** toutes les 60s (voir §6, dernière sous-section pour le format exact). Si le monitor meurt, Better Stack alerte directement par email/push. Sans ça, on a un système de surveillance dont la mort n'est constatée par personne.

Ajouter aussi une surveillance de la souscription webhook : si aucun event Better Stack n'est reçu depuis 24h alors qu'un incident a eu lieu, c'est probablement que la souscription a été désactivée après 10 échecs de livraison.

### Règles générales

- **Aucune exception ne remonte jusqu'à la boucle principale.** Chaque tâche est enveloppée, log et continue.
- **Backoff exponentiel** sur tous les appels externes, plafonné à 5 min.
- **SIGTERM géré proprement** : Railway redéploie souvent, il faut flush l'état vers Redis avant de sortir.
- **Idempotence** : chaque notification a une clé de déduplication (`hash(incident_id + update_index + canal)`) dans `hm:notify:sent`. Un double envoi lors d'un retry ne produit pas deux messages.

---

## 12. Configuration

```env
# Serveur
PORT=8080
HM_INGEST_TOKEN=<secret partagé avec les services>
HM_LOG_LEVEL=INFO

# Services surveillés
HM_SERVICES=moddy-bot,moddy-api,moddy-altguard,moddy-feeds
HM_CRITICAL_SERVICES=moddy-bot,moddy-api
HM_HEARTBEAT_TTL=60
HM_CHECK_INTERVAL=15
HM_FAILURE_THRESHOLD=3
HM_RECOVERY_THRESHOLD=2
HM_STARTUP_GRACE=90

# Redis
REDIS_URL=redis://default:xxx@redis.railway.internal:6379

# Better Stack
BETTERSTACK_TOKEN=
BETTERSTACK_API_BASE=https://uptime.betterstack.com/api/v2
BETTERSTACK_STATUS_PAGE_ID=237745
BETTERSTACK_INDEX_URL=https://status.moddy.app/index.json
BETTERSTACK_POLL_INTERVAL=300          # secours uniquement, le webhook est le chemin principal
BETTERSTACK_WEBHOOK_SECRET=            # placé en query string sur /ingest/betterstack
HM_BS_RESOURCE_MAP=moddy-bot:8720238,moddy-website:8720239,moddy-dashboard:8720240,moddy-api:8720241
HM_SELF_HEARTBEAT_URL=https://uptime.betterstack.com/api/v1/heartbeat/<token>
HM_SELF_HEARTBEAT_INTERVAL=60

# Discord
DISCORD_WEBHOOK_URL=
DISCORD_STATUS_CHANNEL_ID=1398625686301704323
DISCORD_GUILD_ID=1394001780148535387

# Public API
HM_PUBLIC_RATE_LIMIT=60/minute
HM_CORS_ORIGINS=https://moddy.app,https://dashboard.moddy.app
```

---

## 13. Arborescence

```
moddy-health-monitor/
├── app/
│   ├── main.py                 # FastAPI + lancement des boucles asyncio
│   ├── config.py               # Settings pydantic depuis l'env
│   ├── state.py                # Store Redis + fallback mémoire
│   │
│   ├── api/
│   │   ├── ingest.py           # POST /ingest/heartbeat, /ingest/command
│   │   ├── webhooks.py         # POST /ingest/betterstack (2xx immédiat, traitement en tâche de fond)
│   │   ├── public.py           # GET /v1/status, /v1/status/banner
│   │   └── health.py           # GET /health
│   │
│   ├── core/
│   │   ├── detector.py         # Machine à états, seuils, agrégation
│   │   ├── incident.py         # Cycle de vie, structure, historique
│   │   └── scheduler.py        # Boucles : check, poll BS, self-heartbeat, sticky
│   │
│   ├── integrations/
│   │   ├── betterstack.py      # Écriture API + poll index.json + anti-boucle
│   │   ├── discord_webhook.py  # Envoi/édition direct
│   │   └── redis_bus.py        # Pubsub vers/depuis le bot
│   │
│   └── render/
│       ├── components.py       # Construction du JSON Components V2
│       └── colors.py           # Palette, emojis, mapping niveaux
│
├── .env.example
├── Dockerfile
└── README.md
```

Côté bot Moddy, en parallèle :

```
modules/health_monitor.py       # Listener Redis, sticky, relais des messages
views/status_sticky.py          # StickyStatusView (BaseView, persistante)
modals/incident.py              # Modals V2 : create / update / resolve / maintenance
```

---

## 14. Ordre d'implémentation

Découpage en 5 étapes livrables indépendamment. L'étape 1 seule apporte déjà l'essentiel de la valeur.

### Étape 1 — Le cœur (le plus rentable)

1. `POST /ingest/heartbeat` + stockage Redis avec TTL
2. Boucle de détection + seuils + grace period
3. Notification webhook Discord (rendu Components V2)
4. `GET /v1/status` + `GET /health`
5. Heartbeat sortant vers Better Stack

À ce stade tu es alerté de toute panne et le dashboard peut afficher une bannière. **C'est 80% de la valeur.**

### Étape 2 — Better Stack en écriture

6. Création/update/résolution de status reports
7. Mapping service → resource_id

### Étape 3 — Bot Discord

8. Listener Redis côté bot + ACK
9. Bascule bot ↔ webhook
10. Sticky message + bouton Refresh persistant

### Étape 4 — Gestion de crise

11. Commandes `/status *` + Modals V2
12. File de rattrapage `hm:notify:queue`

### Étape 5 — Boucle de retour

13. `POST /ingest/betterstack` + souscription webhook depuis la status page
14. Anti-boucle (`hm:bs:owned`, `hm:bs:seen_updates`) — à écrire **en même temps** que le point 13, pas après
15. Poll `index.json` toutes les 5 min en réconciliation (rattrape une souscription webhook désactivée)

---

## 15. À corriger en amont sur la status page

Constats issus du `index.json` actuel, à régler avant de brancher le monitor dessus :

- **`Moddy Bot` est en `status: "not_monitored"`** alors que `aggregate_state` de la page est `operational`. La page affiche donc publiquement que tout va bien pendant que la ressource principale n'est plus surveillée. Le monitor du bot est en pause ou supprimé côté Uptime.
- **Timezone de la page : `America/Adak`** (UTC-9/-10) alors que les updates sont publiés en `Paris`. Les barres journalières sont décalées d'une dizaine d'heures. À passer en `Paris`.
- **Annonce périmée** : elle annonce une sortie officielle en juin 2026.
- **Ressources manquantes** : ni AltGuard ni Feeds n'existent sur la page. Les créer avant de brancher `HM_BS_RESOURCE_MAP`.
- **Nommage** : espaces en fin de `"Moddy Bot "` et `"Moddy Bot : "`, l'API rangée dans la section « Moddy Website », `"Internal API"` comme nom public.
- **`notify_subscribers: false` sur tous les updates historiques**, y compris les 49h de coupure du 3-5 août. À vérifier si c'était volontaire.

Dernier point, non technique : sur 90 jours d'historique, la quasi-totalité de l'indisponibilité vient d'un seul événement — 49h les 3-5 août pour un problème de facturation hébergeur. Les vraies pannes techniques totalisent environ une heure. Aucun système de monitoring n'aurait empêché la première. Une alerte de renouvellement de paiement dans un calendrier a plus de valeur sur l'uptime réel de Moddy que ce service entier.

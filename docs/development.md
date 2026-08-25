# Développement

## Installation

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio pyflakes
cp .env.example .env        # renseigner au minimum HM_INGEST_TOKEN
```

## Lancer

```bash
uvicorn app.main:app --reload --port 8080
```

Sans `REDIS_URL`, le monitor démarre en mémoire seule avec un warning : suffisant
pour développer, y compris la détection et les incidents.

## Tester

```bash
pytest              # 103 tests, ~3s
pytest tests/test_impact.py -v
python -m pyflakes app examples tests
```

`pytest.ini` active `asyncio_mode = auto` : pas besoin de décorer chaque test
async.

### Organisation

| Fichier | Couvre |
|---|---|
| `test_state.py` | Sémantique du store en mémoire, TTL, sets, listes, claim |
| `test_detector.py` | Seuils, grace period, transitions, payload public |
| `test_impact.py` | Les quatre règles produit et les garde-fous |
| `test_probe.py` | Sonde HTTP : ce qui est écrit, et ce que la détection en fait |
| `test_incident.py` | Ouverture, enrichissement, résolution, commandes staff |
| `test_notifier.py` | Chaîne de redondance, file de rattrapage, idempotence |
| `test_betterstack.py` | Parsing `index.json`, mapping ressources, anti-boucle |
| `test_api.py` | Contrats HTTP de bout en bout, via `TestClient` |

`tests/conftest.py` fixe les variables d'environnement **avant** tout import :
`get_settings()` est mis en cache, changer l'environnement après coup n'a aucun
effet.

### Conventions de test

Les doublures sont des objets simples (`StubNotifier`, `FakeBus`,
`FakeWebhook`), pas des mocks : elles décrivent le contrat attendu et cassent
franchement s'il change. Aucun test ne sort sur le réseau.

Le nom du test dit le comportement attendu, pas la fonction appelée :
`test_degraded_does_not_propagate` plutôt que `test_apply_2`.

## Essai manuel de bout en bout

Seuils raccourcis pour ne pas attendre les 3 cycles et les 60s de silence :

```bash
export HM_INGEST_TOKEN=dev HM_CHECK_INTERVAL=1 HM_FAILURE_THRESHOLD=1 \
       HM_RECOVERY_THRESHOLD=1 HM_MIN_SILENCE=0 HM_STARTUP_GRACE=0 \
       HM_HEARTBEAT_TTL=3 REDIS_URL=
uvicorn app.main:app --port 8080 &

beat () { curl -s -o /dev/null -X POST localhost:8080/ingest/heartbeat \
  -H "X-Health-Token: dev" -H 'Content-Type: application/json' \
  -d "{\"service\":\"$1\",\"status\":\"$2\"}"; }

for s in moddy-bot moddy-api moddy-altguard moddy-feeds; do beat $s ok; done
sleep 2 && curl -s localhost:8080/v1/status | python -m json.tool

# faire tomber l'API et observer la propagation
for i in 1 2 3; do beat moddy-api down; sleep 1; done
curl -s localhost:8080/v1/status | python -m json.tool
```

Attendu : `moddy-bot` passe `degraded` avec `impacted_by: ["moddy-api"]`, un
incident s'ouvre avec Website et Dashboard dans `affected`.

## Conventions de code

- Commentaires et docstrings **en français**, textes publics (Discord, status
  page) **en anglais**.
- Un commentaire explique un *pourquoi* non évident. Le *quoi* est dans le code.
- Aucune exception ne remonte jusqu'à une boucle : `core/scheduler.py` enveloppe
  chaque itération.
- Les noms de clés Redis vivent dans `app/keys.py`, jamais en dur ailleurs.
- Le store ne lève jamais. Un appelant n'a pas à gérer l'indisponibilité de
  Redis.
- Les intégrations sortantes prennent leur `httpx.AsyncClient` en injection : un
  seul pool pour tout le process.

## Ajouter un service surveillé

1. `HM_SERVICES=...,moddy-nouveau`
2. `HM_SERVICE_NAMES=...,moddy-nouveau:Nouveau` si le nom dérivé ne convient pas
3. Créer la ressource sur la status page, ajouter son ID à `HM_BS_RESOURCE_MAP`
4. Ajuster `HM_IMPACT_MAP` si le service en fait tomber d'autres, ou tombe avec
   d'autres
5. Brancher `HeartbeatClient` dans le service

Aucun code à modifier : le monitor n'a aucune logique par service.

## Ajouter un check dans un heartbeat

Rien à faire côté monitor. `checks` est un dictionnaire à clés libres, stocké et
itéré tel quel pour l'affichage.

## Modifier le rendu Discord

Un modèle, `render/model.py`, et deux renderers : `render/layout.py` pour le bot
(objets discord.py), `render/raw.py` pour le webhook (JSON brut). Toucher l'un
sans toucher l'autre casse `test_render_parity.py`, et c'est exactement le rôle
de ce test — les deux chemins ne doivent jamais diverger.

Les couleurs, icônes et libellés sont dans `render/theme.py` ; `render/colors.py`
garde la palette et le vocabulaire des niveaux, partagés avec le cœur.
`test_render.py` verrouille la forme contre le payload de référence.

Trois icônes sont autorisées, pas une de plus — voir
[discord.md](discord.md#sobriété-du-rendu).

## Travailler sur le bot sans token

`DISCORD_TOKEN` vide : le bot n'est pas construit, `publisher.enabled` reste
faux, et toute la chaîne bascule sur le webhook. Le monitor démarre, détecte et
sert `/v1/status` normalement. Les tests du bot (`test_publisher.py`,
`test_sticky.py`, `test_bot_views.py`) tournent sur des doublures : aucun ne
sort sur le réseau ni n'ouvre de gateway.

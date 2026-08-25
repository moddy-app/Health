# Documentation — Moddy Health Monitor

Documentation de référence du service. Elle décrit **le code tel qu'il est**, pas
seulement l'intention. Quand un comportement diverge de la spec d'origine, la
divergence est nommée et justifiée à l'endroit concerné.

## Par où commencer

| Vous voulez… | Lisez |
|---|---|
| Comprendre la forme générale du service | [architecture.md](architecture.md) |
| Brancher un service Moddy sur le monitor | [heartbeat.md](heartbeat.md) |
| Savoir quand et pourquoi une alerte part | [detection.md](detection.md) |
| Suivre la vie d'un incident | [incidents.md](incidents.md) |
| Travailler sur les messages Discord | [discord.md](discord.md) |
| Toucher à la status page | [betterstack.md](betterstack.md) |
| Consommer l'API depuis le dashboard | [api.md](api.md) |
| Régler une variable d'environnement | [configuration.md](configuration.md) |
| Inspecter l'état en production | [redis.md](redis.md) |
| Déployer, ou diagnostiquer une panne | [operations.md](operations.md) |
| Développer et tester | [development.md](development.md) |

[IMPLEMENTATION.md](IMPLEMENTATION.md) conserve la spec d'origine telle qu'elle a
été écrite. C'est une **référence figée** : elle n'est pas mise à jour au fil du
code, les autres pages font foi.

## Le service en trois phrases

Les services Moddy poussent leur état vers le monitor toutes les 20 secondes. Le
monitor n'appelle jamais personne pour savoir comment ça va : l'absence de
signal *est* le signal. Quand un service tombe, il alerte sur Discord, publie sur
la status page Better Stack, et sert un JSON public que le dashboard affiche en
bannière.

## Les cinq décisions qui expliquent tout le reste

1. **Le monitor ne dépend de rien de ce qu'il surveille.** Pas de PostgreSQL, pas
   d'appel vers l'API Moddy. Redis pour la persistance, avec fallback mémoire.
2. **Le service décide lui-même de son état.** Il connaît ses dépendances mieux
   que le monitor, qui n'a aucune logique par service.
3. **Une panne Redis n'empêche jamais une alerte de partir.** Tout l'état est
   mirroré en mémoire et resynchronisé au retour.
4. **Si le bot est mort, l'alerte part quand même**, par webhook — c'est
   précisément le cas où on en a le plus besoin. Et le bot est une application
   Discord **dédiée**, distincte de Moddy : il survit à une panne de Moddy.
5. **Le monitor est lui-même surveillé**, par un heartbeat sortant vers Better
   Stack. Sinon on aurait un système de surveillance dont la mort n'est
   constatée par personne.

Skynet améliore du code que vous possédez déjà, ou du code qu'il rédige pour vous. Skynet, c'est Optimize Anything : chaque exécution optimise un dépôt. Vous lui fournissez un dépôt, un ensemble de cas et un évaluateur. Skynet propose de nouvelles versions du dépôt, note chacune avec votre évaluateur et conserve la meilleure version.

## Ce que Skynet optimise

Chaque exécution optimise un dépôt. Vous pouvez commencer de deux façons :

| Départ                  | Ce qui se passe                                                                                          |
| ----------------------- | -------------------------------------------------------------------------------------------------------- |
| Utiliser un dépôt       | Skynet modifie les fichiers des chemins que vous autorisez, dans un dépôt GitHub que vous connectez.      |
| Créer un nouveau dépôt  | Vous fournissez une tâche et des données. Skynet crée un nouveau dépôt GitHub dans votre compte et rédige une première solution. |

Le code du dépôt peut être de toute nature : un pipeline de prompts, un harnais d'agent, un script ou une bibliothèque. Il peut utiliser n'importe quelle bibliothèque.

Chaque exécution suit le même contrat. Votre évaluateur définit `score(repo_path, case)`. Il renvoie un score et un retour écrit. L'optimiseur lit ce retour pour améliorer la version suivante.

> [!NOTE]
> Chaque version s'exécute dans une sandbox. L'agent de code qui rédige les versions ne peut voir ni le source de l'évaluateur ni les valeurs attendues.

## Un seul produit, plusieurs moteurs

**Auto** est l'option par défaut pour chaque exécution. Il essaie GEPA, AutoResearch, Meta-Harness, AutoSaddler et ShinkaEvolve, puis poursuit GEPA à partir de la meilleure version. Vous pouvez aussi choisir un seul moteur : GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler ou ShinkaEvolve. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization) et [Moteurs](/docs/concepts/engines).

## Déroulement d'une exécution

1. Vous décrivez la tâche, puis choisissez un dépôt ou demandez à Skynet d'en créer un.
2. Vous fournissez les cas. Skynet les répartit en ensembles d'entraînement, de validation et de test.
3. Vous choisissez un optimiseur, les modèles et un budget.
4. Vous écrivez l'évaluateur. Une vérification de la configuration l'exécute sur la version de départ.
5. Skynet lance l'optimisation et affiche les résultats.

Skynet sélectionne les versions sur les cas de validation. Il indique le score final sur les cas de test. Les moteurs ne voient jamais les cas de test.

## Par où commencer

| Si vous voulez                              | Lisez                                                        |
| ------------------------------------------- | ------------------------------------------------------------ |
| Lancer votre première exécution             | [Démarrage rapide](/docs/quickstart)                         |
| Comprendre le fonctionnement de l'optimisation | [Fonctionnement de l'optimisation](/docs/concepts/optimization) |
| Choisir un moteur                           | [Moteurs](/docs/concepts/engines)                            |
| Préparer vos données                        | [Jeux de données et répartitions](/docs/concepts/datasets)   |
| Écrire un évaluateur                        | [Métriques et évaluateurs](/docs/concepts/scorers)           |
| Remplir l'assistant                         | [Lancer une exécution](/docs/guides/submit)                  |
| Lire vos résultats                          | [Résultats](/docs/guides/results)                            |
| Comprendre les coûts                        | [Facturation](/docs/concepts/billing)                        |
| Utiliser Skynet depuis un agent             | [MCP](/docs/mcp)                                             |
| Appeler Skynet depuis votre code            | [API](/docs/api)                                             |
| Consulter les limites                       | [Limites](/docs/limits)                                      |

> [!TIP]
> Commencez avec un petit jeu de données et les paramètres par défaut. Rédigez un retour d'évaluateur précis. Un bon retour donne à l'optimiseur plus de matière que le score seul.

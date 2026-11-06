Skynet améliore un artefact que vous possédez déjà. Vous lui fournissez une version de départ, un ensemble de cas et une métrique. Skynet propose de nouvelles versions, exécute chacune d'elles sur vos cas, note la sortie avec votre métrique et conserve la meilleure version.

## Ce que Skynet optimise

Skynet travaille sur deux types d'artefacts :

| Type d'artefact | Ce qui change                                                       |
| --------------- | ------------------------------------------------------------------- |
| Dépôt | Les fichiers des chemins que vous autorisez |
| Programme DSPy  | Les instructions des prédicteurs d'un `dspy.Module`                 |

Chaque exécution suit le même contrat. Votre code d'exécution définit `run(artifact, input)`. Votre métrique définit `score(input, output, expected=None)`. La métrique renvoie un score et un retour écrit. L'optimiseur lit ce retour pour améliorer la version suivante.

> [!NOTE]
> Votre code d'exécution et votre métrique s'exécutent dans une sandbox, dans des processus séparés. Le code d'exécution ne peut voir ni le source de la métrique ni les valeurs attendues.

## Un seul produit, plusieurs moteurs

Skynet, c'est Optimize Anything. Le type d'artefact détermine les moteurs qui peuvent travailler dessus :

- Les dépôts utilisent les moteurs natifs : GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler et ShinkaEvolve.
- Les artefacts Programme DSPy utilisent les optimiseurs DSPy : DSPy GEPA, DSPy MIPROv2 et DSPy BootstrapFewShot.

Sur un programme DSPy, **Auto** utilise DSPy GEPA. Sur un dépôt, vous choisissez un moteur natif. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization) et [Moteurs](/docs/concepts/engines).

## Déroulement d'une exécution

1. Vous décrivez la tâche et définissez la version de départ.
2. Vous fournissez les cas. Skynet les répartit en ensembles d'entraînement, de validation et de test.
3. Vous choisissez un optimiseur, les modèles et un budget.
4. Vous écrivez le code d'exécution et la métrique. Une vérification du contrat les exécute sur des cas d'exemple.
5. Skynet lance l'optimisation et affiche les résultats.

Skynet sélectionne les versions sur les cas de validation. Il indique le score final sur les cas de test. Les moteurs ne voient jamais les cas de test.

## Par où commencer

| Si vous voulez                              | Lisez                                                        |
| ------------------------------------------- | ------------------------------------------------------------ |
| Lancer votre première exécution             | [Démarrage rapide](/docs/quickstart)                         |
| Comprendre le fonctionnement de l'optimisation | [Fonctionnement de l'optimisation](/docs/concepts/optimization) |
| Choisir un moteur                           | [Moteurs](/docs/concepts/engines)                            |
| Préparer vos données                        | [Jeux de données et répartitions](/docs/concepts/datasets)   |
| Écrire une métrique                         | [Métriques et évaluateurs](/docs/concepts/scorers)           |
| Remplir l'assistant                         | [Lancer une exécution](/docs/guides/submit)                  |
| Lire vos résultats                          | [Résultats](/docs/guides/results)                            |
| Comprendre les coûts                        | [Facturation](/docs/concepts/billing)                        |
| Utiliser Skynet depuis un agent             | [MCP](/docs/mcp)                                             |
| Appeler Skynet depuis votre code            | [API](/docs/api)                                             |
| Consulter les limites                       | [Limites](/docs/limits)                                      |

> [!TIP]
> Commencez avec un petit jeu de données et les paramètres par défaut. Rédigez un retour de métrique précis. Un bon retour donne à l'optimiseur plus de matière que le score seul.

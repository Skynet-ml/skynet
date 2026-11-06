Skynet, c'est Optimize Anything : un seul produit qui améliore un programme DSPy ou un dépôt selon votre métrique. Le type d'artefact détermine les moteurs qui peuvent travailler dessus.

## Le contrat commun

Chaque exécution comporte deux fichiers. `run.py` exécute l'artefact sur un cas. `metric.py` note la sortie.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact` est la version actuelle que propose l'optimiseur.
- `input` est un dict qui contient les champs d'entrée d'un cas.
- `expected` est la valeur attendue du cas, ou `None` si le cas n'en a pas.
- `score` renvoie une paire : un nombre, et un dict contenant une chaîne `"feedback"`.

Le module d'aide `skynet` fournit `llm`, `Image` et `log`. Un appel à `llm` depuis le code d'exécution va au modèle de tâche. Un appel à `llm` depuis la métrique va au modèle de la métrique.

> [!NOTE]
> Le code d'exécution tourne dans son propre processus au sein de la sandbox. Il ne peut voir ni le source de la métrique, ni la valeur attendue, ni la clé de la métrique.

Lisez [Métriques et évaluateurs](/docs/concepts/scorers) pour le contrat complet de la métrique.

## Types d'artefacts et moteurs

| Type d'artefact | Moteurs                                                                      |
| --------------- | ---------------------------------------------------------------------------- |
| Dépôt | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| Programme DSPy  | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

Les moteurs se répartissent en deux familles :

- Les **moteurs natifs** traitent l'artefact comme une boîte noire. Ils fonctionnent sur les dépôts.
- Les **optimiseurs DSPy** travaillent à l'intérieur d'un programme DSPy. Ils ne fonctionnent que sur les artefacts Programme DSPy.

DSPy MIPROv2 et DSPy BootstrapFewShot ont besoin de paires (des cas avec des sorties attendues). Avec une entrée unique, un programme DSPy ne peut utiliser qu'Auto et DSPy GEPA.

## Dépôts

Les moteurs natifs modifient les fichiers des chemins que vous autorisez. Votre métrique note chaque version du dépôt.

Les six moteurs natifs utilisent des méthodes de recherche différentes :

- **GEPA** fait évoluer les versions par réflexion et conserve un front de Pareto.
- **Best-of-N** demande au modèle de réflexion des propositions indépendantes et conserve la meilleure.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** et **ShinkaEvolve** utilisent un agent de code pour proposer des versions.

Lisez [Moteurs](/docs/concepts/engines) pour le détail de chaque moteur et de ses paramètres.

## Artefacts Programme DSPy

Un programme DSPy est un type d'artefact. Votre `run.py` construit un `dspy.Module` au niveau du module et l'appelle dans `run`. Si le fichier contient plus d'un module, nommez `program` celui à optimiser.

Voici ce qui se passe quand un optimiseur DSPy s'exécute :

1. Skynet trouve votre module et enveloppe votre fonction `run` autour de lui. Chaque cas passe par votre propre `run`.
2. L'optimiseur DSPy compile le programme sur les cas d'entraînement.
3. Il sélectionne sur les cas de validation.
4. Le gagnant est le texte d'instruction de chaque prédicteur, plus l'état compilé complet du module. Cet état inclut les démonstrations few-shot.

Les scores finaux mesurent le programme même que Skynet enregistre.

| Optimiseur            | Lit le texte de retour   |
| --------------------- | ------------------------ |
| DSPy GEPA             | Oui                      |
| DSPy MIPROv2          | Non, le score uniquement |
| DSPy BootstrapFewShot | Non, le score uniquement |

DSPy BootstrapFewShot n'utilise pas de modèle de réflexion.

> [!TIP]
> Le modèle de réflexion DSPy ne voit que le texte de retour renvoyé par votre métrique. Il ne voit jamais le source de la métrique.

## Auto

**Auto** est le choix par défaut dans l'assistant.

- **Dépôt** : Auto n'est pas disponible. Choisissez un moteur natif.
- Sur un programme DSPy, Auto exécute DSPy GEPA.

Auto lit le texte de feedback de votre métrique.

## Quel moteur choisir

| Votre situation                                                   | À utiliser                                                              |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Vous avez du code dans un dépôt GitHub | Un dépôt. Choisissez un moteur natif. |
| Vous avez un programme DSPy                                       | Un artefact Programme DSPy. Commencez avec Auto.                        |
| Vous voulez des démonstrations few-shot choisies dans vos données | Un artefact Programme DSPy avec DSPy BootstrapFewShot ou DSPy MIPROv2   |

> [!WARNING]
> GEPA, DSPy GEPA et Auto apprennent du texte de retour. Si votre retour est toujours la même chaîne, ces optimiseurs disposent de moins d'informations.

## Pages associées

- [Moteurs](/docs/concepts/engines)
- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Métriques et évaluateurs](/docs/concepts/scorers)
- [Lancer une exécution](/docs/guides/submit)

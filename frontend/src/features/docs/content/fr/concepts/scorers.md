La métrique indique à Skynet la qualité d'une sortie. C'est une fonction Python dans `metric.py`. Tous les moteurs appellent la même fonction. Cette page présente le contrat, la valeur de retour et des exemples.

## Le contrat

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument   | Ce qu'il contient                                                  |
| ---------- | ------------------------------------------------------------------ |
| `input`    | Un dict avec les champs d'entrée du cas                            |
| `output`   | Ce que votre `run(artifact, input)` a renvoyé pour le cas          |
| `expected` | La valeur attendue du cas, ou `None` si le cas n'en a pas          |

Si votre fonction ne prend que deux arguments, Skynet appelle `score(input, output)`.

Pour un programme DSPy, Skynet adapte l'appel de métrique DSPy à cette même fonction. Vous écrivez une seule métrique pour tous les types d'artefacts. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization).

## La valeur de retour

`score` doit renvoyer une paire (un tuple ou une liste de deux éléments) :

1. **Le score.** Un nombre. L'optimiseur cherche à l'augmenter.
2. **Les informations annexes.** Un dict. Il doit contenir une clé `"feedback"` avec une chaîne comme valeur.

Les informations annexes peuvent aussi contenir une clé `"scores"`. Elle associe des noms à des nombres, par exemple `{"format": 1.0, "accuracy": 0.5}`.

Si la valeur de retour a une autre forme, la vérification du contrat échoue à **Valeur de retour de la métrique**.

| Valeur de retour                                       | Valide                                        |
| ------------------------------------------------------ | --------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Oui                                           |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Oui                                           |
| `0.8`                                                  | Non. Il n'y a pas d'informations annexes.     |
| `(0.8, "Close.")`                                      | Non. Les informations annexes ne sont pas un dict. |
| `(0.8, {})`                                            | Non. Il n'y a pas de chaîne de retour.        |
| `("0.8", {"feedback": "OK"})`                          | Non. Le score est une chaîne.                 |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Non. Un score nommé n'est pas un nombre.      |

## Rédiger un retour utile

GEPA, DSPy GEPA et Auto lisent le texte de retour pour écrire la version suivante. Dites ce qui ne va pas et à quoi ressemble une sortie correcte.

| Retour faible        | Retour utile                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------- |
| « Faux. »            | « Attendu 'billing', obtenu 'other'. Le message mentionne un prélèvement. »               |
| « Mauvais format. »  | « La sortie n'est pas un JSON valide. Il y a une virgule en trop après le dernier champ. » |

> [!NOTE]
> L'optimiseur ne voit jamais le source de votre métrique. Avec un optimiseur DSPy, le modèle de réflexion ne voit que le texte de retour. Vos valeurs attendues restent du côté de la métrique.

## Exemples

### Correspondance exacte

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` est un dict indexé par le nom de votre colonne de sortie. Lisez [Jeux de données et répartitions](/docs/concepts/datasets).

### Plusieurs vérifications

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### Modèle juge

La métrique peut appeler un modèle. Importez `llm` depuis le module d'aide `skynet`. Un appel depuis la métrique va au modèle de la métrique.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

La métrique définit son modèle dans son propre appel à `llm()`. Les quatre arguments présentés sont facultatifs. Sans `model=`, la métrique utilise le modèle de réflexion.

## Précision, vitesse et coût

Une métrique peut combiner la précision avec le temps d'exécution mesuré et le coût du modèle. Définissez `REWARD_SPEC` au niveau du module et renvoyez la valeur de `reward`.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` doit contenir `accuracy`, `speed` et `cost`. Chaque poids vaut 0 ou plus, et ils ne peuvent pas tous valoir 0. Skynet les normalise pour que leur somme fasse 1.
- `speed.reference` est en millisecondes. `cost.reference` est en dollars américains. Les deux doivent être positifs.
- Sans `accuracy.reference`, la précision est un score de 0 à 1. Avec cette valeur, la précision est une erreur : plus elle est basse, mieux c'est.

Skynet mesure lui-même le temps et le coût. Le code d'exécution ne peut pas les modifier. Si Skynet ne peut pas mesurer le temps ou le coût, cette composante vaut 0 et le retour le signale.

La métrique peut aussi lire directement les mesures avec `observation()` et `service_cost()` depuis `skynet`.

## Limites

| Élément                               | Valeur                                      |
| ------------------------------------- | ------------------------------------------- |
| Délai d'expiration de la métrique     | 60 secondes par défaut, 600 au maximum      |
| Exemples de la vérification du contrat | 3 cas                                      |

## Pages associées

- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Moteurs](/docs/concepts/engines)
- [Lancer une exécution](/docs/guides/submit)

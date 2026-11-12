L'évaluateur indique à Skynet la qualité d'une version de votre dépôt. C'est une fonction Python dans `scorer.py`. Tous les moteurs appellent la même fonction. Cette page présente le contrat, la valeur de retour et des exemples.

## Le contrat

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | Ce qu'il contient                                                                |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Le dossier d'une version du dépôt, après l'exécution de la commande d'installation |
| `case`      | Un dict. `case["input"]` contient les champs d'entrée. `case["expected"]` contient la valeur attendue, ou `None` |

Si votre fonction ne prend qu'un argument, Skynet appelle `score(repo_path)`. Utilisez cette forme quand l'évaluateur lance ses propres tests et n'a pas besoin de cas.

L'évaluateur s'exécute dans la sandbox, dans le dossier de la version. Il peut importer du code du dépôt, lancer une commande ou appeler `serve()` depuis `serve.py`. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization).

## La valeur de retour

`score` doit renvoyer un score avec un retour. Ces formes sont valides :

1. Une paire : un nombre, et une chaîne de retour ou un dict avec une clé `"feedback"`.
2. Un dict avec un nombre sous `"score"` et une chaîne sous `"feedback"`.

Le retour doit être une chaîne non vide.

Les informations annexes peuvent aussi contenir une clé `"scores"`. Elle associe des noms à des scores nommés. Chaque score nommé a son propre score et son propre retour, par exemple `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

Les informations annexes peuvent aussi contenir une clé `"cases"` de même forme. Chaque entrée est le résultat d'un cas. Chaque moteur utilise chaque cas comme un objectif distinct.

Si la valeur de retour a une autre forme, la vérification échoue et affiche la raison.

| Valeur de retour                                                                | Valide                                 |
| ------------------------------------------------------------------------------- | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Oui                                    |
| `(0.8, "Close. Missing the date.")`                                             | Oui                                    |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Oui                         |
| `0.8`                                                                           | Non. Il n'y a pas de retour.           |
| `(0.8, {})`                                                                     | Non. Il n'y a pas de chaîne de retour. |
| `("0.8", {"feedback": "OK"})`                                                   | Non. Le score est une chaîne.          |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | Non. Un score nommé n'est pas un nombre. |

## Rédiger un retour utile

GEPA et Auto lisent le texte de retour pour écrire la version suivante. Dites ce qui ne va pas et à quoi ressemble une sortie correcte.

| Retour faible        | Retour utile                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------- |
| « Faux. »            | « Attendu 'billing', obtenu 'other'. Le message mentionne un prélèvement. »               |
| « Mauvais format. »  | « La sortie n'est pas un JSON valide. Il y a une virgule en trop après le dernier champ. » |

> [!NOTE]
> L'agent de code ne voit jamais le source de votre évaluateur, les valeurs attendues ni les secrets de l'exécution. Il ne voit que les scores et le texte de retour.

## Exemples

### Correspondance exacte

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` est un dict dont la clé est le nom de votre colonne de sortie. Lisez [Jeux de données et répartitions](/docs/concepts/datasets).

### Lancer les tests du dépôt

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Un modèle comme juge

L'évaluateur peut appeler un modèle. Importez `llm` depuis le module d'aide `skynet`. L'appel passe par la passerelle de modèles de Skynet vers le modèle de l'évaluateur de l'exécution.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

L'évaluateur peut aussi utiliser n'importe quel client compatible OpenAI avec les variables d'environnement `OPENAI_BASE_URL`, `APP_API_KEY` et `APP_MODEL`. La passerelle n'accepte que le modèle de l'évaluateur de l'exécution.

## Appels de modèle et secrets

- Les appels sur les clés de Skynet sont décomptés du budget de l'exécution. Avec votre propre clé (BYOK), l'appel passe par la clé de votre fournisseur, sans frais de modèle Skynet. Lisez [Facturation](/docs/concepts/billing).
- Les secrets de l'exécution arrivent à l'évaluateur sous forme de variables d'environnement. Le nom de la variable est le nom du secret, par exemple `os.environ["SEARCH_API_KEY"]`.
- Skynet masque les valeurs des secrets dans la sortie de l'évaluateur et dans les journaux.

## Limites

| Élément                              | Valeur                                 |
| ------------------------------------ | -------------------------------------- |
| Délai d'expiration de l'évaluateur   | 60 secondes par défaut, 600 au maximum |

## Pages associées

- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Moteurs](/docs/concepts/engines)
- [Lancer une exécution](/docs/guides/submit)

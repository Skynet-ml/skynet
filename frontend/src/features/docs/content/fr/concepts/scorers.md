L'évaluateur indique à Skynet la qualité d'une version de votre dépôt. C'est une fonction Python dans `scorer.py`. Tous les moteurs appellent la même fonction. Cette page présente le contrat, la valeur de retour et des exemples.

## Le contrat

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argument    | Ce qu'il contient                                                                |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Un dossier qui contient seulement vos fichiers de métrique. Il ne contient aucun autre fichier du dépôt ni aucun code de la version |
| `case`      | Un dict. `case["input"]` contient les champs d'entrée. `case["expected"]` contient la valeur attendue, ou `None` |
| `output`    | Ce que l'entrée `[run]` a renvoyé pour le cas, ou `None` si le fichier n'a pas de `[run]` |

Le fichier `optimize.toml` du dépôt doit avoir une section `[run]` ou une étape `[target.<name>]`. L'`entry` de `[run]` exécute une version sur un cas, dans sa propre boîte de sandbox. Skynet refuse un fichier qui n'a ni l'une ni l'autre, car chaque version recevrait le même score.

- Une entrée de fonction, par exemple `entry = "serve.py:serve"`, reçoit `case["input"]` comme argument. `output` est sa valeur de retour.
- Une entrée de commande, par exemple `entry = ["python", "-m", "pytest", "-q"]`, reçoit l'entrée en JSON sur stdin. `output` est `{"exit_code": ..., "stdout": "..."}`.

L'évaluateur s'exécute dans une boîte de sandbox séparée. Cette boîte contient seulement vos fichiers de métrique. L'évaluateur ne peut ni importer ni exécuter le code de la version. Il voit la version seulement à travers `output`, ou à travers `call()` vers une étape `[target.<name>]`. Importez `call` avec `from skynet import call`. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization).

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

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` est un dict dont la clé est le nom de votre colonne de sortie. Lisez [Jeux de données et répartitions](/docs/concepts/datasets).

### Lancer les tests du dépôt

L'entrée `[run]` lance les tests. L'évaluateur lit le code de sortie et la sortie des tests.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

Quand l'entrée `[run]` échoue sur un cas, ce cas n'a pas de score. Si l'entrée ne démarre pas, dépasse le délai ou si un signal l'arrête, le retour est `Run failed: …`, et Skynet n'appelle pas l'évaluateur. Un code de sortie non nul n'est pas un échec : l'évaluateur le reçoit dans `output`. Une version avec un tel cas n'a pas de score. Elle ne peut pas être la meilleure version, n'atteint aucun `[outcome] target` et n'ouvre aucune pull request. Si un échec doit compter comme un nombre, interceptez l'échec dans votre programme, écrivez une sortie et laissez l'évaluateur la noter. Lisez les [règles du dépôt](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### Un modèle comme juge

L'évaluateur peut appeler un modèle. Importez `llm` depuis le module d'aide `skynet`. L'appel passe par la passerelle de modèles de Skynet vers le modèle de l'évaluateur de l'exécution.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

L'évaluateur peut aussi utiliser n'importe quel client compatible OpenAI avec les variables d'environnement `OPENAI_BASE_URL`, `APP_API_KEY` et `APP_MODEL`. La passerelle n'accepte que le modèle de l'évaluateur de l'exécution.

## Appels de modèle et secrets

- Les appels sur les clés de Skynet sont décomptés du budget de l'exécution. Avec votre propre clé (BYOK), l'appel passe par la clé de votre fournisseur, sans frais de modèle Skynet. Lisez [Facturation](/docs/concepts/billing).
- L'évaluateur ne reçoit pas les secrets de l'exécution sous forme de variables d'environnement. Un secret arrive à l'évaluateur seulement par une table `[services.<name>]` que `[metric] services` liste. Écrivez le secret dans les `headers` de la table sous la forme `${NAME}`. L'évaluateur envoie ses requêtes à l'URL de `SKYNET_SERVICE_<NAME>_URL`. Skynet ajoute le secret hors de la sandbox.
- Skynet masque les valeurs des secrets dans la sortie de l'évaluateur et dans les journaux.

## Limites

| Élément                              | Valeur                                 |
| ------------------------------------ | -------------------------------------- |
| Délai d'expiration de l'évaluateur   | 60 secondes par défaut, 600 au maximum |

## Pages associées

- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Moteurs](/docs/concepts/engines)
- [Lancer une exécution](/docs/guides/submit)

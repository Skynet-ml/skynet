L'API HTTP de Skynet permet à des scripts et à des outils de faire ce que fait l'application web : lancer des exécutions, les suivre et lire leurs résultats. Cette page explique comment signer les requêtes, où trouver la référence complète, et donne un exemple complet.

> [!TIP]
> Vous utilisez un agent IA comme Claude Code, Codex ou Cursor ? Connectez-le plutôt via [MCP](/docs/mcp). L'agent lance et suit alors les exécutions pour vous, avec un plafond de dépenses pour chaque exécution.

## URL de base

Envoyez chaque requête à :

```text
https://api.skynetml.com
```

## Authentification

L'API utilise des jetons d'API.

1. Ouvrez les **Paramètres** dans Skynet, puis l'onglet **API**.
2. Cliquez sur **Générer un jeton**.
3. Copiez immédiatement le jeton. Skynet n'affiche le jeton complet qu'une seule fois.

Envoyez le jeton dans l'en-tête `Authorization` de chaque requête :

```http
Authorization: Bearer skyd_...
```

- Les jetons commencent par `skyd_`.
- Chaque utilisateur possède au plus un jeton valide. Un nouveau jeton désactive l'ancien.
- Cliquez sur **Révoquer le jeton** pour désactiver un jeton.

> [!WARNING]
> Traitez le jeton comme un mot de passe. Tenez-le à l'écart du code source et des journaux. Lisez [Sécurité](/docs/security) pour savoir comment Skynet stocke les jetons.

## Référence interactive

La référence complète se trouve sur [api.skynetml.com/scalar](https://api.skynetml.com/scalar). Elle liste chaque point de terminaison public, ses champs et ses réponses. Vous pouvez envoyer des requêtes de test depuis cette page.

La référence lit le document OpenAPI à l'adresse `https://api.skynetml.com/openapi.public.json`. Utilisez ce fichier pour générer un client dans votre langage.

## Principaux points de terminaison

| Méthode et chemin                               | Rôle                                                              |
| ----------------------------------------------- | ----------------------------------------------------------------- |
| `GET /models`                                   | Liste les modèles que vous pouvez utiliser.                       |
| `GET /optimizers`                               | Liste les optimiseurs adaptés à une configuration.                |
| `GET /blackbox/engines`                         | Liste les moteurs natifs et indique si chacun est disponible.     |
| `POST /optimize` | Lance une exécution pour un programme DSPy. |
| `POST /blackbox/run`                            | Lance une exécution sur un dépôt GitHub.                          |
| `GET /optimizations`                            | Liste vos exécutions.                                             |
| `GET /optimizations/{optimization_id}`          | Renvoie une exécution : statut, progression, journaux et résultat. |
| `GET /optimizations/{optimization_id}/artifact` | Renvoie la meilleure version trouvée par l'exécution.             |
| `POST /optimizations/{optimization_id}/cancel`  | Arrête une exécution.                                             |
| `POST /serve/{optimization_id}`                 | Exécute la meilleure version sur une nouvelle entrée.             |
| `GET /health`                                   | Indique si le service est disponible.                             |

## Lancer une exécution

`POST /optimize` accepte la même configuration que l'assistant de soumission. L'exemple améliore un programme DSPy qui classe des messages de support selon des étiquettes.

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

Champs importants :

- `task.artifact.kind` vaut `dspy_program`.
- `data.mode` vaut `pairs` (des entrées avec des sorties attendues) ou `single` (exactement un cas).
- `optimizer.choice` vaut `auto` ou le nom d'un optimiseur renvoyé par `GET /optimizers`.
- `max_cost_cents` est le plafond de dépenses de cette exécution, en cents de dollar américain.
- `is_private` vaut `false` par défaut. Passez-le à `true` pour garder l'exécution privée.

Lisez [Métriques et évaluateurs](/docs/concepts/scorers) pour les règles de `run_code` et `metric_code`.

### Réponses

| Statut | Signification                                                                                                                                         |
| ------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet a mis l'exécution en file d'attente. Le corps contient `optimization_id` et `status`.                                                          |
| `202`  | La vérification de la configuration est toujours en cours. Attendez le nombre de secondes indiqué dans l'en-tête `Retry-After`. Renvoyez ensuite le même corps avec la même `Idempotency-Key`. |
| `402`  | Votre solde est insuffisant.                                                                                                                          |
| `409`  | Vous avez atteint un quota, ou la vérification de la configuration n'est plus à jour.                                                                 |
| `422`  | La requête ou le code de l'espace de travail ne convient pas à l'optimiseur.                                                                          |

> [!NOTE]
> Envoyez toujours un en-tête `Idempotency-Key`. Si vous renvoyez la même requête avec la même clé, Skynet renvoie la première exécution. Il ne lance pas de seconde exécution.

## Suivre une exécution

Interrogez l'exécution jusqu'à ce que son statut soit final :

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Le champ `status` prend l'une des valeurs suivantes :

| Statut                             | Final |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | Non   |
| `paused`                           | Non   |
| `success`                          | Oui   |
| `failed`, `cancelled`, `stopped`   | Oui   |

Interrogez toutes les 10 à 30 secondes. Une exécution peut durer de quelques minutes à plusieurs heures.

La [référence de l'API](https://api.skynetml.com/scalar) liste chaque champ de la réponse d'une exécution.

## Utiliser le résultat

Quand le statut est `success`, récupérez la meilleure version :

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

Pour exécuter la meilleure version sur une nouvelle entrée, envoyez `POST /serve/{optimization_id}`. Lisez [Résultats](/docs/guides/results) pour la signification de chaque score.

## Limites

L'API applique les mêmes quotas, limites de débit et règles de facturation que l'application web. Lisez [Limites](/docs/limits) et [Facturation](/docs/concepts/billing).

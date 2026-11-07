Skynet, c'est Optimize Anything : chaque exécution optimise un dépôt. Un moteur propose de nouvelles versions du dépôt, votre évaluateur note chaque version, et Skynet conserve la meilleure.

## Le contrat commun

Chaque exécution comporte un dépôt et un évaluateur. L'évaluateur est une fonction Python qui reçoit le chemin d'une version du dépôt et un cas.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path` est le dossier d'une version du dépôt, après l'exécution de la commande d'installation.
- `case` est un dict. `case["input"]` contient les champs d'entrée d'un cas. `case["expected"]` contient la valeur attendue, ou `None` si le cas n'en a pas.
- `score` renvoie une paire : un nombre, et un dict contenant une chaîne `"feedback"`.

Le dépôt contient à sa racine un fichier `serve.py` avec `def serve(input)`. L'évaluateur peut l'appeler, et Skynet l'utilise pour servir la meilleure version après l'exécution.

> [!NOTE]
> L'agent de code qui rédige les versions ne peut voir ni le source de l'évaluateur, ni les valeurs attendues, ni les secrets de l'exécution.

Lisez [Métriques et évaluateurs](/docs/concepts/scorers) pour le contrat complet de l'évaluateur.

## Appels de modèle depuis votre code

Le code de votre dépôt et votre évaluateur peuvent appeler un modèle via la passerelle de modèles de Skynet. La sandbox fournit un point de terminaison compatible OpenAI dans des variables d'environnement :

| Variable             | Ce qu'elle contient                                                   |
| -------------------- | --------------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | L'URL de la passerelle. `APP_GATEWAY_URL` contient la même valeur.    |
| `APP_API_KEY`        | Un jeton pour cette exécution seulement. Ce n'est pas une clé de fournisseur. |
| `APP_MODEL`          | Le nom du modèle à envoyer. C'est le modèle de l'évaluateur de l'exécution. |
| `ANTHROPIC_BASE_URL` | L'URL de la passerelle pour les SDK Anthropic.                        |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Les appels sur les clés de Skynet sont décomptés du budget de l'exécution.
- Avec votre propre clé (BYOK), l'appel passe par la clé de votre fournisseur. Skynet ne facture aucuns frais de modèle.
- La passerelle refuse les requêtes pour tout autre modèle.
- Les secrets de l'exécution arrivent à votre code et à votre évaluateur sous forme de variables d'environnement. Le nom de la variable est le nom du secret. Skynet masque les valeurs des secrets dans la sortie.

La sandbox n'a aucun autre accès au réseau.

## Moteurs

Tous les moteurs travaillent sur des dépôts :

- **Auto** est l'option par défaut. Il essaie plusieurs moteurs, puis poursuit GEPA à partir de la meilleure version.
- **GEPA** fait évoluer les versions par réflexion et conserve un front de Pareto.
- **Best-of-N** demande au modèle de réflexion des propositions indépendantes et conserve la meilleure.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** et **ShinkaEvolve** utilisent un agent de code pour proposer des versions.

Les moteurs ne modifient que les fichiers des chemins que vous autorisez. Lisez [Moteurs](/docs/concepts/engines) pour le détail de chaque moteur et de ses paramètres.

## Auto

**Auto** est le choix par défaut dans l'assistant, pour chaque exécution.

1. Auto explore avec cinq moteurs : GEPA, AutoResearch, Meta-Harness, AutoSaddler et ShinkaEvolve.
2. Il choisit la meilleure version qu'ils ont trouvée.
3. Il poursuit GEPA à partir de cette version jusqu'à épuisement du budget.

Auto lit le texte de retour de votre évaluateur.

## D'où vient la version de départ

| Votre situation                          | À utiliser                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Vous avez du code dans un dépôt GitHub   | **Utiliser un dépôt**. Le code de la branche ou du commit que vous choisissez est le point de départ. |
| Vous n'avez qu'une tâche et des données  | **Créer un nouveau dépôt**. Skynet rédige une première solution. C'est le point de départ.        |

> [!WARNING]
> GEPA et Auto apprennent du texte de retour. Si votre retour est toujours la même chaîne, ces optimiseurs disposent de moins d'informations.

## Pages associées

- [Moteurs](/docs/concepts/engines)
- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Métriques et évaluateurs](/docs/concepts/scorers)
- [Lancer une exécution](/docs/guides/submit)

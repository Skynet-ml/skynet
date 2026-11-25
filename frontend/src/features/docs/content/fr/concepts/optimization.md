Skynet, c'est Optimize Anything : chaque exécution optimise un dépôt. Un moteur propose de nouvelles versions du dépôt, votre évaluateur note chaque version, et Skynet conserve la meilleure.

## Le contrat commun

Chaque exécution comporte un dépôt, un fichier `ascent.toml` avec une entrée `[run]` ou une étape `[target.<name>]`, et un évaluateur. L'entrée `[run]` exécute une version du dépôt sur un cas. L'évaluateur est une fonction Python qui reçoit la sortie de l'entrée et le cas.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    ok = output == case["expected"]
    return {"score": 1.0 if ok else 0.0, "feedback": "Match." if ok else "No match."}
```

- `output` est ce que l'entrée `[run]` a renvoyé pour le cas.
- `case` est un dict. `case["input"]` contient les champs d'entrée d'un cas. `case["expected"]` contient la valeur attendue, ou `None` si le cas n'en a pas.
- `repo_path` est un dossier qui contient seulement vos fichiers de métrique.
- `score` renvoie un objet : un nombre sous `"score"`, et une chaîne `"feedback"` facultative.

Le dépôt contient à sa racine un fichier `serve.py` avec `def serve(input)`. L'entrée `[run]` l'appelle dans sa propre boîte de sandbox. Skynet l'utilise pour servir la meilleure version après l'exécution.

L'évaluateur s'exécute dans une boîte de sandbox séparée. Cette boîte contient seulement vos fichiers de métrique et aucun code de la version, donc l'évaluateur ne peut pas importer `serve.py`. Il voit la version seulement à travers `output`, ou à travers `call()` vers une étape `[target.<name>]`. Importez `call` avec `from openascent import call`. Sans entrée `[run]` ni étape `[target.<name>]`, Skynet refuse le fichier `ascent.toml`.

> [!NOTE]
> L'agent de code qui rédige les versions ne peut voir ni le source de l'évaluateur, ni les valeurs attendues, ni les secrets de l'exécution.

Lisez [Métriques et évaluateurs](/docs/concepts/scorers) pour le contrat complet de l'évaluateur.

## Appels de modèle depuis votre code

Le code de votre dépôt et votre évaluateur peuvent appeler un modèle via la passerelle de modèles de Skynet. La sandbox fournit un point de terminaison compatible OpenAI dans des variables d'environnement :

| Variable             | Ce qu'elle contient                                                   |
| -------------------- | --------------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | L'URL de la passerelle. `ASCENT_MODEL_URL` contient la même valeur.   |
| `ASCENT_MODEL_KEY`   | Un jeton pour cette exécution seulement. Ce n'est pas une clé de fournisseur. |
| `ASCENT_MODEL`       | Le nom du modèle à envoyer. C'est le modèle de l'évaluateur de l'exécution. |
| `ANTHROPIC_BASE_URL` | L'URL de la passerelle pour les SDK Anthropic.                        |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["ASCENT_MODEL_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["ASCENT_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Les appels sur les clés de Skynet sont décomptés du budget de l'exécution.
- Avec votre propre clé (BYOK), l'appel passe par la clé de votre fournisseur. Skynet ne facture aucuns frais de modèle.
- La passerelle refuse les requêtes pour tout autre modèle.
- Votre code et votre évaluateur ne reçoivent pas les secrets de l'exécution sous forme de variables d'environnement. Un secret arrive à un rôle seulement par une table `[services.<name>]` que le rôle liste dans `[run] services` ou `[metric] services`, ou par un serveur du fichier `mcp` du rôle. Écrivez le secret dans les `headers` de la table sous la forme `${NAME}`. Skynet ajoute le secret hors de la sandbox et masque les valeurs des secrets dans la sortie.

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
| Vous avez du code dans un dépôt GitHub, GitLab ou Bitbucket   | **Utiliser un dépôt**. Le code de la branche ou du commit que vous choisissez est le point de départ. |
| Vous n'avez qu'une tâche et des données  | **Créer un nouveau dépôt**. Skynet rédige une première solution. C'est le point de départ.        |

> [!WARNING]
> GEPA et Auto apprennent du texte de retour. Si votre retour est toujours la même chaîne, ces optimiseurs disposent de moins d'informations.

## Pages associées

- [Moteurs](/docs/concepts/engines)
- [Jeux de données et répartitions](/docs/concepts/datasets)
- [Métriques et évaluateurs](/docs/concepts/scorers)
- [Lancer une exécution](/docs/guides/submit)

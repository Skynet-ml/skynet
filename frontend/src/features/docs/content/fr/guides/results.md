La page d'une exécution affiche sa progression et ses résultats. Ce guide présente les onglets de la page d'exécution, les scores, et la manière d'exporter ou de servir la meilleure version.

## Onglets de la page d'exécution

La page d'exécution n'affiche que les onglets qui s'appliquent à l'exécution. Certains onglets dépendent du parcours, du statut et de votre niveau d'expérience.

| Onglet                     | Quand il s'affiche                                                              | Ce qu'il contient                                                                          |
| -------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **Aperçu**                 | Toujours                                                                        | La progression, les scores et l'historique des scores                                      |
| **Utilisation**            | Une exécution réussie avec un programme que Skynet peut servir                  | Un formulaire pour exécuter le programme optimisé, et du code d'intégration                |
| **Artefact** | Une exécution sur un dépôt avec un résultat | Les versions de votre artefact, avec la meilleure version |
| **Données**                | Une exécution de programme DSPy terminée                                        | Les données réparties en ensembles d'entraînement, de validation et de test, et l'évaluation du jeu de test |
| **Code**                   | Quand l'exécution a du code à afficher                                          | Le code d'exécution et la métrique                                                         |
| **Récompense** | Une exécution sur un dépôt dont la métrique déclare des parts de récompense | Les composantes de la récompense et leurs poids |
| **Artefact** | Une exécution de programme DSPy terminée | La sortie optimisée et **Exporter les résultats** |
| **Journaux**               | La plupart des exécutions                                                       | Le journal de l'exécution                                                                  |
| **Utilisation et coût**    | Quand vous êtes connecté                                                        | Ce que l'exécution a dépensé                                                               |
| **Exécution planifiée** | Une exécution réussie d'un programme DSPy | Une planification pour relancer l'optimisation |
| **Configuration**          | Toujours                                                                        | La configuration de l'exécution                                                            |

> [!NOTE]
> Au niveau **Guidé**, la page d'exécution masque les onglets **Données**, **Code**, **Journaux** et **Configuration**. Un lien direct vers l'un de ces onglets l'ouvre quand même. Changez de niveau dans **Paramètres → Compte**.

## Scores

Skynet sélectionne les versions sur les cas de validation. Il indique le score final sur les cas de test. Les moteurs ne voient jamais les cas de test. Lisez [Jeux de données et répartitions](/docs/concepts/datasets).

L'onglet **Aperçu** affiche :

- **Amélioration** : l'évolution entre la version de départ et la meilleure version.
- **Progression du score** : le score au fil de l'exécution.
- **Détail du score** : chaque métrique, avec les colonnes **Référence**, **Optimisé** et **Évolution**.

Pendant que l'exécution est active, l'aperçu affiche aussi **Écoulé**, **Restant** et **Débit**.

Pour une exécution sur un dépôt, la vue d'ensemble affiche aussi **Scores nommés** quand votre métrique renvoie des scores nommés.

Pour une exécution de programme DSPy, l'onglet **Données** comporte une **Évaluation du jeu de test**. Elle affiche le score de référence et le score optimisé pour chaque cas de test.

### Quand la meilleure version est la version de départ

Il arrive qu'aucun candidat ne batte la version de départ. Skynet renvoie alors la version de départ. L'onglet **Artefact** vous indique qu'une protection contre la régression a été appliquée.

Si l'exécution s'est arrêtée à son budget, la page d'exécution vous indique quelle version Skynet a enregistrée. Lisez [Exécutions](/docs/concepts/runs).

## Exporter la meilleure version

### Exécutions sur un dépôt

L'onglet **Artefact** liste les **Versions** de votre artefact. Pour chaque version, vous pouvez :

- Copier la version.
- Télécharger la version sous forme de fichier.
- Pour une exécution sur un dépôt, télécharger la version sous forme d'archive zip du dépôt complet.

Vous pouvez comparer une version avec une autre. L'onglet affiche aussi le **Retour de l'évaluateur** pour une version.

### Exécutions de programme DSPy

L'onglet **Artefact** comporte un panneau **Exporter les résultats**. Ouvrez le menu **Télécharger**. Il affiche les formats disponibles pour l'exécution :

| Option              | Format                                                      |
| ------------------- | ----------------------------------------------------------- |
| Programme DSPy      | `.zip`, un export autonome que vous pouvez exécuter         |
| Programme optimisé  | `.pkl`                                                      |
| Prompt              | `.json`                                                     |
| Code optimisé       | `.py`                                                       |
| Journaux            | `.csv`                                                      |

## Servir la meilleure version

Vous pouvez appeler un programme DSPy optimisé depuis votre propre code. L'onglet **Utilisation** comporte deux parties :

- Un formulaire. Saisissez les champs d'entrée et sélectionnez **Exécuter** pour obtenir une prédiction.
- Du **Code d'intégration** en cURL, Python, JavaScript, Go et DSPy.

Le programme s'exécute toujours avec les paramètres de modèle avec lesquels il a été optimisé. Vous devez fournir tous les champs d'entrée. Skynet ignore les champs supplémentaires.

Pour appeler le programme, créez un jeton d'API dans **Paramètres → API**. Puis envoyez une requête :

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

Remplacez `question` par les champs d'entrée de votre programme. Le **Code d'intégration** de l'onglet **Utilisation** renseigne pour vous l'URL réelle et les noms des champs.

La réponse contient `outputs`, les `input_fields`, les `output_fields` et le `model_used`.

> [!TIP]
> Le corps de la requête accepte aussi `max_cost_cents`. Il définit le coût maximal de cet appel, en centimes. Certaines exécutions l'exigent.

Pour recevoir les sorties partielles au fur et à mesure, envoyez la même requête à `/serve/<optimization_id>/stream`. La réponse est un flux d'événements envoyés par le serveur (server-sent events).

Lisez [API](/docs/api) pour l'authentification et les autres points de terminaison.

## Partager les résultats

Vous pouvez partager une exécution avec d'autres personnes, ou la publier dans la galerie publique Explorer. Lisez [Partager et publier](/docs/guides/sharing).

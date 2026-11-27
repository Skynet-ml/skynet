La page d'une exécution affiche sa progression et ses résultats. Ce guide présente les onglets de la page d'exécution, les scores, et la manière d'exporter la meilleure version.

## Onglets de la page d'exécution

La page d'exécution n'affiche que les onglets qui s'appliquent à l'exécution. Certains onglets dépendent du statut et de votre niveau d'expérience.

| Onglet                     | Quand il s'affiche                                                   | Ce qu'il contient                                                                 |
| -------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Aperçu**                 | Toujours                                                             | La progression, les scores et l'historique des scores                             |
| **Artefact**               | Une exécution avec un résultat                                       | Les versions de votre dépôt, avec la meilleure version                            |
| **Données**                | Une exécution terminée avec des cas                                  | Les données réparties en ensembles d'entraînement, de validation et de test       |
| **Code**                   | Quand l'exécution a du code à afficher                               | L'évaluateur                                                                      |
| **Journaux**               | La plupart des exécutions                                            | Le journal de l'exécution                                                         |
| **Utilisation et coût**    | Quand vous êtes connecté                                             | Ce que l'exécution a dépensé                                                      |
| **Exécution planifiée**    | Une exécution réussie                                                | Une planification pour relancer l'optimisation à partir de la meilleure version   |
| **Configuration**          | Toujours                                                             | La configuration de l'exécution                                                   |

> [!NOTE]
> Au niveau **Guidé**, la page d'exécution masque les onglets **Données**, **Code**, **Journaux** et **Configuration**. Un lien direct vers l'un de ces onglets l'ouvre quand même. Changez de niveau dans **Paramètres → Compte**.

## Scores

Skynet sélectionne les versions sur les cas de validation. Il indique le score final sur les cas de test. Les moteurs ne voient jamais les cas de test. Lisez [Jeux de données et répartitions](/docs/concepts/datasets).

L'onglet **Aperçu** affiche :

- **Amélioration** : l'évolution entre la version de départ et la meilleure version.
- **Progression du score** : le score au fil de l'exécution.
- **Détail du score** : chaque score, avec les colonnes **Référence**, **Optimisé** et **Évolution**.

Pendant que l'exécution est active, l'aperçu affiche aussi **Écoulé**, **Restant** et **Débit**.

L'aperçu affiche aussi **Scores nommés** quand votre évaluateur renvoie des scores nommés.

### Quand la meilleure version est la version de départ

Il arrive qu'aucun candidat ne batte la version de départ. Skynet renvoie alors la version de départ. L'onglet **Artefact** vous indique qu'une protection contre la régression a été appliquée.

Si l'exécution s'est arrêtée à son budget, la page d'exécution vous indique quelle version Skynet a enregistrée. Lisez [Exécutions](/docs/concepts/runs).

## Exporter la meilleure version

L'onglet **Artefact** liste les **Versions** de votre dépôt. Pour chaque version, vous pouvez :

- Copier la version.
- Télécharger la version sous forme de fichier.
- Télécharger la version sous forme d'archive zip du dépôt complet.

Vous pouvez comparer une version avec une autre. L'onglet affiche aussi le **Retour de l'évaluateur** pour une version.

### Branches dans un nouveau dépôt

Quand Skynet a créé le dépôt pour l'exécution sur GitHub, il pousse chaque candidat sur sa propre branche, `skynet/candidate-<version id>`. La branche `skynet/best` pointe toujours vers la meilleure version. Elle avance quand une meilleure version apparaît.

## Partager les résultats

Vous pouvez partager une exécution avec d'autres personnes, ou la publier dans la galerie publique Explorer. Lisez [Partager et publier](/docs/guides/sharing).

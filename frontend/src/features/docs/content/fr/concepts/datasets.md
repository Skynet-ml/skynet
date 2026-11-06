Un jeu de données est un tableau de cas. Chaque ligne est un cas. Skynet note chaque version de votre dépôt sur les cas avec votre évaluateur et utilise les scores pour comparer les versions. Cette page explique comment Skynet lit les cas, à quoi servent les rôles de colonnes et comment il répartit les cas en ensembles d'entraînement, de validation et de test.

## Charger les données

À l'étape **Entrées** de l'assistant, vous avez deux options :

- **Importer un fichier CSV, JSON ou Excel**. Skynet accepte les fichiers `.csv`, `.json`, `.xlsx` et `.xls`.
- **Choisir dans la bibliothèque**. Utilisez un jeu de données que vous avez enregistré auparavant.

Une exécution peut compter au maximum 200 000 cas.

Lisez [Jeux de données](/docs/guides/datasets) pour gérer les jeux de données enregistrés.

## Rôles des colonnes

Sous **Comment lire les données**, attribuez un rôle à chaque colonne :

| Rôle     | Ce que Skynet fait de la colonne                                       |
| -------- | ---------------------------------------------------------------------- |
| Entrée   | Place la valeur dans `case["input"]`, que reçoit votre évaluateur        |
| Sortie   | Place la valeur dans `case["expected"]`, que reçoit votre évaluateur             |
| Ignorer  | N'utilise pas la colonne                                               |

Chaque colonne a aussi un type : **Texte** ou **Image**.

Une seule colonne peut être la colonne de sortie. Si vous définissez une deuxième colonne comme Sortie, la première passe à Ignorer.

### Ce que reçoit votre code

Pour chaque ligne, Skynet construit un cas :

- `input` est un dict. Il contient une clé pour chaque colonne d'entrée.
- `expected` est un dict avec une seule clé : le nom de la colonne de sortie.

Pour ce tableau :

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

avec les rôles `message` = Entrée, `label` = Sortie et `id` = Ignorer, votre code reçoit :

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> Dans votre évaluateur, lisez la valeur attendue par le nom de la colonne, par exemple `case["expected"]["label"]`.

## Modes de données

Les rôles déterminent le mode de données de l'exécution :

| Mode    | Quand                          | Ce qui se passe                                                                                          |
| ------- | ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Paires  | Une colonne est en Sortie      | Chaque ligne est un cas avec une entrée et une valeur attendue. Il faut au moins 2 lignes.               |
| Unique  | Aucune colonne n'est en Sortie | L'exécution n'utilise que les colonnes d'entrée de la première ligne. C'est un seul cas, sans valeur attendue. |

En mode unique, Skynet utilise ce seul cas pour l'entraînement, la sélection et le rapport. Rien n'est mis de côté.

## La répartition

En mode paires, Skynet répartit les cas en trois ensembles :

| Ensemble          | À quoi il sert                                                    |
| ----------------- | ----------------------------------------------------------------- |
| Entraînement      | Le moteur propose et teste des versions sur ces cas.              |
| Val (validation)  | Skynet sélectionne la meilleure version sur ces cas.              |
| Test              | Skynet indique le score final sur ces cas.                        |

> [!NOTE]
> Les cas de test n'atteignent jamais un moteur, un proposeur ni la vérification du contrat. Le score final porte sur des cas que l'optimiseur n'a pas vus.

### La répartition par défaut

| Nombre de paires | Entraînement | Val | Test |
| ---------------- | ------------ | --- | ---- |
| 5 ou plus        | 70 %         | 15 % | 15 % |
| Moins de 5       | 85 %         | 15 % | 0 %  |

Avec moins de 5 paires, Skynet ne conserve aucun ensemble de test.

### Comment Skynet compte les cas

Skynet convertit les fractions en nombres entiers de cas avec la méthode du plus fort reste. La somme des effectifs est toujours égale au nombre de cas. Un ensemble dont la fraction est supérieure à zéro reçoit au moins un cas. Un ensemble dont la fraction vaut zéro ne reçoit aucun cas.

Avant la répartition, Skynet mélange les cas. Le mélange utilise la graine (seed) de l'exécution. Si vous ne fournissez pas de graine, Skynet en dérive une à partir des cas, de l'objectif et de l'artefact. Ainsi, si vous soumettez à nouveau la même exécution, vous obtenez la même répartition.

### Solutions de repli

- Avec moins de 5 paires, Skynet supprime l'ensemble de test et ajuste l'entraînement et la validation pour que leur somme fasse 1.
- Si l'ensemble d'entraînement est vide, Skynet utilise les cas de validation comme entraînement.
- S'il n'y a pas d'ensemble de validation, Skynet sélectionne sur les cas d'entraînement.
- S'il n'y a pas d'ensemble de test, Skynet fait son rapport sur la validation. S'il n'y a pas non plus d'ensemble de validation, il fait son rapport sur l'entraînement.

> [!WARNING]
> Un score que Skynet indique sur l'entraînement ou la validation n'est pas un score sur données mises de côté. Utilisez au moins 5 paires pour obtenir un ensemble de test.

## Modifier la répartition

La section de répartition affiche **Découpage recommandé**. Elle propose deux modes :

1. **Utiliser la recommandation** : Skynet utilise la répartition par défaut ci-dessus.
2. **Sélection manuelle** : vous définissez vous-même **Entraînement**, **Validation** et **Test**.

En sélection manuelle, chaque champ accepte une fraction de 0 à 1, par pas de 0,05. Chaque champ affiche le nombre de cas qu'il reçoit. La somme des trois fractions doit faire 1. Sinon, l'assistant affiche « La répartition doit totaliser 1. »

## Exemples du contrat

La vérification du contrat de l'étape **Métrique et exécution** exécute votre configuration et votre évaluateur sur la version de départ, sur 3 cas d'exemple. Skynet prend ces exemples uniquement dans les cas d'entraînement et de validation, jamais dans le test.

## Pages associées

- [Jeux de données](/docs/guides/datasets)
- [Métriques et évaluateurs](/docs/concepts/scorers)
- [Lancer une exécution](/docs/guides/submit)
- [Limites](/docs/limits)

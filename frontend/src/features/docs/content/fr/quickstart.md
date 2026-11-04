Cette page explique comment lancer votre première exécution en une dizaine de minutes. Vous vous connectez, importez un petit jeu de données, remplissez les quatre étapes de l'assistant et lancez l'exécution. L'exemple améliore un programme DSPy qui classe des messages de support selon des étiquettes.

## Avant de commencer

Il vous faut :

- Un compte Skynet.
- Un fichier CSV, JSON ou Excel (`.csv`, `.json`, `.xlsx` ou `.xls`). Chaque ligne est un cas.
- Une ou plusieurs colonnes d'entrée, par exemple `message`.
- Une colonne de sortie attendue, par exemple `label`.

> [!TIP]
> Utilisez au moins 5 lignes. Avec moins de 5 paires, Skynet ne conserve aucun ensemble de test : le score final provient alors de cas que l'optimiseur a utilisés.

## 1. Connexion

Connectez-vous à Skynet. Ouvrez ensuite la page de soumission à l'adresse `/submit`.

> [!NOTE]
> Cette page utilise les libellés du niveau **Standard**. Le niveau **Guidé** utilise des libellés plus simples, saute l'étape Optimiseur et sélectionne **Auto** pour vous. Changez de niveau dans les Paramètres.

## 2. Tâche

La première étape est **Tâche**. Elle demande « Que souhaitez-vous optimiser ? »

1. Dans **Objectif**, décrivez ce qu'est un bon résultat. Utilisez des mots simples, par exemple « Classer chaque message de support dans billing, bug ou other. »
2. Facultatif : dans **Contexte**, ajoutez des informations utiles à l'optimiseur.
3. Dans **Ce qui est optimisé**, sélectionnez **Programme DSPy**.
4. Dans **Version de départ**, collez l'instruction actuelle de votre programme.

## 3. Entrées

La deuxième étape est **Entrées**.

1. Sélectionnez **Importer un fichier CSV, JSON ou Excel**, ou **Choisir dans la bibliothèque** pour utiliser un jeu de données enregistré.
2. Sous **Comment lire les données**, attribuez un rôle à chaque colonne :
   - **Entrée** : le code d'exécution reçoit cette colonne.
   - **Sortie** : la valeur attendue. La métrique la reçoit.
   - **Ignorer** : Skynet n'utilise pas cette colonne.
3. Conservez **Utiliser la recommandation** pour la répartition. Pour la plupart des jeux de données, la répartition est de 70 % entraînement, 15 % validation et 15 % test.

Lisez [Jeux de données et répartitions](/docs/concepts/datasets) pour les règles complètes.

## 4. Optimiseur

La troisième étape est **Optimiseur** (« Optimiseur, modèles et budget »).

- Laissez l'optimiseur sur **Auto**.
- Sélectionnez un **Modèle de réflexion**. L'optimiseur l'utilise pour écrire les nouvelles versions. Ce modèle est obligatoire.
- Sélectionnez un **Modèle de tâche**. Votre programme l'utilise. Un programme DSPy doit en avoir un.
- Sous **Budget**, laissez **Exécutions du scoreur max.** à sa valeur par défaut de 200.

Lisez [Moteurs](/docs/concepts/engines) pour choisir un moteur précis.

## 5. Métrique et exécution

La dernière étape est **Métrique et exécution** (« Code et métrique »). Vous écrivez deux fichiers.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` est l'instruction actuelle que propose l'optimiseur. Skynet l'applique à `program`. `input` contient les colonnes d'entrée d'un cas. `expected` contient la colonne de sortie, indexée par le nom de la colonne.

Sélectionnez ensuite **Vérifier** sous **Vérification du contrat**. La vérification exécute votre code et votre métrique dans la sandbox sur 3 cas d'exemple. Elle contrôle :

| Vérification                        | Ce qui échoue                                                         |
| ----------------------------------- | --------------------------------------------------------------------- |
| Signature du code d'exécution       | `run.py` ne définit pas `run(artifact, input)`                        |
| Signature de la métrique            | `metric.py` ne définit pas `score(input, output, expected=None)`      |
| Modèle de la métrique               | Le modèle de la métrique n'est pas utilisable                         |
| Code d'exécution sur les exemples   | Votre code d'exécution lève une erreur sur un cas d'exemple           |
| Valeur de retour de la métrique     | `score` ne renvoie pas un score et un retour valides                  |

> [!NOTE]
> Vous ne pouvez lancer l'exécution qu'une fois la vérification du contrat réussie.

## 6. Lancer et suivre

L'écran de résumé affiche « Prêt à lancer ». Ses onglets sont Général, Données, Modèles, Budget et Récompense. Vérifiez la configuration, puis lancez l'optimisation.

Ouvrez l'exécution depuis votre liste d'optimisations pour la suivre. Lisez [Résultats](/docs/guides/results) pour comprendre les scores et la version gagnante.

## Étapes suivantes

- Écrire une meilleure métrique : [Métriques et évaluateurs](/docs/concepts/scorers).
- Savoir quels moteurs conviennent à votre artefact : [Fonctionnement de l'optimisation](/docs/concepts/optimization).
- Savoir ce que coûte une exécution : [Facturation](/docs/concepts/billing).

Cette page explique comment lancer votre première exécution en une dizaine de minutes. Vous vous connectez, importez un petit jeu de données, remplissez les quatre étapes de l'assistant et lancez l'exécution. Dans l'exemple, Skynet crée un nouveau dépôt qui classe des messages de support selon des étiquettes, puis l'améliore.

## Avant de commencer

Il vous faut :

- Un compte Skynet.
- Un compte GitHub. Skynet crée le dépôt dans ce compte.
- Un fichier CSV, JSON ou Excel (`.csv`, `.json`, `.xlsx` ou `.xls`). Chaque ligne est un cas.
- Une ou plusieurs colonnes d'entrée, par exemple `message`.
- Une colonne de sortie attendue, par exemple `label`.

> [!TIP]
> Utilisez au moins 5 lignes. Avec moins de 5 paires, Skynet ne conserve aucun ensemble de test : le score final provient alors de cas que l'optimiseur a utilisés.

## 1. Connexion

Connectez-vous à Skynet. Connectez GitHub dans **Paramètres → Connecteurs**. Ouvrez ensuite la page de soumission à l'adresse `/submit`.

> [!NOTE]
> Cette page utilise les libellés du niveau **Standard**. Le niveau **Guidé** utilise des libellés plus simples, saute l'étape Optimiseur et sélectionne **Auto** pour vous. Changez de niveau dans les Paramètres.

## 2. Tâche

La première étape est **Tâche**. Elle demande « Que souhaitez-vous optimiser ? »

1. Dans **Objectif**, décrivez ce qu'est un bon résultat. Utilisez des mots simples, par exemple « Classer chaque message de support dans billing, bug ou other. »
2. Facultatif : dans **Contexte**, ajoutez des informations utiles à l'optimiseur.
3. Sélectionnez **Créer un nouveau dépôt**. Saisissez un nom, par exemple `support-labels`, et sélectionnez **Privé** ou **Public**.

Skynet crée le dépôt dans votre compte GitHub. L'assistant rédige une première solution à partir de votre tâche et de vos données. Cette solution est la version de départ. Si vous avez déjà du code, sélectionnez plutôt **Utiliser un dépôt**. Lisez [Lancer une exécution](/docs/guides/submit).

## 3. Entrées

La deuxième étape est **Entrées**.

1. Sélectionnez **Importer un fichier CSV, JSON ou Excel**, ou **Choisir dans la bibliothèque** pour utiliser un jeu de données enregistré.
2. Sous **Comment lire les données**, attribuez un rôle à chaque colonne :
   - **Entrée** : votre code reçoit cette colonne.
   - **Sortie** : la valeur attendue. L'évaluateur la reçoit.
   - **Ignorer** : Skynet n'utilise pas cette colonne.
3. Conservez **Utiliser la recommandation** pour la répartition. Pour la plupart des jeux de données, la répartition est de 70 % entraînement, 15 % validation et 15 % test.

Lisez [Jeux de données et répartitions](/docs/concepts/datasets) pour les règles complètes.

## 4. Optimiseur

La troisième étape est **Optimiseur** (« Optimiseur, modèles et budget »).

- Laissez l'optimiseur sur **Auto**.
- Sélectionnez un **Modèle de réflexion**. L'optimiseur l'utilise pour écrire les nouvelles versions. Ce modèle est obligatoire.
- Sous **Budget**, laissez **Exécutions du scoreur max.** à sa valeur par défaut de 200.

Lisez [Moteurs](/docs/concepts/engines) pour choisir un moteur précis.

## 5. Métrique et exécution

La dernière étape est **Métrique et exécution**. L'assistant rédige l'évaluateur pour vous. Vous pouvez le modifier. L'évaluateur reçoit le chemin d'une version du dépôt et un cas :

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` contient les colonnes d'entrée d'un cas. `case["expected"]` contient la colonne de sortie, indexée par le nom de la colonne. Le dépôt contient un fichier `serve.py` avec `def serve(input)`. Skynet utilise la même fonction pour servir la meilleure version après l'exécution.

Lancez ensuite la vérification. La vérification exécute votre installation et votre évaluateur sur la version de départ, dans la sandbox. Vous ne pouvez lancer l'exécution qu'une fois la vérification réussie.

## 6. Lancer et suivre

L'écran de résumé affiche « Prêt à lancer ». Ses onglets sont Général, Données, Modèles, Budget et Récompense. Vérifiez la configuration, puis lancez l'optimisation.

Ouvrez l'exécution depuis votre liste d'optimisations pour la suivre. Skynet pousse chaque candidat sur sa propre branche dans votre nouveau dépôt. La branche `skynet/best` pointe toujours vers la meilleure version. Lisez [Résultats](/docs/guides/results) pour comprendre les scores et la version gagnante.

## Étapes suivantes

- Écrire un meilleur évaluateur : [Métriques et évaluateurs](/docs/concepts/scorers).
- Comprendre le fonctionnement des moteurs : [Fonctionnement de l'optimisation](/docs/concepts/optimization).
- Savoir ce que coûte une exécution : [Facturation](/docs/concepts/billing).

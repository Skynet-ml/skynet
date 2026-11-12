Un jeu de données est un tableau d'exemples dont une exécution d'optimisation se sert pour apprendre et pour noter. Ce guide explique comment importer un fichier, modifier les lignes et les colonnes, définir les rôles des colonnes et partager un jeu de données. Pour les concepts, lisez [Jeux de données](/docs/concepts/datasets).

## Fichiers pris en charge

Skynet lit ces types de fichiers :

| Extension       | Comment Skynet le lit                                                                       |
| --------------- | ------------------------------------------------------------------------------------------- |
| `.csv`          | La première ligne est la ligne d'en-tête. Le fichier doit contenir au moins une ligne de données. |
| `.json`         | Un tableau d'objets, ou un seul objet. Les clés du premier objet deviennent les colonnes.   |
| `.xlsx`, `.xls` | Skynet ne lit que la première feuille.                                                      |

Skynet refuse les autres extensions avec le message « Unsupported file format ».

> [!TIP]
> Placez la sortie attendue dans la dernière colonne. Par défaut, l'assistant de soumission marque la dernière colonne comme sortie.

### Exemple de CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Exemple de JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Importer un jeu de données

1. Ouvrez **Jeux de données**.
2. Cliquez sur **Importer un jeu de données**, ou faites glisser un fichier sur la page. La page affiche « Déposer pour importer » pendant le glisser-déposer.
3. Attendez la notification « Jeu de données enregistré dans votre bibliothèque ».

Skynet utilise le nom du fichier sans son extension comme nom du jeu de données. Pour le changer, sélectionnez le jeu de données et cliquez sur **Renommer**.

> [!NOTE]
> Chaque compte dispose d'un quota de stockage. Si une importation dépasse le quota, Skynet ouvre une boîte de dialogue qui explique la limite. Voir [Limites](/docs/limits).

Vous pouvez aussi importer un fichier dans l'assistant de soumission (« Importer un fichier CSV, JSON ou Excel ») ou en joindre un dans le [panneau de l'agent](/docs/guides/agent-panel).

## Trouver un jeu de données

La bibliothèque liste vos jeux de données. Utilisez **Rechercher par nom** pour filtrer la liste. Les jeux de données que d'autres personnes partagent avec vous apparaissent sous **Partagé avec moi**.

Quand vous sélectionnez un jeu de données, ces actions apparaissent :

- **Étiqueter ce jeu de données** ouvre l'[outil d'annotation](/docs/guides/tagger) avec ce jeu de données.
- **Modifier les données** ouvre l'éditeur de jeu de données.
- **Partager** ouvre la boîte de dialogue de partage.
- **Renommer** change le nom du jeu de données.

Les autres actions sont **Enregistrer une copie** et **Supprimer**.

> [!WARNING]
> Supprimer retire définitivement le jeu de données de votre bibliothèque. Vous ne pouvez pas annuler cette action. Les exécutions construites à partir du jeu de données restent valides, mais leur lien vers celui-ci est rompu.

## Modifier un jeu de données

L'éditeur affiche le jeu de données sous forme de grille. Il affiche 100 lignes par page. Utilisez **Lignes suivantes** et **Lignes précédentes** pour passer d'une page à l'autre.

### Modifier les lignes et les colonnes

- **Ajouter une ligne** et **Ajouter une colonne** ajoutent de nouvelles cellules à la fin.
- **Insérer une ligne au-dessus** et **Insérer une ligne en dessous** ajoutent une ligne à côté de la ligne sélectionnée.
- **Dupliquer la ligne**, **Supprimer la ligne**, **Supprimer la colonne** et **Renommer la colonne** modifient la structure.
- **Faire glisser pour remplir** copie la valeur d'une cellule dans les cellules voisines.
- **Annuler** et **Rétablir** annulent ou répètent votre dernière modification.

Double-cliquez sur une ligne pour l'afficher en entier.

### Enregistrement

L'éditeur enregistre vos modifications automatiquement, peu après chaque modification. Le statut affiche **Enregistrement…** pendant l'enregistrement. Si l'enregistrement échoue, le statut affiche **Échec de l'enregistrement**.

> [!NOTE]
> L'éditeur ne peut pas enregistrer un jeu de données sans ligne. Il affiche « Ajoutez une ligne pour enregistrer vos modifications » jusqu'à ce que vous en ajoutiez une.

Cliquez sur **Retour à la bibliothèque** quand vous avez terminé.

### Accès en lecture seule

Si vous avez le rôle Lecteur sur un jeu de données partagé, l'éditeur s'ouvre en lecture seule. Pour modifier les données, demandez le rôle Éditeur au propriétaire, ou utilisez **Enregistrer une copie**.

### Étiqueter avec l'IA

L'éditeur propose une action **Étiqueter avec l'IA**. Elle ouvre l'outil d'annotation, où un modèle d'IA vous aide à ajouter des étiquettes. Voir [Annoter des données avec l'outil d'annotation](/docs/guides/tagger).

## Définir les rôles des colonnes

Une exécution doit savoir quelles colonnes sont des entrées et quelle colonne est la sortie attendue. Vous définissez ces rôles quand vous soumettez une exécution, pas dans l'éditeur de jeu de données.

Dans l'assistant de soumission, l'étape « Comment lire les données » affiche chaque colonne avec trois choix :

- **Entrée** : votre évaluateur reçoit cette valeur dans `case["input"]`.
- **Sortie** : la réponse attendue à laquelle l'évaluateur compare la sortie.
- **Ignorer** : l'exécution ignore cette colonne.

Les valeurs par défaut sont :

- Chaque colonne est une entrée, et la dernière colonne est la sortie.
- Un jeu de données à une seule colonne a une entrée et aucune sortie.

Sans colonne de sortie, Skynet utilise la première ligne comme entrée unique. Vous devez choisir au moins une colonne d'entrée.

Certaines colonnes peuvent aussi contenir des images. Pour ces colonnes, choisissez **Image** ou **Texte** pour indiquer à Skynet comment lire les valeurs.

> [!TIP]
> L'éditeur de jeu de données affiche un badge « output » sur les colonnes de sortie. Une nouvelle colonne que vous ajoutez dans l'éditeur est d'abord une entrée.

L'assistant répartit aussi les lignes en parties pour l'entraînement et l'évaluation. La somme des parties doit faire 1. Sinon, l'assistant affiche « La répartition doit totaliser 1. » Pour le parcours complet, lisez [Lancer une exécution](/docs/guides/submit).

## Partager un jeu de données

Sélectionnez un jeu de données et cliquez sur **Partager**. La boîte de dialogue **Partage et confidentialité** s'ouvre.

### Accès au lien

| Option                                | Qui peut ouvrir le jeu de données                                              |
| ------------------------------------- | ------------------------------------------------------------------------------ |
| **Restreint**                         | Uniquement le propriétaire et les personnes invitées.                          |
| **Toute personne disposant du lien**  | Toute personne connectée qui dispose du lien, avec le rôle que vous choisissez. |

Pour un jeu de données en « Toute personne disposant du lien », choisissez le rôle que donne le lien : **Lecteur** ou **Éditeur**. Cliquez sur **Copier le lien** pour copier l'URL.

### Inviter des personnes

1. Sous **Inviter des personnes**, saisissez un nom d'utilisateur dans **Ajouter des personnes par nom d'utilisateur**.
2. Cliquez sur **Inviter**.
3. Vérifiez la liste sous **Personnes ayant accès**. Utilisez **Changer de rôle** pour modifier le rôle d'une personne.

Les rôles sont :

| Rôle         | Accès                                                                        |
| ------------ | ---------------------------------------------------------------------------- |
| Propriétaire | Contrôle total, y compris la gestion du partage et la suppression.           |
| Éditeur      | Accès Lecteur, plus l'exécution du modèle, la discussion et le renommage.    |
| Lecteur      | Ouvre le jeu de données. L'éditeur est en lecture seule.                     |

### Transférer la propriété

Utilisez **Transférer la propriété** pour donner le jeu de données à une autre personne. Skynet vous demande de confirmer.

> [!WARNING]
> Après le transfert, vous devenez éditeur et perdez les privilèges de propriétaire.

## Étapes suivantes

- [Lancez une exécution](/docs/guides/submit) avec votre jeu de données.
- [Annotez des données avec l'outil d'annotation](/docs/guides/tagger) si vos lignes n'ont pas encore d'étiquettes.
- Lisez la page sur les [évaluateurs](/docs/concepts/scorers) pour choisir comment Skynet compare les sorties.

L'assistant de soumission configure et démarre une exécution. Il comporte quatre étapes : **Tâche**, **Entrées**, **Optimiseur** et **Métrique et exécution**. Chaque étape comporte un ou plusieurs écrans. Ce guide présente chaque écran, de la tâche jusqu'à la vérification du contrat et au démarrage de l'exécution.

> [!NOTE]
> L'assistant affiche plus ou moins de paramètres selon le niveau d'expérience : **Guidé**, **Standard** ou **Expert**. Guidé est le niveau par défaut. Changez de niveau dans **Paramètres → Compte**, sous **Quantité d'informations affichées**. Certains libellés sont plus simples au niveau Guidé. Ce guide utilise les libellés des niveaux Standard et Expert et donne entre parenthèses le libellé du niveau Guidé quand il est différent.

## Étape 1 : Tâche

### Que voulez-vous optimiser ?

Décrivez votre tâche à l'assistant IA dans la discussion **Entretien sur la tâche**. L'assistant IA rédige deux champs pour vous :

- **Objectif** (Guidé : **But**) : ce que l'artefact optimisé doit bien faire.
- **Contexte** (Guidé : **Contexte supplémentaire**) : le contexte, les contraintes et ce à quoi ressemble un bon résultat.

Vous pouvez modifier les deux champs. L'assistant IA n'écrase pas vos modifications.

### Ce qui sera optimisé

Sélectionnez le **Type d'artefact** :

| Type d'artefact | Ce qui change                                             |
| --------------- | --------------------------------------------------------- |
| Prompt          | Les instructions envoyées à un modèle de langage          |
| Code            | Le source d'un programme                                  |
| Programme DSPy  | Les instructions des prédicteurs d'un module DSPy         |
| Texte           | Tout autre texte, comme un document ou un modèle de texte |

Indiquez ensuite la **Version de départ** (Guidé : **Notre point de départ**). Collez la version actuelle, ou laissez l'assistant IA rédiger un brouillon.

### Optimiser un dépôt

Vous pouvez aussi optimiser le code d'un dépôt. Renseignez ces champs :

- **Dépôt** : choisissez un dépôt, ou saisissez-le sous la forme `owner/name`.
- **Commit** : un SHA de 40 caractères. Laissez le champ vide pour utiliser le dernier commit de la branche.
- **Chemins que l'optimiseur peut modifier** : un fichier ou un dossier par ligne. Un point désigne le dépôt entier.
- **Commande d'installation** : s'exécute dans chaque nouvelle copie de travail avant la notation. Laissez le champ vide et Skynet trouve la commande.

## Étape 2 : Entrées

### Données

Ajoutez les cas de l'exécution. Vous pouvez :

- Importer un fichier CSV, JSON ou Excel.
- Choisir un jeu de données dans votre bibliothèque.

Lisez [Jeux de données et répartitions](/docs/concepts/datasets) et [Préparer un jeu de données](/docs/guides/datasets).

### Comment lire les données

L'assistant IA lit les données et propose une manière de les utiliser. Vérifiez ses choix :

- Marquez chaque colonne comme entrée ou comme sortie attendue, ou ignorez-la.
- Sans colonne de sortie, Skynet utilise la première ligne comme entrée unique.
- Définissez la répartition en ensembles d'entraînement, de validation et de test. La somme de la répartition doit faire 1.

> [!WARNING]
> Vous ne pouvez pas ouvrir les étapes suivantes tant que l'étape **Entrées** présente un problème. Corrigez les problèmes que l'assistant affiche, puis continuez.

## Étape 3 : Optimiseur

Au niveau Standard, cette étape comporte un seul écran. Il affiche l'optimiseur, le budget et, si nécessaire, les modèles. Aux niveaux Guidé et Expert, l'étape comporte trois écrans : **Optimiseur**, **Modèles** et **Budget**.

### Optimiseur

L'assistant ne liste que les optimiseurs adaptés à votre tâche et à vos entrées. **Auto** est sélectionné pour vous. Sélectionnez un autre optimiseur si vous le souhaitez. Lisez [Moteurs](/docs/concepts/engines) et [Fonctionnement de l'optimisation](/docs/concepts/optimization).

### Modèles

- **Modèle de tâche** : le modèle qu'appelle votre code d'exécution. Laissez le champ vide si votre code d'exécution n'appelle pas de modèle. Un programme DSPy doit avoir un modèle de tâche.
- **Modèle de réflexion** : le modèle qui lit les scores et les retours et rédige les candidats suivants. Avec un optimiseur basé sur un agent de code, c'est le modèle de l'agent.

### Budget

Définissez combien de temps l'exécution peut continuer à essayer, et combien elle peut dépenser :

- **Exécutions du scoreur max.** (Guidé : **Essais max.**) : le nombre maximal de candidats que Skynet note. Certains optimiseurs exigent un nombre minimal.
- **Arrêter au score** (Guidé : **Arrêter plus tôt quand le score atteint**) : l'exécution s'arrête quand une version atteint ce score.
- **Itérations max.** (Guidé : **Tours max.**) : affiché pour les optimiseurs qui le prennent en charge.
- **Plafond de dépenses** : le montant maximal que l'exécution peut dépenser. La valeur par défaut est de 5 $, ou le montant **Nécessaire pour démarrer** s'il est plus élevé. Vous pouvez aussi sélectionner **Sans plafond**.

L'assistant ne vous laisse pas continuer si le plafond ou votre solde est inférieur au montant nécessaire pour démarrer. Lisez [Budgets et facturation](/docs/concepts/billing).

## Étape 4 : Métrique et exécution

### Code et métrique

Rédigez deux fichiers :

- `run.py` : le code d'exécution qui exécute votre artefact sur un cas.
- `metric.py` : la métrique qui note la sortie et rédige le retour.

L'**Agent de code** peut rédiger et modifier n'importe quel fichier pour vous. Il vérifie son code et corrige ce qui échoue. Vous pouvez ajouter jusqu'à 20 fichiers supplémentaires. Lisez [Métriques et évaluateurs](/docs/concepts/scorers).

La métrique définit son modèle dans son propre appel à `llm()`. Sans `model=`, la métrique utilise le modèle de réflexion.

### Vérification du contrat

La **Vérification du contrat** (Guidé : **Exécution de test**) exécute votre code et votre métrique dans la sandbox sur 3 cas d'exemple. Elle vérifie :

- la signature du code d'exécution,
- la signature de la métrique,
- le modèle de la métrique,
- le code d'exécution sur les exemples,
- la valeur de retour de la métrique.

Sélectionnez **Vérifier** (Guidé : **Tester**). Vous ne pouvez pas démarrer l'exécution tant que la vérification n'a pas réussi. Si vous modifiez le code ou la configuration après une réussite, relancez la vérification.

> [!TIP]
> Ouvrez chaque exemple pour voir son entrée, sa sortie et son retour. Un bon retour donne à l'optimiseur plus de matière que le score seul.

### Exécution planifiée

Vous pouvez relancer l'optimisation selon une planification après sa réussite. La valeur par défaut est **Désactivée**.

### Prêt à lancer

Le dernier écran affiche un récapitulatif en cinq onglets : **Général**, **Données**, **Modèles**, **Budget** et **Récompense**. Vérifiez la configuration, puis démarrez l'optimisation.

Le récapitulatif affiche aussi la visibilité de l'exécution. Les nouvelles exécutions sont publiques par défaut. Modifiez-la plus tard dans la boîte de dialogue **Partage et confidentialité** de la page de l'exécution. Lisez [Partager et publier](/docs/guides/sharing).

> [!NOTE]
> Une exécution sur un dépôt ne comporte, à cette étape, que l'écran **Code et métrique** et le récapitulatif. Skynet vérifie la configuration par rapport au dépôt au démarrage de l'exécution.

## Après le démarrage

La page de l'exécution s'ouvre. Lisez [Exécutions](/docs/concepts/runs) et [Lire les résultats](/docs/guides/results).

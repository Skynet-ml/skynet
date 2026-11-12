Le panneau de l'agent est un assistant conversationnel intégré à Skynet. Il peut lire vos exécutions, vos jeux de données et votre solde, et il peut agir pour vous, par exemple renommer ou annuler une exécution. Vous décidez quelles actions nécessitent votre approbation.

## Ouvrir le panneau

Le panneau s'ouvre sur le côté de l'application. Quand vous le réduisez, une pastille portant le libellé **Ouvrir l'agent** reste à l'écran. Cliquez sur la pastille pour rouvrir le panneau.

Cliquez sur **Ce que je peux faire** pour voir la liste des outils de l'agent.

### Conversations

- **Nouvelle conversation** démarre une discussion vide.
- **Conversations passées** liste vos anciennes discussions. Vous pouvez les rechercher, les épingler, les renommer et les supprimer.

### Envoyer un message

Saisissez votre message et appuyez sur Entrée. Vous pouvez aussi cliquer sur **Dicter** et prononcer votre message.

Pendant le travail de l’agent, le champ de saisie reste disponible :

- **Entrée** oriente le travail en cours si cette fonction est disponible. L’agent lit le message à l’étape suivante. **Tab** place un nouveau tour dans la file. Sans orientation disponible, Entrée place aussi le message dans la file.
- Sous **Messages en attente**, vous pouvez modifier, supprimer et faire glisser les messages pour les réordonner. **Orienter**, si disponible, transmet un message de la file au travail en cours.
- Un message marqué **Non confirmé** est peut-être déjà parvenu à l'agent. Vous pouvez le modifier ou le supprimer à tout moment. Les autres messages en file peuvent continuer avant lui.
- **Arrêter** interrompt le tour et met la file en pause. Les instructions non lues reviennent dans la liste. Rien ne démarre automatiquement. **Échap** dans le champ fait de même, sauf si un menu est ouvert ou pendant une composition IME. Échap ferme d’abord une suggestion ou un menu de ce champ.
- Quand la file est en pause et sans tour actif, **Orienter** envoie ce message. La file continue ensuite dans l’ordre. Une fin normale de tour lance aussi le message suivant.
- Une fois connecté, **Entrée** dirige l'assistant de documentation quand il annonce une limite d'étape sûre. Il lit le message à la prochaine étape du modèle déjà prévue. **Tab** place toujours le message dans la file. Avant l'annonce, sans connexion et dans le chat protégé des programmes déployés, Entrée et Tab placent les messages dans la file. L'assistant de documentation met les messages restants en pause après une limite de requêtes.

## Ce que l'agent peut faire

L'agent peut lire :

- Vos exécutions, leurs journaux et leurs résultats de test.
- Les statistiques et le solde de votre portefeuille.
- Les jeux de données de votre bibliothèque et les résultats de recherche publics.
- Vos sessions d'étiquetage (en lecture seule).
- Les jeux de données d'exemple, le catalogue des moteurs et le catalogue des optimiseurs.

L'agent peut aussi agir. Les actions sûres comprennent :

- Renommer et épingler des exécutions, et épingler plusieurs exécutions à la fois.
- Mettre une exécution en pause.
- Modifier et valider le code d'une exécution.
- Profiler un jeu de données et définir les rôles de ses colonnes.
- Trouver les modèles disponibles.
- Mettre à jour l'assistant de soumission et vos préférences.
- Préparer un jeu de données d'exemple.
- Créer un dossier.

Les actions destructrices comprennent :

- Supprimer une ou plusieurs exécutions.
- Soumettre, annuler, cloner, réessayer, reprendre ou relancer une exécution, et annuler plusieurs exécutions.
- Supprimer, déplacer ou mettre à jour un dossier, et déplacer des exécutions entre dossiers.

> [!NOTE]
> Les outils web (recherche, récupération d'une URL, exploration d'un site) ne sont disponibles que si une clé de recherche web est configurée sur le serveur. Si vous ne les voyez pas, votre instance Skynet ne les propose pas.

## Modes de confiance

Le mode de confiance détermine quelles actions nécessitent votre approbation. Cliquez sur le sélecteur de confiance dans la zone de saisie pour le changer. Le sélecteur passe successivement par les trois modes.

| Mode                  | Description                                            | Actions sûres          | Actions destructrices  |
| --------------------- | ------------------------------------------------------ | ---------------------- | ---------------------- |
| **Demander**          | Demande une confirmation avant chaque action.          | Approbation requise    | Approbation requise    |
| **Automatique**       | Effectue les actions sûres sans confirmation.          | Exécutées immédiatement | Approbation requise   |
| **Sans restriction**  | Effectue tout sans confirmation.                       | Exécutées immédiatement | Exécutées immédiatement |

Le mode par défaut est **Demander**. Les outils en lecture seule ne nécessitent jamais d'approbation.

> [!WARNING]
> En mode **Sans restriction**, l'agent peut supprimer des exécutions ou soumettre des exécutions payantes sans approbation. N'utilisez ce mode que si vous faites confiance à la tâche.

### Définir le mode par défaut

1. Ouvrez **Paramètres**.
2. Ouvrez l'onglet **Panneau de l'agent**.
3. Sous **Niveau de confiance de l'agent**, choisissez **Demander d'abord**, **Automatique pour les actions sûres uniquement** ou **Aucune approbation préalable**.

### Approuver une action

Quand une action nécessite une approbation, le panneau affiche une carte d'approbation. La carte comporte un bouton de confirmation pour l'action, et **Annuler**. Cliquez sur **Afficher les détails techniques** pour voir l'appel d'outil exact.

Si vous ne répondez pas dans les 15 minutes, l'agent considère l'action comme refusée.

## Joindre un fichier

Vous pouvez joindre des fichiers à un message. Chaque fichier peut atteindre 20 Mo. Vous pouvez joindre un seul jeu de données à la fois.

Pour importer un jeu de données par l'agent :

1. Joignez un fichier CSV, JSON ou Excel.
2. Vérifiez la carte d'importation. Elle affiche les colonnes du fichier.
3. Définissez les rôles des colonnes sur la carte, puis confirmez.

Si l'agent ne peut pas lire le fichier, la carte affiche « Impossible de lire le fichier. Utilisez un fichier CSV, JSON ou XLSX. »

Pour les règles relatives aux fichiers, consultez [Préparer un jeu de données](/docs/guides/datasets).

## Choisir un modèle

Le menu des modèles de la zone de saisie comporte ces groupes :

- **Auto** laisse Skynet choisir le modèle.
- **Crédits Skynet** utilise un modèle hébergé, payé sur votre solde.
- **Votre propre clé** utilise un modèle par l'intermédiaire de votre propre clé de fournisseur.

Certains modèles permettent de régler l'effort de raisonnement. Les niveaux sont **Aucun**, **Minimal**, **Faible**, **Moyen**, **Élevé**, **Très élevé** et **Max**. **Par défaut** utilise le niveau par défaut du modèle.

Pour définir un modèle par défaut pour les nouvelles discussions, ouvrez **Paramètres** > **Panneau de l'agent** > **Modèle par défaut**.

### Coût

Les discussions avec l'agent sur les crédits Skynet utilisent votre solde. Si votre solde est vide, le panneau affiche « Votre solde est vide. Ajoutez des fonds pour continuer à discuter avec l'agent. » Pour les tarifs, lisez [Facturation](/docs/concepts/billing).

## Conseils

> [!TIP]
> Demandez à l'agent d'expliquer une exécution avant de la modifier. Par exemple : « Pourquoi l'exécution 42 a-t-elle obtenu un score inférieur à celui de l'exécution 41 ? »

> [!TIP]
> Utilisez le mode **Demander** quand vous essayez un nouveau type de requête. Passez en mode **Automatique** quand vous savez ce que l'agent va faire.

## Autres façons d'utiliser un agent

Vous pouvez aussi connecter votre propre client d'IA à Skynet par le serveur MCP. Voir [MCP](/docs/mcp). Pour les scripts, utilisez l'[API](/docs/api).

## Étapes suivantes

- [Lancer une exécution](/docs/guides/submit).
- [Lire les résultats](/docs/guides/results).
- Lisez [Sécurité](/docs/security) pour savoir comment Skynet protège vos clés.

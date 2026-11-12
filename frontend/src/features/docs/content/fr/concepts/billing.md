Skynet utilise un solde prépayé. Vous ajoutez des fonds à ce solde. Chaque exécution a un plafond de dépenses, et Skynet impute l'utilisation réelle de l'exécution à votre solde. Cette page explique le solde, les budgets d'exécution, les réservations, les frais et l'utilisation de vos propres clés de fournisseur.

> [!NOTE]
> Les chiffres de cette page sont les valeurs par défaut actuelles. L'application affiche les valeurs en vigueur. Ouvrez **Paramètres → Facturation** pour voir votre solde, les frais de recharge et l'historique de vos transactions.

## Solde

Votre solde est en dollars américains. Skynet le conserve en cents.

Par défaut, Skynet n'offre aucun solde de départ gratuit. Ajoutez des fonds avant de lancer votre première exécution payante.

Vous ajoutez des fonds dans **Paramètres → Facturation**. Vous pouvez choisir un montant fixe ou saisir un montant personnalisé :

| Recharge         | Montant ajouté à votre solde        |
| ---------------- | ----------------------------------- |
| Montants fixes   | 5 $, 20 $ ou 50 $                   |
| Personnalisée    | N'importe quel montant de 5 $ à 1 000 $ |

Le montant que vous choisissez est le montant crédité sur votre solde. Les frais de recharge s'y ajoutent. Voir les frais ci-dessous.

**Paramètres → Utilisation** indique ce que vous avez dépensé. L'historique des transactions dans **Paramètres → Facturation** liste vos recharges et les débits des exécutions.

## Budgets d'exécution

Chaque exécution a un plafond de dépenses. Ce plafond est le maximum que l'exécution peut dépenser. Il couvre la configuration et l'optimisation.

Une exécution n'a pas d'estimation de coût avant de démarrer. Elle dépense jusqu'à son plafond, s'arrête au budget et conserve sa meilleure version. Lisez [Exécutions](/docs/concepts/runs) pour voir comment Skynet décide quand s'arrêter.

Vous fixez le plafond à l'écran **Budget** de l'assistant de soumission, dans le champ **Plafond de dépenses** :

- **Fixer un plafond** est le mode par défaut. Le plafond démarre à 5 $. Si l'exécution a besoin de plus de 5 $ pour démarrer, le plafond démarre à ce montant.
- **Sans plafond** permet à l'exécution de puiser dans votre solde jusqu'à ce qu'elle se termine. L'exécution ne se met jamais en pause. Si votre solde est épuisé, l'exécution s'arrête et conserve ce qu'elle a produit.

### Le montant nécessaire pour démarrer

L'assistant affiche le montant **Nécessaire pour démarrer**. Ce montant correspond à :

- les provisions pour les sandboxes que l'exécution ouvre avant son premier appel de modèle, plus
- une marge pour les premiers appels de modèle. Cette marge est de 1 $, ou du coût de l'appel unique le plus cher parmi vos modèles sélectionnés s'il est plus élevé.

L'assistant ne vous laisse pas continuer si le plafond est inférieur à ce montant. Il vous bloque aussi si votre solde est inférieur à ce montant. Relevez le plafond ou ajoutez des fonds pour continuer.

> [!TIP]
> Quand une exécution s'arrête à son budget, utilisez **Relever le plafond et continuer** sur la page de l'exécution. L'exécution reprend à partir de son dernier point de contrôle.

## Réservations

Avant tout travail payant, Skynet réserve une partie du plafond de l'exécution. Skynet libère la réservation quand le travail est réglé et ne facture que le coût réel.

Quand un budget a déjà de l'activité, la carte **Plafond de dépenses** de l'assistant affiche son registre :

| Libellé                         | Signification                                                                        |
| ------------------------------- | ------------------------------------------------------------------------------------ |
| **Restant dans ce plafond**     | La part du plafond qui reste.                                                        |
| **Dépensé jusqu'ici**           | Le coût réglé.                                                                       |
| **Réservé**                     | L'argent bloqué pour le travail en cours. Skynet le libère quand le travail est réglé. |

## Frais

Skynet applique deux frais en plus du coût du fournisseur.

| Frais                                  | Valeur par défaut actuelle                    | S'applique à                                                  |
| -------------------------------------- | --------------------------------------------- | ------------------------------------------------------------- |
| Frais de recharge                      | 12,5 % du montant, plus 0,35 $                | Chaque recharge                                               |
| Majoration d'utilisation               | Coût du fournisseur × 1,15 (coût plus 15 %)   | Appels de modèle sur les clés de Skynet, et calcul en sandbox |

### Frais de recharge

Skynet arrondit la partie en pourcentage au cent supérieur, puis ajoute 0,35 $. Par exemple, une recharge de 20 $ entraîne des frais de 2,85 $. L'écran de recharge affiche les frais avant le paiement.

### Majoration d'utilisation

Skynet multiplie le coût du fournisseur par la majoration d'utilisation. La majoration s'applique aux appels de modèle que Skynet paie et au calcul en sandbox. Les réservations, le montant nécessaire pour démarrer et les prix des modèles dans l'application incluent déjà la majoration.

Skynet détermine le coût fournisseur d'un appel de modèle dans cet ordre :

1. Le coût que OpenRouter indique pour l'appel.
2. La grille tarifaire en vigueur d'OpenRouter pour le modèle.
3. La table de prix de LiteLLM.
4. Un tarif de repli élevé de 15 $ par million de jetons d'entrée et 75 $ par million de jetons de sortie. Ce tarif garantit que Skynet ne facture jamais un modèle inconnu en dessous de son coût.

## Vos propres clés de fournisseur (BYOK)

Vous pouvez effectuer des appels de modèle avec votre propre clé de fournisseur. Ajoutez des clés dans **Paramètres → Fournisseurs**. Pour OpenRouter, vous pouvez aussi utiliser **Continuer avec OpenRouter**.

Quand un appel de modèle utilise votre clé :

- Votre fournisseur facture les jetons du modèle sur votre clé.
- Skynet ne facture aucuns frais de modèle.
- Le budget de l’exécution couvre uniquement l’utilisation du bac à sable.

Les éventuels frais d'un évaluateur externe ou d'un fournisseur MCP sont payés directement à ce fournisseur. Ils ne font pas partie du plafond de dépenses de l'exécution.

## Appels de modèle depuis votre code

Le code de votre dépôt et votre évaluateur peuvent appeler un modèle via la passerelle de modèles de Skynet. Lisez [Fonctionnement de l'optimisation](/docs/concepts/optimization).

- Un appel sur les clés de Skynet est décompté du budget de l'exécution, comme tout autre appel de modèle. La majoration d'utilisation s'applique.
- Un appel sur votre propre clé (BYOK) passe par la clé de votre fournisseur. Skynet ne facture aucuns frais de modèle pour cet appel.

## Utilisation de la sandbox

Le code de votre dépôt et votre évaluateur s'exécutent dans une sandbox. Skynet mesure le calcul en sandbox :

1. Avant le démarrage d'une sandbox, Skynet réserve un montant sur le plafond de l'exécution.
2. Quand la sandbox se termine, Skynet facture l'utilisation finale indiquée par le fournisseur de la sandbox.
3. La majoration d'utilisation s'applique au calcul en sandbox.

Le calcul en sandbox fait partie du plafond de dépenses de l'exécution, y compris quand les appels de modèle utilisent votre propre clé.

## Service (serving)

Chaque appel au point de terminaison serve d'une exécution terminée est facturé séparément. L'appelant paie avec son propre solde. Définissez `max_cost_cents` dans la requête pour plafonner un appel. Lisez [Résultats](/docs/guides/results).

## Offres

Il existe deux offres : Free et Pro. Pro relève certaines limites, comme le nombre d'exécutions simultanées et le stockage. Si un renouvellement Pro échoue, le compte conserve les limites Pro pendant 7 jours. Ensuite, il applique les limites Free jusqu'à ce que le paiement aboutisse. Voir [Limites](/docs/limits).

## Garde-fous de la plateforme

- Un tour de modèle interactif, comme une conversation avec l'assistant, nécessite un solde d'au moins 0,05 $ avant de démarrer.
- L'assistant IA de soumission est gratuit. Chaque compte peut lancer 50 tours de cet assistant par jour UTC.

## Pages associées

- [Exécutions](/docs/concepts/runs)
- [Lancer une exécution avec l'assistant](/docs/guides/submit)
- [Limites](/docs/limits)

Skynet fixe des limites sur les exécutions, les fichiers, le stockage et les débits de requêtes. Ces limites garantissent la stabilité de la plateforme et protègent votre solde. Cette page liste les valeurs par défaut. Un administrateur peut modifier certaines limites pour un compte.

> [!NOTE]
> Les valeurs de cette page sont les valeurs par défaut actuelles. Quand vous atteignez une limite, l'application ou l'API vous indique quelle limite a bloqué la requête.

## Exécutions

| Limite                           | Free | Pro   |
| -------------------------------- | ---- | ----- |
| Exécutions actives simultanées   | 5    | 20    |
| Exécutions enregistrées          | 100  | 1 000 |

Une exécution active est une exécution au statut `pending`, `validating`, `running` ou `paused`. Quand vous avez atteint le nombre maximal d'exécutions actives, Skynet refuse les nouvelles exécutions jusqu'à ce que l'une d'elles se termine.

La limite d'exécutions enregistrées compte toutes vos exécutions. Supprimez celles dont vous n'avez pas besoin pour faire de la place.

Si un renouvellement Pro échoue, le compte conserve les limites Pro pendant 7 jours. Ensuite, il applique les limites Free jusqu'à ce que le paiement aboutisse.

| Limite                                              | Valeur                                                                  |
| --------------------------------------------------- | ----------------------------------------------------------------------- |
| Copies dans une requête de clonage par l'API        | 1 à 5                                                                   |
| Tentatives de reprise automatique d'une exécution   | 3                                                                       |
| Cas d'exemple dans la vérification du contrat       | 3                                                                       |
| Événements de progression stockés par exécution     | 5 000. Skynet supprime d'abord les événements les plus anciens.         |
| Durée de vie d'une sandbox                          | 45 minutes. Une exécution qui a besoin de plus de temps ouvre une nouvelle sandbox. |

Lisez [Exécutions](/docs/concepts/runs) pour savoir comment fonctionnent les tentatives de reprise.

## Stockage

| Limite            | Free    | Pro    |
| ----------------- | ------- | ------ |
| Stockage total    | 500 Mio | 20 Gio |

Le stockage total comprend vos exécutions, jeux de données, journaux, conversations avec l'agent, imports en attente et données de recherche. Skynet refuse toute écriture qui dépasserait votre limite de stockage.

## Fichiers et jeux de données

| Limite                                                  | Valeur                                    |
| ------------------------------------------------------- | ----------------------------------------- |
| Fichier de jeu de données enregistré dans la bibliothèque | 50 Mio par fichier, après compression   |
| Lignes issues d'un connecteur qui importe des enregistrements | 20 000                              |
| Fichier joint à une conversation avec l'agent           | 20 Mio                                    |
| Texte lu dans une pièce jointe de conversation          | 50 000 caractères                         |
| Clip audio pour la dictée                               | 25 Mo                                     |
| Archive de dépôt GitHub                                 | 200 Mio, compressée                       |
| Fichier de dépôt ouvert sur la page d'exécution         | 1 Mio                                     |

Les connecteurs qui importent des enregistrements comprennent les tables de bases de données, les traces LLM et les bases de données Notion. Lisez [Préparer un jeu de données](/docs/guides/datasets).

### Fichiers de l'espace de travail dans l'assistant

| Limite                                   | Valeur                                                                  |
| ---------------------------------------- | ----------------------------------------------------------------------- |
| Fichiers supplémentaires dans l'espace de travail | 20                                                             |
| Longueur d'un nom de fichier             | 120 caractères                                                          |
| Profondeur des dossiers                  | 3 dossiers                                                              |
| Types de fichiers                        | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`scorer.py` contient votre évaluateur. Skynet appelle sa fonction `score(repo_path, case)` pour chaque cas.

## Débits de requêtes

| Limite                                                                          | Valeur                                            |
| ------------------------------------------------------------------------------- | ------------------------------------------------- |
| Soumissions d'exécutions                                                        | 30 par compte sur une minute glissante            |
| Clips de dictée                                                                 | 120 par compte et par heure                       |
| Fichiers lus pour les conversations avec l'agent                                | 300 par compte et par heure                       |
| Requêtes de compte (inscription, réinitialisation du mot de passe, vérification de l'e-mail) | 20 par adresse e-mail sur une heure glissante |
| Tours de l'assistant IA de soumission                                           | 50 par compte et par jour UTC                     |
| Jetons de recherche et d'indexation                                             | 2 000 000 par compte et par mois                  |

Quand vous dépassez une limite de débit, l'API renvoie le statut HTTP `429`. Patientez, puis renvoyez la requête.

Quand un compte dépasse son quota mensuel de jetons de recherche et d'indexation, Skynet arrête d'indexer les nouveaux éléments de ce compte. La recherche fonctionne alors comme si la recherche sémantique n'était pas disponible.

## Argent

| Limite                                                       | Valeur                                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| Plafond de dépenses par défaut d'une nouvelle exécution      | 5 $, ou le montant nécessaire pour démarrer s'il est plus élevé                |
| Marge incluse dans le montant nécessaire pour démarrer       | 1 $, ou le coût de l'appel de modèle unique le plus cher s'il est plus élevé   |
| Recharge personnalisée                                       | De 5 $ à 1 000 $                                                               |
| Solde nécessaire pour démarrer un tour de modèle interactif  | 0,05 $                                                                         |

Lisez [Budgets et facturation](/docs/concepts/billing) pour les frais et les réservations.

## Pages associées

- [Exécutions](/docs/concepts/runs)
- [Budgets et facturation](/docs/concepts/billing)
- [FAQ](/docs/faq)

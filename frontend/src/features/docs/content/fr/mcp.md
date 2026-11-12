Le serveur MCP de Skynet permet à un agent IA d'utiliser Skynet à votre place. Claude, ChatGPT, Codex, Cursor et d'autres clients MCP peuvent vérifier la configuration d'une exécution, lancer des exécutions, les suivre et les arrêter. Vous approuvez la connexion une seule fois dans votre navigateur. L'agent fixe un plafond de dépenses pour chaque exécution.

## URL du serveur

Tous les clients utilisent la même URL :

```text
https://mcp.skynetml.com/mcp
```

Le serveur utilise Streamable HTTP. La connexion utilise OAuth 2.1. Vous ne copiez ni jeton ni clé d'API dans le client.

## Connecter votre client

Sélectionnez votre client. Suivez ses étapes, puis passez à [Approuver la connexion](#approuver-la-connexion).

```skynet-mcp-clients

```

## Approuver la connexion

La première fois que l'agent appelle Skynet, votre navigateur ouvre une page Skynet.

1. Connectez-vous à Skynet si ce n'est pas déjà fait.
2. Lisez la page. Elle affiche le nom de l'agent, le site vers lequel il revient et votre compte.
3. Lisez ce que l'agent peut faire :
   - Voir vos exécutions d'optimisation et leurs résultats.
   - Lancer et arrêter des exécutions d'optimisation, payées sur votre solde.
4. Cliquez sur **Allow** pour vous connecter. Cliquez sur **Deny** pour refuser.

Une fois que vous avez cliqué sur **Allow**, le navigateur revient à l'agent. L'agent peut alors appeler les outils ci-dessous.

> [!NOTE]
> L'agent fixe un plafond de dépenses pour chaque exécution. Il ne peut pas ajouter de fonds.

## Outils

| Outil       | Rôle                                                                                         | Accès    |
| ----------- | -------------------------------------------------------------------------------------------- | -------- |
| `check_run` | Vérifie la configuration d'une exécution sans la lancer ni vous facturer. Suggère un plafond de dépenses. | Lecture  |
| `start_run` | Lance une exécution avec un plafond de dépenses.                                             | Écriture |
| `get_run`   | Renvoie le statut, les dépenses et, à la fin de l'exécution, le résultat.                    | Lecture  |
| `list_runs` | Liste vos exécutions, de la plus récente à la plus ancienne. Peut filtrer par statut.        | Lecture  |
| `stop_run`  | Arrête une exécution. Un second appel renvoie une erreur indiquant que l'exécution est déjà terminée. | Écriture |

L'accès en lecture utilise la portée `runs:read`. L'accès en écriture utilise la portée `runs:write`.

Aucun outil n'ajoute de fonds, ne modifie votre solde ni ne change les paramètres de facturation.

## Comment un agent lance une optimisation

1. L'agent appelle `check_run` avec la configuration de l'exécution. Skynet effectue les vérifications statiques et renvoie :
   - Le résultat de chaque vérification.
   - Le plafond de dépenses le plus bas avec lequel l'exécution peut démarrer.
   - Un plafond de dépenses suggéré. Il est d'au moins 5,00 $.
   - Les modèles dont le prix est inconnu.
   - Le montant que vous pouvez dépenser maintenant.
2. L'agent appelle `start_run` avec un plafond de dépenses (`max_cost_cents`, de 1 à 1 000 000 cents de dollar américain). Il peut aussi envoyer une `idempotency_key`, pour qu'une nouvelle tentative ne lance pas de seconde exécution.
3. Si la vérification de la configuration est toujours en cours, `start_run` renvoie `in_progress` et `retry_after_seconds`. Passé ce délai, l'agent rappelle `start_run` avec la même entrée.
4. L'agent appelle `get_run` jusqu'à la fin de l'exécution. Les statuts finaux sont `success`, `failed`, `cancelled` et `stopped`.

> [!TIP]
> Indiquez votre plafond de dépenses à l'agent dans votre prompt, par exemple « Optimise ce dépôt. Dépense au maximum 10 $. » L'agent utilise alors ce plafond dans `start_run`.

L'exécution s'arrête quand elle atteint son plafond de dépenses. Lisez [Facturation](/docs/concepts/billing) pour savoir comment Skynet facture une exécution.

## Sécurité

- Chaque client reçoit sa propre autorisation. Le client ne voit jamais votre mot de passe.
- La connexion utilise PKCE (S256). Le client s'enregistre par enregistrement dynamique de client ou par un document de métadonnées d'identifiant client.
- Les jetons d'accès expirent au bout d'une heure. Le client les renouvelle avec un jeton d'actualisation.
- Un jeton d'actualisation expire après 30 jours sans utilisation. Une autorisation dure au maximum 90 jours. Vous devez ensuite approuver à nouveau la connexion.
- Chaque jeton d'actualisation ne fonctionne qu'une seule fois. Si quelqu'un réutilise un ancien jeton d'actualisation, Skynet met fin à l'autorisation.
- L'agent ne peut voir et modifier que vos propres exécutions.

Lisez [Sécurité](/docs/security) pour savoir comment Skynet protège votre compte et vos données.

## Déconnexion

Pour déconnecter un agent, supprimez le serveur Skynet du client. La plupart des clients révoquent alors leurs jetons. Si un client ne les révoque pas, son jeton d'actualisation expire après 30 jours sans utilisation.

## Dépannage

| Problème                                           | Que faire                                                                      |
| -------------------------------------------------- | ------------------------------------------------------------------------------ |
| La connexion échoue immédiatement                  | Vérifiez l'URL du serveur. Elle doit être exactement `https://mcp.skynetml.com/mcp`. |
| « This agent can't connect »                       | Relancez la connexion depuis le client.                                        |
| « Too many attempts »                              | Patientez quelques minutes, puis réessayez.                                    |
| Le client n'ouvre pas de navigateur                | Mettez le client à jour. Il doit prendre en charge les serveurs MCP distants avec OAuth. |
| `start_run` échoue avec une erreur de solde        | Ajoutez des fonds dans Skynet, ou lancez l'exécution avec un plafond de dépenses plus bas. |
| Les outils cessent de fonctionner après quelques semaines | L'autorisation a expiré. Supprimez le serveur et reconnectez-vous.      |

Pour un accès HTTP direct sans agent, lisez [API](/docs/api).

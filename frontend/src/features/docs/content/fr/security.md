Cette page décrit comment Skynet protège votre compte, vos clés et vos données. Elle explique aussi comment signaler un problème de sécurité.

## Signaler une vulnérabilité

Signalez les problèmes de sécurité en privé. N'ouvrez pas d'issue publique.

- Sur GitHub, ouvrez le dépôt Skynet et allez dans **Security** > **Report a vulnerability**.
- Ou envoyez un e-mail à `security@skynetml.com`.

L'équipe s'efforce d'accuser réception de chaque signalement sous cinq jours ouvrés.

## Votre compte

### Méthodes de connexion

Vous pouvez vous connecter avec :

- Une adresse e-mail et un mot de passe.
- **Continuer avec Google** ou **Continuer avec GitHub**.
- **Se connecter avec une clé d'accès**. Une clé d'accès utilise Face ID, Touch ID ou une clé matérielle.

Vous pouvez gérer les fournisseurs que vous utilisez sous **Comptes associés**.

### Règles des mots de passe

Un mot de passe doit :

- Comporter au moins 8 caractères.
- Comporter au plus 128 caractères.
- Ne pas contenir votre adresse e-mail.

Un nouveau compte doit vérifier son adresse e-mail avec un code. Une réinitialisation du mot de passe utilise aussi un code que Skynet envoie par e-mail.

Après plusieurs tentatives échouées, Skynet bloque la connexion pour cette adresse e-mail pendant un certain temps.

### Vérification en deux étapes

Activez la vérification en deux étapes pour une meilleure protection. Choisissez une méthode :

| Méthode                              | Fonctionnement                                                    |
| ------------------------------------ | ----------------------------------------------------------------- |
| **Application d'authentification**   | Saisissez un code à 6 chiffres issu de votre application d'authentification. |
| **Codes par e-mail**                 | Saisissez un code à 6 chiffres que Skynet envoie à chaque connexion. |

Skynet vous fournit aussi des codes de récupération. Conservez-les en lieu sûr. Utilisez-en un si vous perdez l'accès à votre méthode.

> [!NOTE]
> Si vous vous connectez avec Google ou GitHub, c'est ce fournisseur qui gère votre vérification en deux étapes.

## Où s'exécute votre code

Les charges de travail des utilisateurs s'exécutent dans des sandboxes isolées. Dans le produit hébergé, Vercel Sandbox constitue la frontière d'exécution. Cela couvre les vérifications de configuration, les exécutions d'optimisation, la reprise et le travail sur les exécutions terminées.

Les workers Skynet n'exécutent pas votre code. Ils constituent le plan de contrôle. Ils lancent les tâches, réservent et règlent les budgets, transmettent des identifiants à portée limitée aux sandboxes et enregistrent les points de contrôle.

Avant de lancer un travail payant, Skynet réserve une marge dans votre portefeuille et dans le budget de l'exécution. Voir [Facturation](/docs/concepts/billing).

Dans l'[app Mac](/docs/guides/mac-app), les tâches s'exécutent dans des conteneurs Docker locaux. Tous les services locaux écoutent uniquement sur `127.0.0.1`.

## Clés de fournisseur (BYOK)

Vous pouvez ajouter vos propres clés de fournisseur de modèles.

- Skynet chiffre chaque clé au repos.
- Skynet ne renvoie jamais la clé complète via l'API.
- Skynet ne conserve que les quatre derniers caractères et un statut : non vérifiée, vérifiée ou invalide.
- Quand vous saisissez une clé, Skynet envoie une petite requête de test pour la vérifier.

Votre fournisseur facture les jetons du modèle sur votre clé. Skynet ne facture aucuns frais de modèle. Le budget de l’exécution couvre uniquement l’utilisation du bac à sable.

Skynet retire aussi les secrets des journaux et des messages d'erreur. Il les remplace par `[REDACTED]`.

## Jetons d'API

Les jetons d'API permettent à des scripts et à des outils d'appeler l'API Skynet.

- Chaque utilisateur possède au plus un jeton valide. Quand vous générez un nouveau jeton, l'ancien cesse de fonctionner.
- Les jetons commencent par `skyd_`.
- Skynet ne stocke qu'une empreinte (hash) du jeton.
- Skynet n'affiche le jeton complet qu'une seule fois. Copiez-le et conservez-le immédiatement.
- Ensuite, Skynet n'en affiche qu'une forme masquée : `skyd_…` suivi des quatre derniers caractères.

Envoyez le jeton dans l'en-tête `Authorization` :

```http
Authorization: Bearer skyd_...
```

Pour désactiver un jeton, cliquez sur **Révoquer le jeton**. Pour l'API complète, lisez [API](/docs/api).

> [!WARNING]
> Traitez le jeton comme un mot de passe. Toute personne qui détient le jeton peut agir en votre nom via l'API. Ne le committez pas dans un dépôt.

## Confidentialité des données des modèles

Le paramètre **Confidentialité des données des modèles** détermine ce que les fournisseurs de modèles peuvent faire de vos données. Il s'applique aux optimisations que Skynet facture sur votre solde.

| Option                   | Effet                                                                                               |
| ------------------------ | --------------------------------------------------------------------------------------------------- |
| **Tout autoriser**       | N'importe quel fournisseur, y compris ceux qui peuvent s'entraîner sur vos prompts ou les stocker.  |
| **Sans entraînement**    | Uniquement les fournisseurs qui ne s'entraînent pas sur vos prompts et ne les conservent pas. C'est l'option par défaut. |
| **Aucune conservation**  | Uniquement les points de terminaison couverts par un accord de non-conservation des données. Certains modèles peuvent être indisponibles. |

> [!NOTE]
> Les appels BYOK via OpenRouter suivent les paramètres de confidentialité de votre compte OpenRouter. Les appels BYOK directs suivent la politique de données du fournisseur.

## Visibilité des exécutions

> [!WARNING]
> Les nouvelles exécutions sont **Publiques** par défaut. Une exécution publique apparaît dans la recherche publique. Tout le monde peut la trouver et parcourir ses résultats.

Pour exclure une exécution de la recherche publique, réglez **Visibilité** sur **Privé** lorsque vous la soumettez. Vous pouvez modifier la visibilité plus tard dans la boîte de dialogue de partage. Voir [Partage](/docs/guides/sharing).

Les nouveaux jeux de données sont **Restreints**. Seuls vous et les personnes que vous invitez pouvez les ouvrir, tant que vous ne modifiez pas l'accès par lien. Voir [Préparer un jeu de données](/docs/guides/datasets).

## Vos données

Ouvrez l'onglet **Confidentialité et données** dans les **Paramètres** pour gérer vos données.

### Exporter vos données

Cliquez sur **Exporter** sous **Exporter mes données**. L'export contient toutes les données de votre compte. Choisissez un format : JSON, CSV, Excel, Pickle, Parquet ou Feather.

### Supprimer votre compte

Cliquez sur **Supprimer le compte**. Saisissez votre mot de passe actuel et tapez votre adresse e-mail pour confirmer.

> [!WARNING]
> Cette action supprime définitivement votre compte et toutes ses données. Elle est irréversible.

### Statistiques d'utilisation

Le paramètre **Envoyer des statistiques d'usage anonymes** active ou désactive les statistiques anonymes du produit.

## Étapes suivantes

- Lisez [Limites](/docs/limits).
- Lisez la [FAQ](/docs/faq).

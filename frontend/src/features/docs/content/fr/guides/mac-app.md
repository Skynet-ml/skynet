L'app Mac Skynet exécute Skynet sur votre propre ordinateur. Elle envoie les appels de modèle par l'intermédiaire d'une CLI de code connectée sur votre Mac, si bien que l'abonnement de votre CLI paie ces appels. Vous pouvez aussi associer l'app à votre compte Skynet hébergé.

## Prérequis

- Un Mac avec une puce Apple. Des builds Intel existent, mais personne ne les a testés sur du matériel Intel.
- Docker Desktop, en cours d'exécution, avec l'option **Use Rosetta for x86_64/amd64 emulation** activée.
- Une CLI de code, connectée :
  - Claude Code (`claude`) 2.1.226 ou plus récent, ou
  - Codex (`codex`) 0.160.0 ou plus récent.

> [!WARNING]
> Codex doit utiliser une connexion ChatGPT. Une connexion avec une clé API ne compte pas. Une connexion conservée dans le trousseau macOS ne compte pas non plus.

### Configurer Codex

Codex doit conserver ses identifiants dans un fichier. Effectuez ces étapes avant le premier lancement :

1. Ouvrez `~/.codex/config.toml` et ajoutez cette ligne :

```toml
cli_auth_credentials_store = "file"
```

2. Connectez-vous avec votre compte ChatGPT :

```bash
codex login
```

## Installer l'app

Les builds sont signés ad hoc et ne sont pas notariés. macOS les bloque tant que vous n'avez pas retiré l'attribut de quarantaine.

1. Ouvrez le fichier `.dmg`.
2. Faites glisser **Skynet** dans **Applications**.
3. Exécutez cette commande dans le Terminal :

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Ouvrez Skynet depuis **Applications**.

> [!NOTE]
> Le premier lancement prend plusieurs minutes. L'app construit une image de sandbox locale et prépare sa base de données.

## Ce qui se passe au lancement

L'app effectue ces étapes dans l'ordre :

1. Elle lit le `PATH` de votre shell de connexion, afin de trouver Docker et votre CLI.
2. Elle vérifie Docker et votre CLI de code.
3. Elle démarre une base de données Postgres locale dans Docker.
4. Elle construit l'image de sandbox `skynet-local-sandbox` pour `linux/amd64`. Docker l'exécute sous Rosetta.
5. Elle met à jour le schéma de la base de données.
6. Elle démarre l'API locale et l'application web, et affiche l'application web dans la fenêtre.

### Vérifications de démarrage

L'app affiche une liste de vérifications : **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** et **Account**. Si une vérification échoue, l'app affiche un écran qui vous indique quoi faire :

| Écran                      | Que faire                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------- |
| **Install Docker Desktop** | Installez Docker Desktop.                                                          |
| **Start Docker**           | Démarrez Docker Desktop.                                                           |
| **Install a coding CLI**   | Installez Claude Code ou Codex.                                                    |
| **Update your coding CLI** | Mettez à jour la CLI vers la version minimale indiquée plus haut.                  |
| **Sign in to your CLI**    | Connectez-vous à la CLI dans le Terminal.                                          |
| **Usage limit reached**    | Attendez l'heure de réinitialisation. De nouvelles tâches peuvent démarrer après la réinitialisation. |

Cliquez sur **Check again** après avoir corrigé le problème. Cliquez sur **Open logs** pour voir les fichiers journaux. Sur l'écran **Usage limit reached**, vous pouvez cliquer sur **Open Skynet anyway**.

## Exécutions locales

Dans l'app, les appels de modèle passent par la CLI de votre Mac. Le menu des modèles affiche ce mode sous le nom **Local**. L'indication précise : « Votre abonnement paie les appels, ce modèle n'ajoute donc 0 $ à l'exécution. »

Dans l'app, les CLI s'appellent **Claude Code** et **Codex**.

> [!NOTE]
> L'app retire les clés API de l'environnement des processus qu'elle démarre. Ainsi, la CLI utilise votre abonnement, et non une clé API.

### Exécuter le proposeur sur votre CLI

Le paramètre **Exécuter le proposeur sur ma CLI** détermine où s'exécute l'agent proposeur. Les choix sont **Moteur de l'agent (par défaut)** et **Désactivé (proposeur isolé)**.

**Accès du proposeur** définit ce à quoi la CLI peut accéder :

- **Espace de travail uniquement** est la valeur par défaut. Ce mode s'appuie sur la sandbox propre à chaque CLI.
- **Accès complet** exécute la CLI avec vos privilèges d'utilisateur.

> [!WARNING]
> Avec **Accès complet**, la CLI peut lire, modifier et supprimer tous les fichiers auxquels vous avez accès, et accéder au réseau. Le mode acceptEdits de Claude n'isole pas le réseau, même avec **Espace de travail uniquement**.

### Étiquetage dans l'app

L'étiquetage par IA dans l'app Mac nécessite votre propre clé de fournisseur. Choisissez un modèle sous **Votre propre clé**. Voir [Annoter des données avec l'outil d'annotation](/docs/guides/tagger).

## Associer votre compte Skynet

Vous pouvez utiliser l'app sans compte. Cliquez pour cela sur **Use without signing in**. Pour synchroniser avec votre compte hébergé, associez l'app.

### Associer ce Mac

1. À l'étape **Sign in to Skynet**, l'app ouvre la page **Link a Mac** dans votre navigateur par défaut.
2. Connectez-vous à Skynet dans le navigateur s'il vous le demande.
3. Vérifiez la liste sous **Une fois associé, ce Mac pourra :**.
4. Cliquez sur **Associer ce Mac**. La page affiche « Retour à l’app Mac… ».

Si la connexion échoue ou si vous annulez, l'app affiche **Try again** et **Use without signing in**.

Une fois associé, le Mac peut :

- Voir votre nom et votre e-mail.
- Voir votre solde.
- Synchroniser vos exécutions avec votre compte.
- Utiliser des modèles hébergés, payés sur votre solde.
- Utiliser vos sources de données connectées.
- Rechercher et naviguer sur le web par l'intermédiaire de Skynet.

L'association utilise un parcours de connexion sécurisé (OAuth avec PKCE). L'app stocke ses jetons chiffrés sur votre Mac. Quand vous dissociez le Mac, Skynet révoque les jetons.

### Limites de l'association

| Limite                              | Valeur                                    |
| ----------------------------------- | ----------------------------------------- |
| Mac associés par compte             | 10                                        |
| Durée de vie du jeton d'accès       | 1 heure, puis l'app le renouvelle         |
| Délai d'inactivité de l'association | 30 jours sans utilisation                 |
| Durée maximale de l'association     | 365 jours                                 |

Après le délai d'inactivité ou la durée maximale, associez à nouveau le Mac.

### État de l'association

L'app affiche l'un de ces états : **Mode local**, **Associé à ce Mac**, **Se connecter pour synchroniser** ou **Associé (hors ligne)**.

## Services et données locaux

Tous les services locaux n'écoutent que sur `127.0.0.1` :

| Service         | Plage de ports  |
| --------------- | --------------- |
| API             | 48100 à 48119   |
| Application web | 48120 à 48139   |
| Postgres        | 48140 à 48159   |

L'app conserve ses données dans `~/Library/Application Support/Skynet`. Le dossier `logs/` contient `desktop.log`, `api.log`, `frontend.log`, `migrate.log` et `sandbox-build.log`.

Quand vous quittez l'app, elle arrête l'application web, l'API, Postgres et tous les conteneurs de tâches.

### Réinitialiser l'app

> [!WARNING]
> Ces commandes suppriment toutes les données locales, y compris vos exécutions et jeux de données locaux.

Quittez d'abord l'app. Puis exécutez :

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Étapes suivantes

- [Lancer une exécution](/docs/guides/submit).
- Lisez la page sur la [facturation](/docs/concepts/billing) pour les modèles hébergés.
- Lisez [Sécurité](/docs/security).

Cette page répond aux questions fréquentes sur Skynet. Chaque réponse renvoie à la page qui donne tous les détails.

## Général

### Qu'est-ce que Skynet ?

Skynet est une plateforme qui optimise des prompts, des programmes, des agents et des workflows à partir de vos propres données et de votre métrique. Vous importez un jeu de données, décrivez la tâche et lancez une exécution. L'exécution essaie de nouvelles versions et les note sur des données mises de côté. Pour commencer, lisez le [Démarrage rapide](/docs/quickstart).

### Que peut optimiser Skynet ?

Skynet, c'est Optimize Anything. Il optimise des prompts, du code, du texte et des programmes DSPy dans un seul produit. Le type d'artefact détermine les moteurs qui peuvent travailler dessus :

- Les artefacts Prompt, Code et Texte utilisent les moteurs natifs : GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler et ShinkaEvolve.
- Les programmes DSPy utilisent les optimiseurs DSPy : DSPy GEPA, DSPy MIPROv2 et DSPy BootstrapFewShot.

**Auto** combine les moteurs adaptés à votre tâche. Voir [Fonctionnement de l'optimisation](/docs/concepts/optimization) et [Moteurs](/docs/concepts/engines).

### Quelles langues l'interface prend-elle en charge ?

L'interface est disponible en 24 langues. L'hébreu est la langue de base, et la mise en page prend en charge le texte de droite à gauche. La documentation en hébreu se trouve sur [/docs/he](/docs/he).

### Skynet est-il open source ?

Oui. Le code est publié sous licence AGPL-3.0. Si vous proposez une version modifiée de Skynet sous forme de service, la licence vous oblige à partager vos modifications.

## Données

### Quels types de fichiers puis-je importer ?

Vous pouvez importer des fichiers CSV, JSON, `.xlsx` et `.xls`. Pour les fichiers Excel, Skynet ne lit que la première feuille. Voir [Préparer un jeu de données](/docs/guides/datasets).

### Mes données n'ont pas d'étiquettes. Que faire ?

Utilisez l'outil d'annotation. Vous pouvez étiqueter les lignes vous-même, ou laisser un modèle d'IA les étiqueter pendant que vous vérifiez son travail. Voir [Annoter des données avec l'outil d'annotation](/docs/guides/tagger).

### Qui peut voir mes exécutions et mes jeux de données ?

> [!WARNING]
> Les nouvelles exécutions sont **Publiques** par défaut. Une exécution publique apparaît dans la recherche publique, et tout le monde peut parcourir ses résultats.

Réglez **Visibilité** sur **Privé** lorsque vous soumettez une exécution pour l'exclure de la recherche publique. Les nouveaux jeux de données sont **Restreints** : seuls vous et les personnes que vous invitez pouvez les ouvrir. Voir [Partage](/docs/guides/sharing) et [Sécurité](/docs/security).

### Les fournisseurs de modèles s'entraînent-ils sur mes prompts ?

Par défaut, non. L'option par défaut de **Confidentialité des données des modèles** est **Sans entraînement**. Elle n'envoie les prompts qu'à des fournisseurs qui ne s'entraînent pas dessus et ne les conservent pas. Ce paramètre s'applique aux appels que Skynet facture sur votre solde. Les appels BYOK via OpenRouter suivent les paramètres de confidentialité de votre compte OpenRouter. Les appels BYOK directs suivent la politique de données du fournisseur. Voir [Sécurité](/docs/security).

## Facturation

### Comment Skynet facture-t-il les exécutions ?

Skynet utilise un solde prépayé. L'utilisation des modèles gérés et leur majoration, ainsi que l'utilisation de la sandbox, sont mesurées. Des frais s'appliquent aux recharges. Avant le début de tout travail payant, Skynet réserve une marge dans votre portefeuille et dans le budget de l'exécution. Voir [Facturation](/docs/concepts/billing).

### Puis-je utiliser ma propre clé de fournisseur de modèles ?

Oui. Avec votre propre clé (BYOK), votre fournisseur facture les jetons du modèle sur votre clé. Skynet ne facture aucuns frais de modèle. Le budget de l’exécution couvre uniquement l’utilisation du bac à sable. Skynet chiffre votre clé et ne l'affiche plus jamais. Voir [Facturation](/docs/concepts/billing) et [Sécurité](/docs/security).

### Puis-je utiliser Skynet sans payer les appels de modèle ?

L'[app Mac](/docs/guides/mac-app) exécute Skynet sur votre propre ordinateur. Elle fait passer les appels de modèle par Claude Code ou Codex, connectés sur votre Mac. Votre abonnement CLI paie ces appels : ils ajoutent donc 0 $ à l'exécution.

## Outils et intégrations

### Puis-je piloter Skynet depuis mon propre client IA ?

Oui. Skynet dispose d'un serveur MCP à l'adresse `https://mcp.skynetml.com/mcp`. Vous vous connectez avec OAuth. Le serveur propose les outils suivants : `check_run`, `start_run`, `get_run`, `list_runs` et `stop_run`. Voir [MCP](/docs/mcp).

### Existe-t-il une API ?

Oui. Générez un jeton d'API dans les Paramètres, puis envoyez-le sous la forme `Authorization: Bearer <token>`. Chaque utilisateur possède un seul jeton valide à la fois. Voir [API](/docs/api).

### Le panneau de l'agent peut-il supprimer mes exécutions ?

Seulement si vous l'y autorisez. Dans le mode par défaut **Demander**, l'agent demande avant chaque action. En mode **Automatique**, il demande quand même avant les actions destructrices, comme une suppression ou une soumission. Voir [Panneau de l'agent](/docs/guides/agent-panel).

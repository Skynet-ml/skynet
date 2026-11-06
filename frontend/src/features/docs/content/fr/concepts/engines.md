Un moteur est la méthode de recherche qui propose de nouvelles versions de votre artefact. Skynet a six moteurs natifs pour les dépôts, trois optimiseurs DSPy pour les programmes DSPy, et **Auto**. Cette page explique ce que fait chacun, quand le choisir et quels paramètres il a.

Lisez d'abord [Fonctionnement de l'optimisation](/docs/concepts/optimization) si vous ne savez pas quels moteurs conviennent à votre artefact.

## Comparaison rapide

| Moteur                | Types d'artefacts    | Paramètres de l'agent de code              | Paramètre Itérations max |
| --------------------- | -------------------- | ------------------------------------------ | ------------------------ |
| Auto | Programme DSPy | Non | Non |
| GEPA | Dépôt | Non | Non |
| Best-of-N | Dépôt | Non | Non |
| AutoResearch | Dépôt | Oui | Non |
| Meta-Harness | Dépôt | Oui | Oui |
| AutoSaddler | Dépôt | Oui | Oui |
| ShinkaEvolve | Dépôt | Non. Il a son propre paramètre d'éditeur. | Oui |
| DSPy GEPA             | Programme DSPy       | Non                                        | Non                      |
| DSPy MIPROv2          | Programme DSPy       | Non                                        | Non                      |
| DSPy BootstrapFewShot | Programme DSPy       | Non                                        | Non                      |

L'assistant signale GEPA, DSPy GEPA et Auto comme des optimiseurs qui utilisent le texte de retour de votre métrique.

## Auto

Auto est l'option par défaut. Ce qu'il exécute dépend du type d'artefact :

- **Programme DSPy** : Auto exécute DSPy GEPA.
- **Dépôt** : Auto n'est pas disponible. Choisissez un moteur natif.

**Choisissez-le quand** vous ne savez pas quel moteur convient. C'est le meilleur point de départ.

## GEPA

« Évolution réflexive avec un front de Pareto de versions. » Le modèle de réflexion lit le score et le retour de chaque cas et écrit une meilleure version. GEPA conserve un front de versions qui sont chacune les meilleures sur certains cas.

**Choisissez-le quand** votre métrique donne un retour précis et que vous voulez un moteur unique performant.

## Best-of-N

« Propositions indépendantes du modèle de réflexion ; on garde la meilleure. » Chaque proposition est indépendante des autres. Skynet note à nouveau les meilleures propositions sur les cas de validation.

**Choisissez-le quand** vous voulez une référence simple à comparer avec d'autres moteurs.

## AutoResearch

Un agent de code mène des cycles de recherche. Il modifie, note et conserve ce qui aide, jusqu'à épuisement du budget.

**Choisissez-le quand** une bonne modification demande plusieurs étapes d'investigation.

## Meta-Harness

Un agent de code propose un lot de candidats à chaque itération. Il s'appuie sur l'historique complet des candidats et de leurs scores.

**Choisissez-le quand** vous voulez un agent qui apprend de l'historique de toutes les tentatives précédentes. Utilisez **Candidats par itération** pour définir la taille du lot.

## AutoSaddler

Un agent de code lit le retour de votre métrique et corrige la version. Skynet ne conserve que les correctifs qu'une nouvelle exécution de l'évaluateur confirme.

**Choisissez-le quand** le retour de votre métrique pointe des défauts clairs et localisés.

## ShinkaEvolve

« Fait évoluer de nombreuses versions dans des îles parallèles et apprend quels modèles les améliorent le plus. » C'est une recherche évolutionnaire sur une population de candidats, avec des mutations et des croisements écrits par un modèle.

**Choisissez-le quand** vous voulez une exploration large et que vous disposez d'un budget plus important.

## Optimiseurs DSPy

| Optimiseur            | Ce qu'il fait                                                                                                                     |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| DSPy GEPA             | Lit le retour de la métrique et fait évoluer les instructions de chaque prédicteur.                                               |
| DSPy MIPROv2          | Propose ensemble des instructions et des démonstrations few-shot, et explore leurs combinaisons par optimisation bayésienne.       |
| DSPy BootstrapFewShot | Collecte des exemples résolus que le programme réussit et les ajoute comme démonstrations few-shot. Il n'utilise pas de modèle de réflexion. |

DSPy MIPROv2 et DSPy BootstrapFewShot ont besoin de paires (des cas avec des sorties attendues).

## Paramètres

### Budget

| Paramètre                        | Par défaut  | Plage                                                           |
| -------------------------------- | ----------- | --------------------------------------------------------------- |
| Exécutions du scoreur max.       | 200         | 1 à 100000                                  |
| Arrêter au score                 | Désactivé   | 0 ou plus                                                       |
| Itérations max.                  | Désactivé   | 1 à 1000. Uniquement Meta-Harness, AutoSaddler et ShinkaEvolve. |

Une exécution de l'évaluateur correspond à un appel de votre métrique sur un cas. La notation finale de la version de départ et de la version gagnante ne compte pas dans **Exécutions du scoreur max.**

### Paramètres de l'agent de code

Ces paramètres s'appliquent à AutoResearch, Meta-Harness et AutoSaddler.

| Paramètre                          | Par défaut | Plage                                                    |
| ---------------------------------- | ---------- | -------------------------------------------------------- |
| Agent de code                      | Codex      | Pi, Codex, OpenCode, Prime Agent, commande personnalisée |
| Candidats par itération            | 3          | 1 à 8. |
| Appels d'outils max. par candidat  | 100        | 1 à 1000                                                 |

### Paramètres de ShinkaEvolve

| Paramètre                                                    | Par défaut                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------------ |
| Mode d'écriture des nouvelles versions                       | Un seul appel de modèle. L'autre option est « Agent de code (Pi) ». |
| Îles                                                         | 2 (1 à 8)                                                          |
| Intervalle de migration                                      | 10                                                                 |
| Sélection des parents                                        | Pondérée. Autres options : loi de puissance, recherche en faisceau. |
| Modifications par diff / Réécritures complètes / Croisements | 60 % / 30 % / 10 %. La somme doit faire 100 %.                     |
| Taille de l'archive                                          | 40                                                                 |
| Évaluations parallèles                                       | 2                                                                  |
| Propositions parallèles                                      | 2                                                                  |

## Pages associées

- [Fonctionnement de l'optimisation](/docs/concepts/optimization)
- [Métriques et évaluateurs](/docs/concepts/scorers)
- [Exécutions](/docs/concepts/runs)
- [Facturation](/docs/concepts/billing)

# Truth Politicians

**Outil libre de collecte, de vérification et de préparation éditoriale pour comparer les déclarations politiques, les mesures et les votes à partir de preuves consultables.**

Truth Politicians contient un backend Django et son interface de travail : sources, dossiers, recherche documentaire, vérification, revue humaine, corrections et exports. Il s'installe indépendamment d'un site de publication. Chaque utilisateur choisit son hébergement et, s'il souhaite diffuser ses résultats, son site destinataire.

## Ce que contient le dépôt

| Dossier | Fonction |
|---|---|
| [outil/](outil/) | Application, interface interne, migrations et dépendances. |
| [outil/contracts/](outil/contracts/) | Contrats JSON versionnés et exemples synthétiques pour les échanges. |
| [outil/docs/](outil/docs/) | Méthode, sécurité, modèle documentaire et raccordement. |
| [outil/deploy/docker/](outil/deploy/docker/) | Exemple facultatif pour exécuter l'outil en conteneur. |
| [outil/scripts/](outil/scripts/) | Construction, exports et vérifications. |
| [outil/atelier/tests/](outil/atelier/tests/) | Tests applicatifs et contractuels. |

Le dépôt distribue l'outil. Le design, les pages, le contenu et l'hébergement du site de destination appartiennent à chaque installation. L'interface interne sert à travailler sur les dossiers ; elle fait partie de Truth Politicians.

## Fonctions et méthode

- Collecter et normaliser des sources en conservant leur provenance, leur version et les références vérifiables.
- Distinguer personnes, partis, groupes et affiliations datées ; vérifier le texte exact et la procédure des scrutins.
- Préparer des rapprochements entre déclarations, mesures et votes, avec preuves, limites et contre-éléments.
- Séparer les faits vérifiés, les hypothèses et les interprétations. Une absence de données, une abstention, une non-participation et un vote contre ne sont pas interchangeables.
- Soumettre les comparaisons interprétatives à une revue humaine rattachée à leur version exacte. Une divergence ne prouve pas une intention mensongère.
- Préparer des exports contrôlés, suivre les reçus, gérer les corrections et les retraits.

Une source officielle ne rend pas officielle l'interprétation produite par l'outil. La couverture documentaire et l'automatisation dépendent des sources disponibles, des droits de réutilisation et des contrôles de chaque installation.

## Démarrage

Le [guide d'installation de l'outil](outil/README.md) décrit la création d'un environnement Python, d'une base locale et des premiers comptes. Aucun site, compte d'un fournisseur d'IA ou service payant n'est nécessaire pour ce démarrage.

Le réseau de collecte et les envois sont désactivés par défaut. Les données de travail et les secrets sont fournis par l'administrateur de chaque installation et conservés hors Git.

## Publication vers le site de son choix

L'outil produit des données structurées et versionnées. Un site ou un service relais peut les consommer en respectant les contrats, les états de revue, les réserves et les preuves. Un relais hébergé est un mode d'intégration possible ; ce dépôt n'en exploite aucun.

Le connecteur WordPress existant est facultatif. Son URL, ses identifiants et les hôtes autorisés doivent être configurés explicitement. Il ne constitue pas un récepteur universel et aucun site complet n'est inclus. Les autres destinations peuvent utiliser les exports et implémenter leur propre récepteur compatible. Voir [le contrat d'échange](outil/docs/CONTRAT_SITE_CANDIDAT.md).

Les identifiants techniques `dv.*`, les paramètres `DV_*` et le paquet Python `dv_project` sont conservés pour la compatibilité des données et des scripts. Ils ne désignent ni un site imposé, ni une destination préconfigurée. Leur renommage nécessiterait une migration versionnée.

## État du logiciel

Cette distribution vise une installation neuve et isolée. Elle ne contient ni base de production, ni comptes, ni clés, ni corpus privé, ni attestations de revue réelles. Certaines recettes historiques nécessitent des pièces documentaires ou des services externes qui ne sont pas distribués ; leur absence n'est pas une réussite de test.

Les résultats vérifiés de cette révision et les limites connues sont indiqués dans [VALIDATION.md](VALIDATION.md). Une réussite locale ne qualifie pas un déploiement sur Internet, un fournisseur d'analyse ou un récepteur tiers.

## Contributions et licence

Les propositions de modification peuvent être soumises par pull request. Les contributions doivent préserver la traçabilité des preuves, la revue éditoriale, les frontières d'accès et les contrats versionnés. Ne pas ajouter de secrets, de données personnelles d'exploitation ou de corpus non autorisés.

Le code et la documentation originaux sont distribués sous **GNU AGPL version 3 uniquement (`AGPL-3.0-only`)**. Le texte complet est dans [LICENSE](LICENSE) ; les dépendances et contenus de tiers conservent leurs droits propres, décrits dans [NOTICE.md](NOTICE.md).

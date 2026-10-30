# Truth Politicians — scripts techniques

Les scripts sont des commandes locales de développement, d’export ou de vérification. Ils ne chargent pas un corpus privé manquant, ne valident pas une analyse politique et n’installent aucun site public. Exécuter les commandes depuis `outil/`, avec les dépendances de l’application disponibles.

## Livraison et contrats

- `build_release.py` : archive déterministe du code, des contrats, de la documentation et de l’exemple Docker, sous `runtime/releases/`. La liste d’inclusion est `release-include.txt` ; données, secrets et environnements virtuels sont exclus.
- `build_release.ps1` : lanceur Windows du même constructeur.
- `build_contract_bundle.py` : paquet limité à la famille de contrats draft.3 et à ses exemples.
- `verify_site_vector.mjs`, `verify_full_review_vector.mjs`, `verify_indicator_vector.mjs` : vérifications locales des vecteurs synthétiques livrés. Node.js est requis ; aucun site n’est contacté.
- `build_site_review_vector.py`, `build_full_review_vector.py`, `build_racc001_full_review_schema.py` : générateurs d’artefacts de développement. Ils écrivent dans `contracts/` ; ne pas les employer comme commandes d’installation ni comme création d’une revue humaine.

## Exports et notices

Les scripts `export_public_v1.py`, `export_senate_public_v1.py`, `export_delta_v1.py` et `export_coverage_v1.py` travaillent sur des archives, projections et manifestes explicitement fournis. Leurs options sont décrites par `--help`. Les sources réelles et leurs reçus d’intégrité doivent être collectés séparément ; ils ne sont pas inclus dans Git.

`prepare_notices_rec01.py`, `qualify_organizations_rec01.py`, `link_scrutins_rec01.py` et `notices_v2.py` consomment une sélection ordonnée de manifestes. Créer dans la racine de données choisie le fichier privé `runtime/export-inputs.json` :

```json
[
  "runtime/export-an/MANIFEST.json",
  "runtime/export-senate/MANIFEST.json"
]
```

Les chemins sont relatifs à cette racine ; les manifestes et lots doivent rester dans leur périmètre. L’ordre détermine l’application des versions successives d’un même identifiant. Chaque manifeste emploie le format `dv-public-export-v1`, avec les lots et empreintes correspondants. La sélection ne signifie pas que le corpus est exhaustif ou actuel.

Les notices nouvelles s’obtiennent avec `notices_v2.py` après qualification des organisations et production du delta requis. La voie historique `prepare_notices_rec01.py` produit seulement des brouillons ; elle ne les rend pas importables. `verify_notices_rec01.py` reproduit les formats 2 et 3 ; le contrôle d’une ancienne notice demande un index de référence externe explicite via `--reference-index`. Aucun index personnel n’est distribué.

`directory_snapshots.py` et les scripts de liaison vérifient des relations documentaires à partir des entrées fournies. Les graines pédagogiques de `prepare_explain_rec02.py` restent non qualifiées ; la commande n’invente ni date de consultation ni revue humaine.

## Tests et prérequis externes

Les scripts `test_*.py` restants utilisent des cas synthétiques pour les transformations correspondantes. `verify_sqlite_serialization.py` vérifie un comportement local avec une base temporaire. `verify_counter_review_002.py` orchestre les suites applicatives et précise ses options via `--help` ; certaines recettes dépendent de pièces ou de bancs externes absents du dépôt public. Les options d’intégration PHP/WordPress demandent un récepteur et des clés de test fournis séparément. Leur absence ne doit pas être présentée comme un test réussi.

Les recettes qui reconstruisaient un site personnel ou comparaient des archives privées figées ne sont pas distribuées. Les tests applicatifs de l’outil, contrats et contrôles de publication restent dans leurs répertoires propres.

## Contrôles de sources sur le réseau

`build002_live_checks.py` est une commande manuelle distincte qui contacte directement des sources institutionnelles. Elle n’utilise pas le transport protégé de l’application. Son exécution doit donc correspondre au mode de connexion choisi. Les résultats sont écrits par défaut dans `runtime/source-checks/` et ne doivent pas être confondus avec une qualification complète des données.

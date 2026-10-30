# Truth Politicians — validation de la distribution

Vérification du **30 octobre 2026**, sur une installation temporaire Linux x86_64, Python 3.12 et les versions exactes de `outil/requirements.lock`. Les bases de test sont jetables. Aucun environnement de production n'a été utilisé.

## Résultats

| Contrôle | Résultat |
|---|---|
| Installation des dépendances dans un environnement isolé | Réussie. |
| `manage.py check` | Aucune anomalie signalée. |
| Migrations sur base neuve | Réussies, jusqu'à la migration applicative 0037. |
| `manage.py makemigrations --check --dry-run` | Aucun changement détecté. |
| Profil applicatif ciblé | **84 tests réussis**, aucun ignoré. |
| `python -m unittest discover -s tests -v` | **60 tests réussis, 1 ignoré** sur 61 tests trouvés. |
| `python -m unittest discover -s scripts -p 'test_*.py'` | **32 tests réussis**. |
| Construction de l'archive de code | Réussie ; fichiers privés et runtime exclus. |
| Contrats renommés | Changements d'annotations `$id`/`title` seulement ; empreintes des schémas recalculées. |
| Vecteurs signés de référence | Octets conservés. |

Le profil applicatif couvre les modules `test_public_tool_configuration`, `test_quality`, `test_suite004_design`, `test_suite004_hardening`, `test_contract_draft3`, `test_racc001_contract`, `test_site_bridge`, `test_counter_review`, ainsi que `review_regressions` et `review_followup`.

Il vérifie notamment la destination facultative et configurable, le refus d'une liste d'hôtes vide avant résolution réseau, les restrictions HTTPS, la conservation des barrières d'envoi, l'URL complète de l'adaptateur d'analyse, les contrats, la revue et les régressions applicatives. Les scripts disposent de cas synthétiques sur la sélection d'entrées, l'ordre de leurs versions, les empreintes altérées et le confinement des chemins.

Les tests réseau sont exécutés avec le garde `scripts/offline_guard` : les destinations externes sont bloquées ; un test échange uniquement sur `127.0.0.1`. Le premier passage dans un bac à sable interdisant toute création de socket était bloqué par cet environnement ; le rejeu avec sockets locales autorisées a réussi. Aucun test ne constitue une publication vers un site réel.

## Contrôles non exécutés

Le contrôle historique `test_37_liot_original_is_not_authorized` requiert une pièce documentaire privée exclue de cette distribution. Il est explicitement ignoré lorsqu'elle manque ; aucune pièce ni validation de remplacement n'est inventée. Les autres tests synthétiques de la barrière de publication restent exécutés.

La suite historique complète n'est pas couverte par ce profil. Certaines recettes BUILD002 et un contrôle de la matrice documentaire nécessitent des fragments institutionnels ou une pièce privée absents. Les intégrations PHP et WordPress demandent un banc externe et ses clés de test. Elles restent non qualifiées ici.

L'exemple Docker a été inspecté mais n'a pas été construit ni démarré. Windows, macOS, les autres architectures, MariaDB, les collectes institutionnelles réelles, un fournisseur d'analyse et un site destinataire réel n'ont pas été qualifiés dans cette vérification.

## Compatibilité et portée

Le paquet vise une installation neuve. Les migrations et identifiants de contrats existants restent conservés. Les nouveaux noms de configuration d'analyse et de métadonnées de capture ne migrent pas les anciennes bases et ne réattestent pas des contenus historiques.

Ces résultats valident le recentrage de cette distribution sur l'outil et les comportements couverts. Ils ne constituent pas une qualification de production, une preuve d'exhaustivité des sources ou une autorisation de publication d'analyses politiques.

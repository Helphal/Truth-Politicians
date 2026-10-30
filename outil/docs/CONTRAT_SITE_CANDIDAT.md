# Truth Politicians — contrats de raccordement facultatif

L’outil fonctionne sans site public. Les données autorisées peuvent être exportées vers le site ou le service choisi par l’administrateur. Le dépôt contient les contrats, exemples synthétiques, vérificateurs et clients ; il ne fournit pas le site complet ni une réception automatiquement compatible.

## Versions et compatibilité

Les versions `dv.*`, identifiants de destination, règles de canonicalisation et contextes de signature sont des identifiants techniques conservés. Un nom `dv-site:test`, `dv-site:private` ou `dv-site:production` identifie un contexte de protocole ; ce n’est pas une URL ni un domaine imposé.

Les annotations `$id` des schémas utilisent l’espace de noms de Truth Politicians. Ce changement modifie leurs empreintes de fichier ; les constantes de version, champs et vecteurs signés restent conservés. Un récepteur qui épingle les anciens octets doit explicitement vérifier les nouveaux schémas avant adoption.

| Artefact | SHA-256 de la copie publique |
|---|---|
| `dv-content-0.4.0-draft.3.schema.json` | `59c139266a0558f4aee285aaa385b46f325fa2a640181e91ce97e6ef9d28d42e` |
| `dv-event-0.2.0-draft.1.schema.json` | `8f9ea578bdf4b12030d13033db40f96b7d8d3c0162ee69da8e6d89be1598fe47` |
| `dv-full-review-0.1.0-draft.2.schema.json` | `b1716a74c2d1dcaab3e8c99b2ef57c361d1d492e32e9ee9c9e69bf77e2ed8281` |

La présence de ces schémas ne constitue aucune acceptation par un site extérieur. Les formats historiques `dv-site-bridge/0.1.0-draft.1`, les événements `dv.event/*` et les autres contrats présents ne sont pas interchangeables. La [migration draft.3](CONTRAT_DRAFT3_MIGRATION.md) expose les invariants et limites de cette famille.

## Données et autorités

L’outil collecte, importe, qualifie et prépare. Le récepteur restitue uniquement les instantanés autorisés. Trois fonctions restent distinctes : authentification auprès du récepteur, intégrité du transport événement/reçu et preuve humaine de la révision exacte. Une signature technique ou un booléen d’import ne remplace pas la revue éditoriale.

Les événements conservent environnement, destination, ordre, révision, expiration, empreintes et suspension. Toute modification de preuve ou de contenu invalide les dépendances concernées. Une valeur absente ou invalide ne devient jamais zéro. Les calculs conservent leur couverture, les zéros documentés et les exclusions.

Le destinataire reçoit uniquement la projection publiable : conclusion, réserve, sources, références et métadonnées autorisées. Il ne reçoit ni coffre privé, compte administratif, clé, journal interne ni adresse personnelle de réviseur.

## Intégration d’un site

1. Choisir le contrat exact et les familles de données à recevoir.
2. Implémenter ou sélectionner le récepteur correspondant, avec validation des charges, contrôle d’accès, détection des rejeux et reçus liés aux événements.
3. Tester les versions, erreurs, révocations, suspensions, concurrence et restaurations sur données synthétiques.
4. Vérifier que les fiches et aperçus rendent le même instantané autorisé, avec les preuves et la réserve.
5. Configurer séparément la destination et les autorités de publication. Aucun envoi n’est activé par la seule installation du logiciel.

Le client WordPress conservé est un adaptateur facultatif. Ses hôtes autorisés sont configurables via `DV_WORDPRESS_ALLOWED_HOSTS` et ne sont pas déduits d’un site personnel. Un autre site peut consommer les exports ou implémenter les mêmes contrats.

## Comparateur et restitution

Le processeur `DV-THEMATIC-2` produit des lectures documentaires versionnées avec empreinte, couverture et exclusions. Le récepteur ne doit pas recalculer une conclusion politique, fusionner citation/texte/scrutin ni transformer une absence ou un vote non nominatif en zéro. Les besoins et limites présents dans le dépôt sont décrits dans [COMPARATEUR_PAROLE_VOTES.md](COMPARATEUR_PAROLE_VOTES.md).

Le flux de nouveautés ne porte que sur les contenus confirmés comme publiés. Aucun abonné, courriel réel, service de newsletter ou mandat d’envoi n’est inclus dans les contrats.

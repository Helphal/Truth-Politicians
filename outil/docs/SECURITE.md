# Truth Politicians — sécurité de l’outil

Le logiciel et ses exemples sont publics. Les données de travail, comptes, journaux, clés, archives et accès d’administration restent propres à chaque installation. L’interface de l’outil n’est pas un site éditorial à ouvrir au public.

## Contrôles applicatifs

- Authentification Django sans compte fourni, rôles côté serveur, protection CSRF, sessions courtes et limitation des tentatives de connexion.
- Pseudonymes régénérables, identité interne stable, accès temporaire ou invitation à usage unique, changement de mot de passe et révocation des sessions.
- Hors local/test, capacités administratives conditionnées à une entrée privée attestée par un proxy de confiance (`DV_PRIVATE_ENTRY_*`). Les en-têtes reçus directement d’un client ne constituent pas une preuve d’origine.
- HTTPS requis en modes `private` et `production`. `DV_TRUST_PROXY_SSL_HEADER=1` ne convient qu’à un proxy maîtrisé qui remplace l’en-tête correspondant.
- Réponses privées sans mise en cache, protection contre l’intégration en cadre et politique CSP sans dépendance distante pour l’interface.
- Champs publiables limités, schémas fermés, empreintes canoniques, contrôles des URL et signatures de transport.
- Revue liée à la révision exacte et à ses preuves ; identités de test et comptes machine distincts des autorités de production.
- Coffre privé séparé des statiques et des projections publiables. Une sauvegarde complète reste une donnée privée sensible.

La présence de ces mécanismes dans le code ne remplace pas leur vérification sur une installation réelle.

## Configuration privée

En modes `private` ou `production`, fournir au minimum `DV_SECRET_KEY`, `DV_REVIEW_ATTESTATION_KEY` et `DV_ALLOWED_HOSTS`, puis configurer l’entrée privée, le proxy TLS et les origines CSRF adaptées. Les secrets restent hors Git. Le mode `local` utilise des valeurs de développement et ne doit pas être exposé à distance.

Le paramètre historique `DV_PRIVATE_ADMIN_NAS_QUALIFIED` concerne un état de qualification d’entrée privée ; son nom ne rend aucun matériel particulier obligatoire. Ne pas le positionner pour simuler une qualification inexistante.

Le réseau applicatif est désactivé par défaut. Une collecte directe révèle l’adresse de sortie au fournisseur de la source. Un mode de transport protégé demande une configuration et une qualification effectives, sans repli silencieux. Le fournisseur d’analyse se configure séparément ; aucun service personnel n’est une dépendance implicite.

## Récepteur facultatif

Un site extérieur ne reçoit que les données autorisées. Les hôtes du client WordPress facultatif sont définis dans `DV_WORDPRESS_ALLOWED_HOSTS`, avec une liste vide par défaut. Le choix du site ne supprime ni les contrôles d’envoi, ni les reçus, ni les suspensions, ni la revue éditoriale.

Les droits, journaux, clés, DNS et rendu de ce récepteur doivent être vérifiés indépendamment. Un reçu d’import ne prouve pas la conformité du rendu public.

## Exploitation

Contrôler les permissions des volumes, le stockage des secrets, le chiffrement approprié des sauvegardes, leur restauration, les journaux du proxy et le redémarrage des services. L’application ne chiffre pas elle-même chaque archive de sauvegarde. Les preuves brutes ne doivent jamais être montées dans un serveur de fichiers public.

L’exemple [Docker local](../deploy/docker/README.md) est lié à la boucle locale. Une installation sur serveur demande sa propre configuration et ses propres vérifications. Aucun résultat de test local ne vaut audit juridique, qualification des licences de sources ou garantie d’anonymat.

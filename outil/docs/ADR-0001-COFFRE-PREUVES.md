# ADR-0001 — Coffre privé et preuves publiables séparés

## Décision d’architecture

Truth Politicians conserve les octets originaux dans un coffre privé et prépare séparément les éléments autorisés à diffusion. Le support peut être un volume local ou un stockage privé configuré par l’administrateur. Aucun NAS, hébergeur ou site précis n’est imposé.

1. Les octets sont dédupliqués par SHA-256 sous `runtime/evidence-vault`, avec quota explicite. Le coffre ne chevauche jamais `runtime/static`.
2. Le serveur de fichiers ne voit que les statiques. Aucune route publique ne donne accès au coffre brut.
3. Le sas `runtime/public-evidence-staging` contient des manifestes expurgés : URL publique, attribution, date, extrait borné, type, taille et empreinte. Il n’est pas servi directement.
4. L’export vers un site ou un hébergeur de preuves est facultatif et limité aux dérivés autorisés. Il ne crée aucun accès de retour vers le coffre.
5. Les sauvegardes privées incluent les originaux et le coffre. Une restauration n’accorde aucune autorité de publication supplémentaire.

## Motifs

Une source peut changer ou disparaître. La conservation sert à vérifier ce qui a été effectivement examiné, sans révéler chemins locaux, réseau privé ou métadonnées d’administration. L’archive brute peut être lourde ou soumise à des conditions de réutilisation qui ne permettent pas sa republication complète.

Les captures légères, le texte, les repères et empreintes sont privilégiés. Les vidéos intégrales restent exceptionnelles. Le dépassement de quota doit bloquer une nouvelle capture sans effacer silencieusement les précédentes.

Un service externe d’archivage peut conserver une copie autorisée, mais ne remplace pas la sauvegarde privée et n’est pas activé implicitement par cette architecture.

## Conséquences

Le site de destination restitue les preuves autorisées et leurs références sans pouvoir parcourir les originaux privés. Chaque dérivé conserve sa liaison avec la source et reste soumis aux contrôles, droits de diffusion et autorités de publication. Cette séparation ne promet ni anonymat absolu ni conformité juridique automatique.

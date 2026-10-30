# Contrat candidat Atelier → Site — contre-revue 001

## Statut et périmètre

Contrat candidat de Truth Politicians pour un récepteur facultatif. Le code de réception fourni est un simulateur SQLite réservé aux environnements local/test. Le site choisi par l’utilisateur doit implémenter et vérifier le contrat ; son installation, son hébergement et son interface ne sont pas inclus.

Le format de lots `dv-site-bridge/0.1.0-draft.1` et le transport d’événements décrit ici ont des enveloppes, stockages et autorités différents. Aucun adaptateur silencieux ni migration automatique de récepteur n’est fourni.

La preuve humaine Ed25519 demeure une autorité distincte : HMAC de transport et champ `human_reviewed` ne la remplacent pas. Aucun objet `speech_vote` réel ne devient publiable par la seule présence d’un schéma ou d’un vecteur synthétique. Les autorités et preuves de réception doivent être vérifiées sur l’intégration effectivement choisie.

| Frontière | Historique conservé | Candidat |
|---|---|---|
| Contenu | dv.content/0.4.0-draft.2 | dv.content/0.4.0-draft.3 |
| Politique | DV-PUB-2 | DV-PUB-3 |
| Événement | schéma 0.1.0-draft (sans version signée) | dv.event/0.2.0-draft.1 |
| Reçu de transport | réponse non liée | dv.receipt/0.1.0-draft.1 |
| Canonicalisation | DV-CANON-JSON-V1 | inchangée, sans flottants |

Les versions et charges historiques draft.2 restent conservées ; les annotations de schémas ont été renommées pour Truth Politicians. Le validateur
de lecture historique sait les ouvrir, mais la nouvelle réception refuse leur
utilisation dans un événement courant. Ce n'est pas une migration implicite.

## Champs et sens à préserver

Le contenu réel comporte un catalogue figé : personnes, organisations, affiliations
datées sourcées, mandats sourcés, dossiers législatifs, versions de texte, scrutins
et observations de vote. Les définitions sont accessibles par les références
du schéma racine. Une affiliation au scrutin n'est pas une adhésion à un parti.
Les mandats ou affiliations sans pièce sont exclus explicitement, pas inventés.

Le catalogue est un ensemble fermé sur au plus 25 scrutins et 250 observations :
au-delà, la création refuse le périmètre plutôt que tronquer silencieusement.
Les compteurs de pagination ne décrivent que le corpus matérialisé dans ce périmètre,
pas la couverture institutionnelle depuis 2022. L'offset est 0 ; l'API de pagination
historique globale n'est pas construite par ce candidat.

Les comparaisons discours–votes comprennent actes retenus/écartés, attribution
personne/organisation/capacité et preuves avec citation exacte, emplacement et
version source. Une révision ne remplace pas son instantané par les objets courants.
Toute modification de ces objets invalide la liaison ; il faut produire une
nouvelle révision et la revoir réellement.

Les deux URL officielles restent distinctes : texte/version soumise et scrutin.
Une absence d'observation reste une absence, jamais une abstention.
Les comparaisons factuelles identifient chaque côté par personne ou base explicite
de catégorie ; une catégorie de référence n'est pas un deuxième parlementaire.

Les contrôles de décomptes sont qualifiés pour les scrutins publics AN/Sénat :
pour + contre = exprimés lorsque connus, exprimés ≤ votants, et
exprimés + abstentions ≤ votants. Ne pas généraliser une égalité d'effectif.
La fraction et les exclusions sont recalculées depuis les observations.
Le Site doit vérifier aussi les invariants sémantiques décrits dans les tests :
digest public interne, références fermées, source_version présente dans l'enveloppe,
cohérence du catalogue avec le contenu, positions et bases du calcul.

## Signature, destination et reçus

L'événement exige event_schema_version, environment, destination, key_id et expires_at.
Ces champs sont signés avec tous les autres champs, sauf signature_algorithm et
signature. environment vaut test/private/production et destination doit être
exactement dv-site suivi de deux-points et du même environnement.
Un paquet test, même constitué de données réelles, ne devient pas une autorisation
de production. Les clés de test sont exclusivement des fixtures locales.

DV-CANON-JSON-V1 : JSON UTF-8, clés triées par ordre Unicode, sans espaces superflus,
sans flottants, NaN ou normalisation implicite des chaînes. Ce n'est pas RFC 8785.
payload_digest est SHA-256 du payload ; authorization.snapshot_digest est SHA-256
de public. La signature est HMAC-SHA256 de l'événement non signé canonique.
Le reçu reprend event_id, sequence, record_id, environment, destination et
SHA-256 de l'événement complet (signature comprise), avec son propre key_id.
Sa signature exclut également les deux champs de signature.

Le registre privé des clés associe chaque identifiant à environnement, destination,
usage event OU receipt, activité et secret. En réel les usages doivent être séparés.
La clé signant un ancien message reste explicitement active pendant sa fenêtre de
rejeu, puis est révoquée. Le code ne lit ni fichier de secret ni coffre pour
configurer ce registre : branchement à la configuration privée encore à réaliser
sur la cible autorisée. Ne pas insérer les valeurs réelles dans ce dossier.

Un reçu applied ou duplicate prouve uniquement la réception liée à cet événement.
Il ne prouve ni visibilité publique, ni autorisation personnelle, ni conformité
du rendu public. Le récepteur doit établir séparément ces preuves.

## Autorité courante, ordre et restauration

La file est réévaluée avant export ; l'envoi relit encore la base courante sous
transaction. Un fichier signé archivé n'est pas une autorité autonome.
Une revue révoquée, un refus/complément, une identité désactivée, une correction
source ou une version périmée neutralisent l'upsert. S'il a déjà été émis, une
suspension persistante est créée, prioritaire même dans un lot borné à un élément.
Le récepteur conserve le digest de chaque event_id et refuse son changement.
L'ordre par dossier et le verrou de suspension empêchent une reprise implicite.

L'upsert expire 300 secondes après sa première émission, pas après la mise en file.
Un export ultérieur peut créer un NOUVEL événement de même contenu et séquence
supérieure après recontrôle complet. L'ancien fichier n'est jamais prolongé.
L'expiration du transport ne retire pas à elle seule un contenu déjà reçu.
Les suspensions restent applicables après leur horodatage d'expiration.
Une nouvelle réception après suspension requiert une reprise explicitement autorisée.

Une restauration produit .dv-restored-unreconciled. L'envoi est bloqué tant que
la restauration n'a pas été réconciliée avec les séquences et suspensions du Site.
Ne pas effacer le marqueur ou activer DV_RECEIVER_STATE_RECONCILED pour contourner
ce contrôle. La procédure distante de réconciliation reste à réaliser et prouver.

## Plan de raccordement du récepteur choisi

1. Examiner les schémas, les exemples synthétiques et les vecteurs fournis.
2. Porter les validations sémantiques, le registre d'événements et les reçus côté
   récepteur, sous véritable contrôle d’accès ; exécuter les rejets avant les cas heureux.
3. Installer les migrations 0005–0007 uniquement sur une cible privée autorisée,
   après sauvegarde vérifiée ; les anciennes lignes sans digest ne valent pas état réconcilié.
4. Vérifier signatures inter-langages, rejouabilité, séquences, suspension, rotation,
   révocation entre export et réception, indisponibilité et restauration ancienne.
5. Comparer le rendu substantiel du site au même instantané que la revue.
6. Consigner l'acceptation du contrat séparément de l'autorisation de publication.

Les paramètres DV_PUBLICATION_SEND_AUTHORIZED et DV_RECEIVER_STATE_RECONCILED restent
faux par défaut. Le client WordPress facultatif applique les contrôles réseau avant
résolution DNS. Le transport direct est limité aux hôtes explicitement configurés
dans `DV_WORDPRESS_ALLOWED_HOSTS`, avec TLS et adresses épinglées, sans suivre une redirection. Le contrôle de révocation
pendant le trajet distant reste borné par l'expiration et la suspension : il n'existe
pas ici de preuve de validation en ligne avec un récepteur déployé.

## Tests et limites de la livraison

Dans le dépôt complet, exécuter manage.py test atelier.tests review_regressions puis
python -m unittest discover -s tests. Les suites exigeant des corpus ou bancs externes ont des prérequis distincts,
énoncés dans `../scripts/README.md`. Les tests locaux ne démontrent pas une concurrence distribuée.
Le paquet contractuel minimal ne comprend ni base, ni corpus privé, ni rapport
de mission, ni environnement Python, ni historique Git.

prepare_inquiry est une recherche lexicale bornée dans le corpus local, pas un
atelier documentaire exhaustif ni un modèle de raisonnement politique.
La collecte depuis 2022, les mandats historiques et la qualification documentaire
complète restent partiels. Aucune revue du responsable éditorial n'a été créée par la recette.

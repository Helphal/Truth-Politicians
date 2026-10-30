# Truth Politicians — contrat des segments, sous-lots et reprises

Version transport : `dv.segment-plan/1.0.0`. Agrégation : `dv.partition-aggregation/1.0.0`. Exécuteur : `dv.plan-executor/1.0.0`. La politique documentaire demeure **`dv.verification/1.1.0`**. Le résultat final est traduit vers son contrat reçu, puis passe réellement par `verification.finish`, ses contrôles de fraîcheur, actions, métadonnées, sources et diffusion. Aucun changement du contrat signé Atelier → Site.

## Pièces et identité

Le plan exact lie job/key, révision, propriétaire vérificateur, politique, modèle/effort, instantané complet, extraction de toutes les assertions scalaires, registre et mesures. Son SHA est calculé sur le corps sans son propre champ d'empreinte. Une préparation ne démarre aucun job, n'alloue aucun budget et ne produit aucun reçu.

Chaque segment relie source/version/SHA, passage/SHA du texte UTF-8, bornes **en points de code Unicode**, texte contigu et SHA du fragment. Python utilise donc `text[start:end]`, sans normalisation Unicode ni conversion implicite des offsets JavaScript UTF-16. Le registre est reconstruit depuis l'instantané pour vérifier toute traduction. Les limites de capture ou de schéma provoquent un refus explicite ; accents, négations, nombres, signes et contexte ne sont pas supprimés pour faire rentrer un paquet.

La première lecture reçoit les sources et leurs passages complets, sans assertions ni réponse éditoriale. Les sous-lots claims reçoivent l'objet exact de chaque assertion, les identités/dates/conditions scalaires de ses ancêtres, le contexte global (réponse et réserve incluses), toutes les sources et contre-éléments. Les assertions stables ne changent pas lorsqu'on modifie seulement la taille du lot.

## Traduction et contrôles

Le modèle sélectionne des `segment_id` réellement fournis dans `support` et `against`, avec une explication du rapport à l'assertion. Le serveur traduit uniquement cette sélection vers `{passage_id, quote}`. Il ne cherche pas une citation proche, ne change aucun jugement, passage choisi, action ou limite. Le brut exact, sa relecture SHA, le registre, le mapping des références/explications et le canonique restent distincts. Un runner SYNTH retournant un objet Python est explicitement marqué comme sérialisation d'adaptateur, jamais comme réponse fournisseur réelle.

Une citation exacte n'est pas une preuve automatique de sens. Le témoin SYNTH d'une citation authentique mais sans rapport garde son jugement négatif. La capacité du futur vérificateur à apprécier le sens devra être exercée sur un vrai sous-lot autorisé ; les tests de chaînes n'en fournissent pas la qualification.

La politique reçue refuse toujours `ETAYE_DANS_PERIMETRE` sans support, même pour une limitation. Les metadata proviennent exclusivement du prédicat reçu et inchangé, jamais d'une nouvelle liste de noms, dates ou phrases gênantes. `corriger/rechercher/suspendre` restent ouverts. Pour `attribuer/nuancer`, une résolution déjà présente cite la valeur exacte de l'assertion, à son propre chemin ; une citation institutionnelle ne peut pas la remplacer.

Les doublons d'assertion ne se replient que si leur noyau canonique est strictement égal. Tous les motifs, limites, références et occurrences sont conservés. Un noyau contradictoire, booléen, ID étranger, champ manquant, citation invalide ou membre JSON dupliqué refuse le résultat.

## Partition et capacité

Les assertions sémantiques sont réparties de manière disjointe et couvrante. Les métadonnées strictes restent comptées et explicitement reprises au résultat final. Un passage entier est indivisible pour la lecture ; les fragments ne servent qu'à sélectionner des citations contiguës.

Les valeurs locales par défaut bornent les paquets à 4 Mio d'entrée, 128 Kio de capture sortie, 32 assertions maximum et 2 Kio d'estimation de sortie par assertion. Le découpage est réduit si les mesures effectives dépassent ces bornes. Ce sont des bornes locales et des estimations conservatrices, pas une nouvelle qualification du fournisseur. Les mesures réelles avec les premières lectures sont contrôlées à nouveau avant lancement.

Les schémas utilisent `$defs` afin de conserver un seul enum de segments malgré les variantes support/against/action. Le garde applique les limites documentées : 1 000 valeurs enum au total ; pour un enum de plus de 250 chaînes, 15 000 caractères ; 120 000 caractères des identifiants/enum/const au total ; 5 000 propriétés ; dix niveaux d'imbrication, références développées pour ce dernier calcul. Les tests portent sur chaque borne admissible et son dépassement. [Documentation officielle](https://developers.openai.com/api/docs/guides/structured-outputs), consultée par l'intégrateur le 3 octobre 2026.

L'agrégateur conserve `prepared`, `reserved`, `launched`, `returned`, `accepted`, `partial`, `refused`, `interrupted` et `ambiguous`. Il ne reçoit aucun résultat final si un sous-lot manque ou reste non accepté. Chaque résultat accepté conserve le contrat, l'empreinte, les chemins, les valeurs et leurs types exacts ; un ancien snapshot aux mêmes assertions ne peut pas être rebaptisé courant. Une exécution complète peut être documentairement négative et rester inéligible à la diffusion.

## Allocation et journal

Aucun budget par défaut. L'allocation JSON doit contenir exactement :

```json
{
  "contract": "dv.verification-allocation/1",
  "allocation_id": "IDENTITE-EXPLICITE-A-FOURNIR",
  "authorization": "explicit_dv_verite",
  "authority_reference": "REFERENCE-REELLE-A-FOURNIR",
  "owner_id": 1,
  "plan_digests": ["SHA-DU-PLAN-A-FOURNIR"],
  "fingerprints": ["SHA-INSTANTANE-A-FOURNIR"],
  "job_ids": [42],
  "model": "gpt-6-sol",
  "effort": "high",
  "max_phases": 0,
  "expires_at": "DATE-EXPLICITE-AVEC-FUSEAU-A-FOURNIR"
}
```

Cet exemple **est inexécutable**, sans autorisation réelle et avec zéro phase. Il n'a pas été déposé dans le runtime comme allocation. Le fichier n'est pas une preuve de consentement humain en lui-même : sa production appartient à une opération explicitement autorisée par l'autorité du projet. Le rôle authentifié, la propriété, le périmètre, le plafond non consommé et l'expiration sont vérifiés avant toute construction du client CLI ou préflight.

La racine privée est `settings.DV_VERIFICATION_PLAN_ROOT`, sinon `RUNTIME_DIR/verification-plans`. Chemins résolus, traversées `..`, symlinks et jonctions sont contrôlés. Le plan et l'allocation ont deux verrous OS exclusifs acquis dans un ordre constant ; changer d'allocation ne permet pas de lancer deux fois le même plan. Un job déjà détenu par une autre tentative active est préservé avant tout client.

Le journal cumulatif par `allocation_id` conserve les réservations, lancements, retours et reprises. Un timeout ou une réservation interrompue conserve sa consommation. Le plafond et le périmètre d'une identité d'allocation déjà enregistrée sont immuables. Les retries natifs CLI ne sont pas présentés comme gratuits ni confondus avec ces phases applicatives ; les traces CLI reçues restent assainies par le superviseur existant.

Une reprise peut réutiliser une sortie retournée seulement si sa propre attribution durable, le hash de l'entrée, le SHA du brut, le plan et les mappings sont présents et intacts. Une coupure après reçu de retour ou après commit du reçu métier est récupérée sans génération double. Un lancement sans reçu de retour attribué est **ambigu** et refuse toute régénération automatique, même si une sortie JSON isolée existe. Un sous-lot techniquement refusé requiert une nouvelle opération explicite `--retry-batch`, comptée dans l'allocation ; les résultats déjà acceptés ne sont pas régénérés.

L'expiration de bail reçue reste contrôlée avant et pendant les phases. Une opération dépassant ce bail s'arrête ; les résultats attribués déjà reçus restent repris par une commande explicite sous allocation encore applicable. Aucun bail n'est prolongé silencieusement. Le journal fichiers n'est pas une preuve de résistance à un administrateur modifiant son propre volume ou à une perte matérielle du stockage.

Une lecture de sources peut être réutilisée par un autre dossier seulement si ordre des sources, identités, révisions, textes, contexte, contrat et modèle/effort sont identiques, avec un reçu source actuel, relu et attribué. Une lecture source acceptée reste utilisable lorsque les claims de son dossier ont été refusés. La provenance annonce cette réutilisation, sans prétendre à trois lectures indépendantes.

## Annexe documentaire exacte C1

L'entrée facultative est un JSON strict `{contract: "dv.documentary-input/1", fingerprint, records}`. Chaque record contient exactement `family`, `purpose`, `locations` et `proof`. Les locations désignent uniquement des scalaires actuels de l'extracteur reçu. Le type de preuve est l'un de :

- `snapshot_pointer` : `pointer`, `expected`, vérifiés exactement dans le contenu de cet instantané ; contexte déclaré, sans preuve indépendante du fait.
- `source_passage` : `passage_id`, `quote`, `source_version`, vérifiés dans le passage complet épinglé.
- `pinned_original_segment` : `source_version`, `original_sha256`, `start`, `end`, `exact_segment`, `offset_unit="unicode_codepoints"`. Les octets complets explicitement fournis doivent conserver le SHA de la source et le fragment exact doit correspondre aux bornes Unicode.

La préparation pure construit des `record_id`, contrôles typés, reçus de fragments et un `annex_digest`, sans qualification C3 ni jugement de fournisseur. Elle limite l'entrée brute à 256 Kio, l'annexe gelée à 512 Kio et le nombre de records à 256. Un dépassement refuse explicitement l'annexe, sans supprimer de preuve pour faire rentrer le paquet.

L'annexe lie son propre fingerprint courant et fait partie du digest et des mesures du plan. Elle est présente dans chaque paquet claims et dans son prompt effectif ; elle reste hors de la première lecture des sources. Les mentions de record_id dans `reason` sont conservées et tracées, sans changer le jugement ou fabriquer un support. `support` et `against` continuent à accepter exclusivement les segments du registre des passages. Un href exact dans un original ne crée donc pas à lui seul un passage ou une citation v1.1.0 nouvelle.

Le chargeur applicatif reçoit un fichier explicitement nommé sous `settings.DV_VERIFICATION_DOCUMENTARY_ROOT`, sinon `RUNTIME_DIR/verification-plans/documentary-inputs`. Il sélectionne les octets des originaux par les tuples source/version/SHA déjà présents dans le snapshot et les `DocumentVersion` reçues, avec confinement, refus de liens et lecture bornée sous `DV_EVIDENCE_VAULT_PATH`. Un champ documentaire ne choisit jamais un chemin de fichier. Les octets originaux sont recontrôlés lors du contrôle courant et de la reprise, avant tout appel.

Les `bound_artifact` ne sont pas envoyés au modèle. L'état administratif réel, les droits et les preuves de code restent au contrôle déterministe ou humain approprié. Leurs valeurs déjà déclarées dans le snapshot et leurs assertions ne gagnent aucune exemption de metadata, d'action ou de support. Les douze annexes livrées documentent leurs exclusions et références humaines dans `objections/annexes/RECU-ANNEXES-DOCUMENTAIRES.json`. Aucun ancien verdict ou avis personnel n'est transmis comme preuve fraîche d'un candidat différent.

## Commandes applicatives

Depuis `code-outil/outil`, la préparation uniquement :

```powershell
python -B manage.py prepare_verification_plan --job 42 --user-id 1
```

La sortie annonce le chemin gelé, son SHA et les phases prévues. Les six plans calculés depuis les captures historiques ne remplacent pas ce contrôle du job actuel.

Pour inclure une annexe brute actuelle explicitement déposée sous la racine documentaire privée :

```powershell
python -B manage.py prepare_verification_plan --job 42 --user-id 1 --documentary-input CHEMIN_PRIVE_DOCUMENTARY_INPUT_JSON
```

La pièce doit déjà être liée à l'empreinte du job courant ; une annexe historique ou de candidat non adopté ne franchit pas ce contrôle. Cette préparation reste sans budget, appel fournisseur ou adoption de révision.

Après une allocation nouvelle, explicite, applicable et non épuisée uniquement :

```powershell
python -B manage.py execute_verification_plan --plan CHEMIN_PLAN_JSON --user-id 1 --execution-id ID_OPERATION --allocation CHEMIN_ALLOCATION_JSON --codex-executable CHEMIN_CLI_QUALIFIE
```

Ces commandes d'exécution n'ont pas été lancées sur les dossiers réels. Les douze phases historiques restent consommées. Aucun nouvel email, nouvelle inférence, migration, service, modèle, compte, revue personnelle ou publication n'a été créé par ce lot.

## Limites de réception

Le code intégré et ses tests SYNTH établissent le protocole local, les refus, les transitions de l'application, la conservation et les reprises. Le CLI supervisé existant est réutilisé par deux hooks réellement testés sans processus fournisseur. Le nouveau protocole n'a pas encore été exercé sur ce fournisseur. La couverture documentaire favorable, la correction des 49 objections, l'approbation personnelle, le droit de diffusion, le pont actif et la réception externe ne résultent pas des tests de ce composant.

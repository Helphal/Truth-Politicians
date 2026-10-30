# Comparateur parole publique ↔ mesures ↔ votes

**Version :** DV-THEMATIC-2  
**Périmètre :** moteur et interface interne de Truth Politicians ; raccordement public facultatif.

Les états de tests et de déploiement doivent être établis pour la version effectivement installée.

## Besoin produit

Le contrôle utile ne consiste pas à rapprocher littéralement « je voterai ce
texte » du scrutin correspondant. Il doit pouvoir partir d'une position
publique durable — par exemple défendre le pouvoir d'achat — puis montrer les
mesures concrètes qui relèvent réellement de cette position et les votes
nominatifs qui les ont fait avancer ou bloquer. Le parcours inverse doit partir
d'une mesure et retrouver les paroles publiques et votes comparables.

La parole peut provenir de la presse, de la télévision, de la radio, d'une
vidéo, d'un discours, d'une publication institutionnelle ou d'un réseau social.
Le texte et le scrutin restent reliés à deux URL officielles distinctes.

## Principes issus de la recherche

- Full Fact décrit un contrôle qui commence par préciser exactement
  l'affirmation, rassemble des sources variées, privilégie les sources
  primaires et rend les preuves accessibles au lecteur. L'interface sépare donc
  citation, mesure, scrutin et lecture permise au lieu d'afficher une sentence
  isolée : [méthode de Full Fact](https://fullfact.org/about/how-we-fact-check/).
- Les engagements IFCN demandent de présenter les éléments qui soutiennent et
  ceux qui fragilisent une affirmation, d'appliquer les mêmes standards quel
  que soit l'acteur et de publier une méthodologie transparente. Les relations
  sont donc versionnées, sourcées et qualifiées indépendamment de la personne :
  [Code de principes IFCN](https://ifcncodeofprinciples.poynter.org/).
- « Save Page Now » produit une copie ponctuelle d'une page, sans garantir un
  crawl futur ou complet du site. Wayback est donc une preuve externe possible,
  pas le coffre ni la sauvegarde du produit : [aide officielle Internet
  Archive](https://archivesupport.zendesk.com/hc/en-us/articles/360004651732-Using-The-Wayback-Machine).

## Architecture

```text
Déclaration publique
  │  relation qualifiée + passage exact
  ▼
Mesure concrète
  │  relation qualifiée + objet procédural
  ▼
Scrutin officiel
  │  observation nominative exacte
  ▼
Convergence / tension / non comparable / non concluant
```

Deux lectures utilisent la même chaîne :

1. **Parole → actes** : choisir une déclaration, puis parcourir ses mesures et
   scrutins qualifiés.
2. **Loi → paroles** : choisir une mesure, puis retrouver les déclarations et
   votes nominatifs rattachés.

La recherche lexicale retrouve uniquement des candidats. Elle ne crée jamais
un lien de comparaison. Le calcul ne consulte ni réseau, ni modèle externe.

## Modèle de données

| Objet | Rôle | Invariant |
|---|---|---|
| `Declaration` | Citation, acteur, date et nature de la source publique | Personne et organisation ne sont jamais fusionnées |
| `Measure` | Un effet précis d'une version de texte | Résumé simple et effet concret distincts du titre juridique |
| `DeclarationMeasureLink` | Sens documenté de la parole envers la mesure | Révisions immuables ; passage et justification obligatoires |
| `MeasureScrutinyLink` | Effet d'un vote « pour » sur la mesure | Révisions immuables ; ne présume pas qu'un vote pour adopte toujours la mesure |
| `VoteObservation` | Catégorie nominative issue d'une version de source | Une absence de donnée ne devient jamais abstention ou opposition |

La révision la plus récente de chaque relation fait foi. Si elle est candidate
ou écartée, une ancienne relation qualifiée n'est pas réutilisée silencieusement.
Un conflit entre versions de vote est affiché comme non concluant.

## Règles de lecture

- `pour` et `contre` ne prennent un sens que relativement à l'effet procédural
  qualifié du scrutin (`advances` ou `blocks`).
- `abstention`, `non_voting`, `not_recorded`, `unknown` et l'absence
  d'observation sont non directionnels.
- Une déclaration collective n'autorise aucun vote personnel.
- Une parole postérieure peut être en convergence ou en tension avec un vote
  antérieur, mais n'est jamais présentée comme une promesse rompue.
- Une position conditionnelle ou un effet de scrutin composite restent non
  comparables.
- Aucun résultat de ce service ne crée de `HumanReview`, d'événement d'envoi ou
  de publication.

## Interface privée

- `/parole-et-votes/` : recherche dans les deux sens et restitution des chaînes
  qualifiées.
- `/parole-et-votes/qualifier/` : saisie des deux relations par les seuls rôles
  reviewer/opérateur. Chaque saisie est attribuée au compte et crée une nouvelle
  révision immuable. Elle ne vaut pas revue éditoriale du responsable éditorial.

Les quatre étapes sont lisibles dans l'ordre, avec les liens distincts vers la
source de la déclaration, la version officielle du texte et le scrutin officiel.
L'écran ajoute une lecture courte, des filtres de couverture, une pagination
explicite, la séparation orateur / entité représentée / acteur comparé, et un
panneau « preuves distinctes » lié aux empreintes.

## Coffre et archivage

Le coffre privé défini par `ADR-0001-COFFRE-PREUVES.md` reste séparé du
comparateur. La politique de volume demeure : métadonnées, texte, empreinte et
capture légère par défaut ; WARC/WACZ pour les pages importantes ou fragiles ;
vidéo intégrale seulement à titre exceptionnel. Aucun chemin privé n’entre dans
une donnée destinée à un site. Un envoi vers un service d’archivage ou d’hébergement
extérieur nécessite une autorisation et une configuration distinctes.

## Fiabilité et limites

- limites de résultat et de scan explicites ;
- aucune exhaustivité officielle revendiquée ;
- sources interdites ou mises en quarantaine exclues du calcul ;
- historique conservé par révisions immuables ;
- migrations et données existantes à vérifier avant une mise à niveau ;
- intégration et rendu du site choisi à qualifier séparément.

## Choix et compromis

Le produit préfère une chaîne vide à une relation lexicale trompeuse. Cela
retarde l'affichage tant que les deux maillons ne sont pas qualifiés, mais rend
chaque conclusion reproductible. Le moteur est déterministe et moins souple
qu'un modèle génératif ; son calcul local n’appelle pas de modèle distant, mais l’hébergement et la collecte
ont leurs propres ressources. Il ne décide pas au nom du réviseur.

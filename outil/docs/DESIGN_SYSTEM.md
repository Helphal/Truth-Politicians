# Système visuel de l’Atelier

## Intention

L’Atelier est l’interface de travail de Truth Politicians, un logiciel de vérification documentaire.
L’interface doit être nette, moderne et rapide à parcourir : identité produit
compacte, surfaces légères, actions visibles et métadonnées secondaires. Elle
rend les preuves faciles à atteindre et distingue les faits, les limites
techniques et les décisions humaines.

## Principes

1. La conclusion lisible vient avant les métadonnées techniques.
2. Une source officielle est nommée par son rôle : texte, scrutin ou preuve.
3. Les états ne reposent jamais sur la couleur seule ; chaque pastille garde un
   libellé explicite.
4. L’indigo structure l’interface, l’orange signale une limite, le rouge un
   blocage et le vert un contrôle réussi. Aucune couleur n’encode un parti.
5. Les détails exhaustifs restent accessibles par divulgation progressive.

## Tokens

| Rôle | Valeur | Usage |
|---|---|---|
| Encre | `#172033` | texte principal |
| Ardoise | `#111827` | en-tête produit et navigation |
| Accent | `#5b5bd6` | actions, repères et focus fonctionnel |
| Toile | `#f5f7fb` | fond général discret |
| Orange | `#b45309` | réserve ou attention |
| Vert | `#047857` | réussite explicitement libellée |
| Rouge | `#b42318` | refus ou blocage explicitement libellé |
| Contenu | `78rem` | largeur maximale de travail |
| Rayon | `.9rem` | surfaces principales |

Titres et corps utilisent une pile sans-serif système inspirée des interfaces
produit contemporaines. Le monogramme et certaines actions ont un léger
dégradé indigo/bleu, sans image décorative. Aucune police, image ou bibliothèque
tierce n’est chargée.

## Composants

### En-tête et navigation

La marque « Truth Politicians » et le sous-titre « Atelier de vérification » empêchent
la confusion avec un site officiel. L’onglet courant est marqué par
`aria-current="page"` et une capsule indigo sobre.

### Pastilles d’état

Variantes neutre, réel/attention, réussi et bloqué. Une pastille ne doit pas
porter seule une information complexe : la raison détaillée reste adjacente.

### Cartes métriques

Un nombre, un libellé et une phrase d’interprétation courte. Elles servent à
orienter, jamais à fabriquer un score politique.

### Carte d’affirmation

Sur la page de revue, `hero-card` présente d’abord ce que la version affirme,
la réserve principale et les deux liens officiels. La provenance et les
contrôles suivent dans l’ordre de lecture.

### Parcours « parole ↔ votes »

Le composant `evidence-flow` rend la chaîne Parole → Mesure → Scrutin → Position
visible avant tout résultat. `direction-switch` propose les deux sens de
recherche sans onglet JavaScript ; ce sont des liens GET accessibles au clavier.
La `comparison-card` conserve quatre blocs nommés, les trois liens de preuve et
une conclusion textuelle. Vert signifie « convergence documentée », orange
« tension documentée » et indigo « non comparable/non concluant » ; le libellé
reste toujours présent et la couleur n'encode aucun acteur politique.

### Qualification documentaire

Deux panneaux séparés matérialisent les deux décisions humaines. Chaque champ a
un libellé visible, les erreurs utilisent `role="alert"`, et le bouton nomme la
révision créée. Le bandeau rappelle qu'une qualification documentaire n'est ni
une revue éditoriale, ni une publication.

### Formulaires

Libellés au-dessus, cible minimale de 43 px, focus indigo de 3 px et action
principale verbale. Les actions de décision restent séparées des corrections.

## Responsive et accessibilité

- À 960 px, les métriques passent de quatre à deux colonnes.
- À 700 px, cartes et colonnes passent à une colonne ; la navigation reste
  horizontale et défilable.
- Le flux en quatre étapes passe à deux colonnes à 960 px puis une colonne à
  700 px ; les flèches décoratives disparaissent lorsqu'elles ne décrivent plus
  l'ordre visuel.
- Contraste visé : au moins 4,5:1 pour le texte courant.
- Les liens restent soulignés et ne sont donc jamais identifiés par la couleur
  seule.
- Le lien d’évitement, les focus visibles et `prefers-reduced-motion` sont
  conservés.

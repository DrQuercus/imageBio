# ADR 006 : traitements du mode Cryo côté serveur, dans un ordre fixe

**État :** acceptée ; points 2 et 3 amendés par l'[ADR 013](013-filtrer-la-vue-reduite.md) (une vue réduite filtrée est réduite dans Fourier, puis filtrée).

## Contexte

Le mode Cryo ajoute des opérations qui changent la valeur des pixels : moyenne de frames, référence de gain, filtres en résolution. L'ancien CryoViewer les refaisait à chaque réglage d'affichage, sans cache, et son filtre mélangeait les bords opposés de l'image (`docs/audit/AUDIT.md`, CV-18). Il fallait décider où ces opérations se font, dans quel ordre, et comment elles s'articulent avec les niveaux de détail de la visionneuse (vue binnée, puis région visible).

## Décision

1. **Les traitements sont des paramètres de la requête de plan**, pas un état du serveur : `t_end`, `gain`, `gain_orient`, `gain_inv`, `lp`, `hp`, `apix`. L'API reste sans état (ADR 003) et un lien reproduit l'image traitée.
2. **Ils s'appliquent au plan en pleine résolution, dans un ordre fixe** : moyenne de frames, gain et pixels défectueux, filtres, puis découpe et binning. Le client ne choisit pas l'ordre.
3. **Le résultat en pleine résolution est mis en cache** sous une clé qui contient tous les paramètres et l'identité (chemin, taille, date) du fichier et de la référence de gain. La vue binnée et la région de détail d'un même plan traité partagent ce résultat : le filtre n'est calculé qu'une fois.
4. **Le contraste, le gamma et la palette restent sur la carte graphique** (ADR 002). Seul ce qui change les valeurs passe par le serveur.
5. **La référence de gain est désignée par un chemin logique** et autorisée comme n'importe quel fichier, à chaque requête.
6. **L'orientation du gain n'est jamais devinée en silence.** Le serveur la vérifie à la demande et rend ses mesures ; l'interface montre le résultat et l'utilisateur applique la correction.
7. **Le spectre de puissance est calculé sur l'image non filtrée** (après moyenne et gain) : filtré, il ne montrerait que l'enveloppe du filtre.
8. **Le binning peut se faire dans Fourier** (`fbin=true`, ajouté à la fin de M4) : le plan entier est réduit une fois en gardant les basses fréquences de sa transformée, et les régions sont découpées dans ce résultat, pour que les tuiles de détail coïncident avec la vue d'ensemble. Une région doit donc commencer sur un multiple du bin. Le paramètre fait partie de la clé du traitement ; le plan en pleine résolution, lui, n'est gardé qu'une fois en cache, quel que soit le binning demandé. L'image est prise comme périodique : pas de prolongement en miroir ici, contrairement aux filtres.

## Conséquences

- Filtrer une image 4K coûte environ 0,7 s la première fois, puis rien tant que le résultat reste dans le cache ; zoomer et se déplacer dans une image filtrée ne relance pas le filtre.
- Un filtre appliqué à une région seule donnerait un autre résultat près de ses bords : c'est exclu par construction, toute région est découpée dans le plan filtré en entier.
- Un plan traité occupe la place d'un plan `float32` en pleine résolution dans le cache (64 Mio en 4K). Le cache est borné en octets ; faire défiler un film filtré en remplace les entrées au fur et à mesure.
- Tout nouveau traitement doit entrer dans la clé (`Processing.key()`), sans quoi deux requêtes différentes partageraient une entrée de cache.

## Alternatives écartées

- **Filtrer dans le navigateur** (shader ou WebAssembly) : il faudrait lui envoyer le plan entier en pleine résolution, ce que la visionneuse évite, et maintenir un second moteur de calcul à comparer au premier.
- **Filtrer la vue binnée** : rapide, mais le filtre agirait sur des pixels moyennés et la coupure en Å ne serait plus celle demandée dès que le niveau de détail change.
- **Appliquer automatiquement la meilleure orientation du gain** : une erreur de détection (dose trop faible, gain sans motif) corrigerait les images de travers sans que personne ne le voie.

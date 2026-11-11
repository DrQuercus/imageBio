# ADR 008 : pointés et galeries, lus par le serveur, jamais retournés d'office

**État :** acceptée.

## Contexte

La fin du mode Cryo demandait de superposer à une micrographie les particules pointées par RELION, crYOLO, EMAN ou cryoSPARC, et de parcourir d'un coup d'œil les piles d'images (particules extraites, moyennes de classes 2D). Ces programmes n'écrivent pas leurs coordonnées de la même façon : centres ou coins de boîtes, pixels ou fractions de l'image, lignes comptées depuis le haut ou depuis le bas, un fichier par micrographie ou un seul pour tout un jeu de données, de plusieurs millions de lignes. Aucun vrai fichier n'était disponible pour trancher les conventions.

## Décision

1. **Le serveur lit les fichiers de pointés et rend des centres en pixels de l'image affichée** (`cryo/picks.py`, `/api/cryo/picks`). Le client ne connaît qu'une forme. Le fichier de pointés est un chemin logique, autorisé comme n'importe quel fichier.
2. **Une coordonnée (x, y) est la colonne x et la ligne y de l'image telle qu'elle est enregistrée**, la ligne 0 étant la première du fichier, que la visionneuse dessine en haut. **Rien n'est retourné d'office** : l'interface a un bouton « Retourner Y », et son état est dans le lien. C'est la règle déjà suivie pour l'orientation du gain (ADR 006).
3. **Un fichier STAR est lu ligne à ligne**, jamais chargé en entier, et seules les colonnes demandées sont gardées (`cryo/star.py`). Pour un fichier de jeu entier, les particules sont celles dont la micrographie porte le nom de l'image, une fois retiré ce que les programmes ajoutent aux noms (`micrograph_key`).
4. **Un fichier `.cs` de cryoSPARC est un tableau NumPy, jamais dépicklé** (`allow_pickle=False`).
5. **La réponse est bornée** (50 000 points par image) et dit ce qu'elle a laissé de côté : points coupés, points hors de l'image, aucune micrographie du fichier ne correspondant à l'image.
6. **La galerie d'une pile est faite de vignettes par plan** (`/api/datasets/thumb`), binnées par moyenne et étirées chacune sur ses propres valeurs ; seules les tuiles visibles existent dans la page. Elle sert aux particules, aux sections d'un volume et aux moyennes de classes.
7. **Les classes 2D sont étiquetées d'après le fichier `_model.star` voisin** de `*_classes.mrcs` (`/api/cryo/classes`) : part des particules et résolution. Sans ce fichier, la galerie montre les plans sans étiquette.

## Conséquences

- Un fichier de pointés écrit avec les lignes comptées depuis le bas s'affiche à l'envers tant que l'utilisateur n'a pas coché « Retourner Y » : c'est visible tout de suite, et cela vaut mieux qu'une correction devinée qui serait fausse une fois sur deux.
- Un `particles.star` est relu en entier à chaque image (environ une seconde par million de lignes). Un index par micrographie pourra être mis en cache si cela gêne.
- Les noms de micrographies sont rapprochés par une liste courte de suffixes connus ; un nom que la liste ne couvre pas donne « aucune micrographie du fichier n'est cette image », pas des particules au mauvais endroit.
- Un fichier de pointés propre à une image ne suit pas à l'image suivante ; un fichier de jeu entier suit.
- Les conventions restent à confirmer sur des fichiers écrits par les vrais programmes.

## Alternatives écartées

- **Lire les fichiers de pointés dans le navigateur** : un `particles.star` de plusieurs centaines de Mo n'a rien à y faire, et les droits d'accès se vérifient côté serveur.
- **Deviner le sens de Y** d'après le programme qui a écrit le fichier : la même extension est écrite avec les deux conventions selon la façon dont les images ont été importées.
- **Dépendre de `starfile` (pandas)** : charge le fichier entier en mémoire ; il ne faut ici que deux à quatre colonnes d'une table.
- **Une mosaïque calculée par le serveur pour la galerie** : une seule requête, mais une image à refaire à chaque changement de taille ou de tri, et pas de défilement virtuel.

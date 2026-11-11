# imageBio : l'interface web

La visionneuse : React, TypeScript et un canevas WebGL2. Elle parle à l'API du backend (voir [`../backend/README.md`](../backend/README.md)).

## Utilisation

Une fois construite, l'interface est servie par le backend lui-même :

```bash
npm ci
```

```bash
npm run build
```

```bash
imagebio serve --local /chemin/vers/mes/donnees
```

Puis ouvrir `http://127.0.0.1:8000`. La construction écrit dans `backend/src/imagebio/static/`, qui n'est pas versionné.

Pour développer, avec rechargement à chaud (le serveur de Vite relaie `/api` vers le backend sur le port 8000) :

```bash
npm run dev
```

## Ce que fait la visionneuse

- **Affichage sur la carte graphique.** Le serveur envoie les valeurs brutes des pixels. La fenêtre de contraste, le gamma, l'inversion et la palette sont calculés dans le shader : les régler ne déclenche aucune requête.
- **Deux niveaux de détail.** Une vue d'ensemble binnée de tout le plan, puis la région visible à la résolution que l'écran peut montrer. Une image de 4K ou plus ne voyage jamais en entier à pleine résolution.
- **Vraie valeur sous le curseur**, avec la mention « moyenne N×N » quand l'affichage est binné.
- **Taille de pixel avec sa provenance.** Si le fichier ne la donne pas, elle est marquée inconnue et peut être saisie ; la règle n'affiche alors que des pixels.
- **Règle et barre d'échelle** en Å, nm ou µm.
- **Piles** : curseurs T, Z et C ; le lecteur ne demande le plan suivant qu'une fois le plan courant affiché.
- **Galerie** (bouton « Galerie », touche `G`) : tous les plans d'une pile côte à côte, pour des particules extraites, les sections d'un volume ou des moyennes de classes. Un clic ouvre le plan dans la visionneuse. Les moyennes de classes 2D de RELION (`*_classes.mrcs`) portent la part des particules et la résolution de chaque classe, lues dans le fichier `_model.star` voisin, et se trient par population ou par numéro.
- **Palettes** : gris, Fire et Ice d'ImageJ, Viridis, HiLo, couleurs pures, import d'un fichier `.lut`.
- **Lien partageable** : le fichier, le plan, le zoom, la fenêtre, la palette et les réglages du mode Cryo sont dans l'adresse.
- **Ouvrir dans Fiji** : télécharge le plan affiché, tel qu'il est traité, en TIFF ImageJ avec sa taille de pixel ; « Pile » télécharge tout le fichier tel qu'il est enregistré (1 Gio au plus). imageBio ne mesure pas de régions et ne seuille pas : cela se fait dans Fiji ([ADR 009](../docs/adr/009-perimetre-cryo-et-live.md)).

## Mode Live

Le bouton **Live** de la barre du haut ouvre le suivi d'une session d'acquisition. Une session est désignée par l'adresse (`#live=<dossier>`) : ouvrir ce lien surveille le dossier, ou retrouve la surveillance qu'on a déjà dessus.

- **Choisir un dossier** dans le navigateur de gauche, puis « Surveiller ce dossier ». Les sessions suivies restent listées ; le carré arrête la surveillance sans effacer ce qui a été indexé, la croix d'une session arrêtée l'oublie.
- **Cartes d'état** : nombre d'images, débit des arrivées (sur 5 minutes et sur l'heure), temps écoulé depuis la dernière image (vert sous 2 minutes, orange sous 10, rouge au-delà), place restante sur le disque, état du scrutateur.
- **Graphe** des acquisitions par minute : les arrêts de la collecte se voient d'un coup d'œil.
- **Grille de vignettes**, la plus récente d'abord. Seules les tuiles visibles existent dans la page : une session de 50 000 images défile comme une de 50. Une image en cours d'écriture est montrée en pointillés, avec la raison, et n'a pas de vignette tant qu'elle n'est pas complète. « Non vérifié » marque les images indexées depuis un dossier au repos, sans contrôle du fichier.
- **Glace.** Chaque tuile porte la glace de son image. Sans étalonnage, c'est la glace relative ln(I₀/I) par rapport aux images les plus claires de la session : 0 pour une image aussi claire qu'elles, 1 pour 2,7 fois moins de coups. Le panneau « Glace » reçoit les coups moyens d'un trou vide (lus dans l'info-bulle d'une tuile prise dans un trou) et le libre parcours moyen du microscope : l'épaisseur est alors en nanomètres, et les trous vides sont marqués. Une marque signale aussi la glace cristalline (rapport de l'anneau de glace au-dessus de 1,2) et les images dix fois plus sombres que la référence ; l'info-bulle donne les nombres, dont la dérive du film quand le serveur la mesure. La carte « Glace » résume les 50 dernières images mesurées.
- **Tri, filtre, vues.** La grille se trie par heure, glace, anneau de glace, défocus, dose ou dérive (les images sans la valeur vont à la fin), se limite aux images marquées ou sans marque, et montre au choix les acquisitions ou les vues d'ensemble d'EPU : trous, carrés, atlas.
- **CTF.** Chaque tuile porte le défocus estimé et la résolution jusqu'à laquelle les anneaux de Thon suivent (« fit »). C'est un indicateur de suivi : ajustement rapide, sans astigmatisme.
- **Seuils d'acceptation**, dans le panneau de gauche : résolution du fit, glace, anneau de glace, dérive, bornes du défocus. Une tuile hors seuils est entourée et dit pourquoi ; la carte « Acceptées » donne la part d'images qui passent ; la grille se limite aux acceptées ou aux rejetées. Une mesure qu'une image n'a pas ne compte pas contre elle. Rien n'est supprimé ni déplacé.
- **Carte des acquisitions** : les positions de platine des images montrées, à la même échelle sur les deux axes, colorées selon le verdict ; un clic ouvre l'image.
- **Un clic sur une tuile** ouvre l'image dans la visionneuse ; le bouton Live y ramène à la session.
- **Alertes**, dans le panneau de gauche : cocher les canaux proposés par le serveur, saisir un ou plusieurs seuils (minutes sans image, pourcentage d'espace libre, pourcentage du débit horaire), enregistrer. « Tester » envoie un message d'essai et dit quels canaux l'ont pris. Les règles appartiennent à la surveillance : elles sont gardées par le serveur. Une alerte en cours entoure de rouge la carte concernée.

La page demande toutes les trois secondes ce qui a changé depuis le dernier changement qu'elle connaît.

## Mode Cryo

Le panneau **Cryo** apparaît pour toute image qui n'est pas en couleurs. Les calculs sont faits par le serveur, à pleine résolution, avant la découpe et le binning ; les réglages se conservent d'un fichier au suivant.

- **Passe-bas et passe-haut en Å.** Préréglages 20, 10 et 5 Å ou valeur libre (validée par Entrée). Filtres gaussiens de gain ½ à la coupure. Si la taille de pixel est inconnue, le filtre attend qu'elle soit saisie ; si le passe-bas n'est pas plus fin que le passe-haut, il n'est pas envoyé. Le panneau dit pourquoi dans les deux cas.
- **Moyenne de frames.** Une fenêtre de N frames à partir de la frame courante : « 5 frames » sur la frame 3 donne la moyenne des frames 3 à 7. La fenêtre suit le curseur de frames et le lecteur ; « Toutes » couvre le film entier.
- **Fractions EER par frame** : le nombre de fractions sommées dans chaque frame affichée. Le bouton « ≈ 1 e⁻/Å² » le calcule à partir de la dose par fraction donnée par le film et de la taille de pixel.
- **Référence de gain.** La liste propose les fichiers dont le nom contient « gain » dans le dossier du film et dans le dossier parent, en commençant par celui que le film nomme dans ses métadonnées. Le serveur vérifie l'orientation de la référence sur le film : confirmée, non vérifiable (trop peu de dose), ou enregistrée dans une autre orientation, avec un bouton **Corriger**. L'orientation et l'inversion (image de coups) se règlent aussi à la main. Un gain n'est conservé d'un fichier au suivant que dans le même dossier et pour le même type de fichier, pour ne pas corriger deux fois une image déjà traitée.
- **Réduction dans Fourier.** Les vues réduites (image entière, zoom arrière) gardent les basses fréquences de la transformée au lieu de moyenner des blocs de pixels : pas de repliement, et les détails plus gros que le nouveau pixel gardent tout leur contraste. Une transformée par plan : plus lent, donc au choix.
- **Pointés.** Un fichier `.star`, `.box`, `.cbox` ou `.cs` cliqué dans la liste de gauche est superposé à l'image en cercles à la taille des boîtes ; celui qui porte le nom de l'image est proposé dans le panneau. Un fichier de jeu entier (`particles.star`) suit d'une image à l'autre et ne montre que les particules de l'image affichée. Réglages : diamètre des cercles, seuil de score, et « Retourner Y » pour les programmes qui comptent les lignes depuis le bas (rien n'est retourné d'office). Le panneau dit combien de particules sont montrées et ce que le serveur a remarqué (points hors de l'image, aucune micrographie du fichier ne correspondant).
- **Spectre de puissance** (touche `P`), à côté de l'image : spectre en échelle logarithmique, anneaux de résolution en Å, anneau de la glace à 3,7 Å, moyenne radiale, résolution sous le curseur, agrandissement du centre ×2 et ×4. Il est calculé sur l'image après moyenne et gain, mais sans les filtres.
- **Défocus.** Le bouton « CTF » du spectre demande au serveur le défocus d'après les anneaux de Thon et dessine les zéros attendus sur le spectre et sur la moyenne radiale. Quand le spectre ne montre pas d'anneaux, il le dit et ne donne pas de valeur.

| Touche | Action |
|---|---|
| `←` `→` | Plan précédent / suivant |
| `Espace` | Lecture / pause |
| `A` | Contraste automatique |
| `I` | Inverser |
| `R` | Règle ; `Échap` pour revenir au déplacement |
| `P` | Spectre de puissance |
| `G` | Galerie des plans d'une pile |
| `F` ou `0` | Ajuster à la fenêtre |
| `1` | 1 pixel image = 1 pixel écran |
| `+` `-` | Zoom |
| Molette | Zoom centré sur le curseur |
| Double-clic | Ajuster à la fenêtre |

## Organisation du code

```
src/
  api/client.ts, types.ts     client typé de l'API ; décodage des plans
  viewer/view.ts              géométrie de la vue, choix du niveau de détail (fonctions pures)
  viewer/gl.ts                moteur de rendu WebGL2 (textures flottantes, shader de contraste)
  viewer/planes.ts            chargement des plans, cache côté client
  viewer/luts.ts              palettes et lecture des fichiers .lut
  viewer/ViewerCanvas.tsx     canevas, interactions, règle, barre d'échelle
  viewer/cryo.ts              mode Cryo : traitements demandés au serveur, pointés, géométrie du spectre (fonctions pures)
  viewer/gallery.ts, Gallery.tsx   galerie d'une pile : ordre et étiquettes (fonctions pures), grille de vignettes
  viewer/SpectrumView.tsx     spectre de puissance, anneaux de résolution, moyenne radiale
  live/store.ts               mode Live : fusion des changements, grille virtuelle, libellés (fonctions pures)
  live/LiveView.tsx           mode Live : sessions suivies, boucle de suivi, mise en page
  live/ImageGrid.tsx, StatsBar.tsx, RateGraph.tsx   grille de vignettes, cartes d'état, graphe
  live/AlertsPanel.tsx, IcePanel.tsx   règles d'alerte de la session, étalonnage de la glace
  live/QualityPanel.tsx, StageMap.tsx  seuils d'acceptation, carte des positions de platine
  Root.tsx                    choix du mode d'après l'adresse (visionneuse ou Live)
  components/                 navigateur de fichiers, contraste, pile, panneau Cryo, informations
  state/url.ts                état de la vue dans l'adresse
  App.tsx                     l'application
tests/                        tests unitaires (vitest)
e2e/                          tests de bout en bout (Playwright) et générateur de fichiers
```

## Tests

```bash
npm run typecheck
```

```bash
npm test
```

```bash
npm run build && npm run e2e
```

Les tests de bout en bout lancent le vrai backend sur des fichiers générés par `e2e/make_fixtures.py`, dont les valeurs sont prévisibles à partir de la position du pixel.

- `e2e/viewer.spec.ts` vérifie les critères du jalon M2 : valeur exacte sous le curseur, aucun plan redemandé quand on règle le contraste, une seule requête de plan en vol pendant la lecture, affichage des images avec NaN et pixel chaud, lien qui reproduit la vue, nom de fichier hostile affiché comme du texte ; l'export vers Fiji relu comme un TIFF calibré ; et la galerie d'une pile de moyennes de classes, triée par population d'après le fichier de RELION.
- `e2e/live.spec.ts` vérifie le mode Live sur une session de 120 expositions écrite par le microscope simulé : affichage immédiat, tuiles limitées à ce qui est visible, image écrite en deux moitiés qui reste « en cours » entre les deux, aller-retour avec la visionneuse, démarrage, arrêt et oubli d'une surveillance, image de glace cristalline déposée dans la session et marquée comme telle, réglage d'une alerte et message d'essai reçu par un récepteur local ; étalonnage de la glace en nanomètres, marques, tri, filtre et vue des carrés ; seuils d'acceptation (tuile rejetée, décompte, filtre) et carte des acquisitions.
- `e2e/cryo.spec.ts` vérifie le mode Cryo sur des fichiers dont la réponse est connue : un réseau de période 10 Å (passe-bas, pic du spectre sur l'anneau de 10 Å), une pile en escalier (moyenne des frames 3 à 7), un film EER (regroupement des fractions) et un détecteur synthétique dont le gain est enregistré tête en bas (détection et correction) ; des pointés dessinés au pixel près (fichier de boîtes, fichier d'un jeu entier, retournement, diamètre, seuil de score) ; et la réduction dans Fourier demandée pour la vue d'ensemble comme pour le détail ; le défocus lu sur une image synthétique de CTF connu, et aucune valeur annoncée sur une image sans anneaux.

La page interdit `eval` et les scripts en ligne (Content-Security-Policy). Dans un test Playwright, passez une fonction à `page.waitForFunction`, pas une chaîne.

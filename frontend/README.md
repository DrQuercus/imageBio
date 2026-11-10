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
- **Palettes** : gris, Fire et Ice d'ImageJ, Viridis, HiLo, couleurs pures, import d'un fichier `.lut`.
- **Lien partageable** : le fichier, le plan, le zoom, la fenêtre, la palette et les réglages du mode Cryo sont dans l'adresse.

## Mode Live

Le bouton **Live** de la barre du haut ouvre le suivi d'une session d'acquisition. Une session est désignée par l'adresse (`#live=<dossier>`) : ouvrir ce lien surveille le dossier, ou retrouve la surveillance qu'on a déjà dessus.

- **Choisir un dossier** dans le navigateur de gauche, puis « Surveiller ce dossier ». Les sessions suivies restent listées ; le carré arrête la surveillance sans effacer ce qui a été indexé.
- **Cartes d'état** : nombre d'images, débit des arrivées (sur 5 minutes et sur l'heure), temps écoulé depuis la dernière image (vert sous 2 minutes, orange sous 10, rouge au-delà), place restante sur le disque, état du scrutateur.
- **Graphe** des acquisitions par minute : les arrêts de la collecte se voient d'un coup d'œil.
- **Grille de vignettes**, la plus récente d'abord. Seules les tuiles visibles existent dans la page : une session de 50 000 images défile comme une de 50. Une image en cours d'écriture est montrée en pointillés, avec la raison, et n'a pas de vignette tant qu'elle n'est pas complète. « Non vérifié » marque les images indexées depuis un dossier au repos, sans contrôle du fichier.
- **Un clic sur une tuile** ouvre l'image dans la visionneuse ; le bouton Live y ramène à la session.

La page demande toutes les trois secondes ce qui a changé depuis le dernier changement qu'elle connaît.

## Mode Cryo

Le panneau **Cryo** apparaît pour toute image qui n'est pas en couleurs. Les calculs sont faits par le serveur, à pleine résolution, avant la découpe et le binning ; les réglages se conservent d'un fichier au suivant.

- **Passe-bas et passe-haut en Å.** Préréglages 20, 10 et 5 Å ou valeur libre (validée par Entrée). Filtres gaussiens de gain ½ à la coupure. Si la taille de pixel est inconnue, le filtre attend qu'elle soit saisie ; si le passe-bas n'est pas plus fin que le passe-haut, il n'est pas envoyé. Le panneau dit pourquoi dans les deux cas.
- **Moyenne de frames.** Une fenêtre de N frames à partir de la frame courante : « 5 frames » sur la frame 3 donne la moyenne des frames 3 à 7. La fenêtre suit le curseur de frames et le lecteur ; « Toutes » couvre le film entier.
- **Fractions EER par frame** : le nombre de fractions sommées dans chaque frame affichée. Le bouton « ≈ 1 e⁻/Å² » le calcule à partir de la dose par fraction donnée par le film et de la taille de pixel.
- **Référence de gain.** La liste propose les fichiers dont le nom contient « gain » dans le dossier du film et dans le dossier parent, en commençant par celui que le film nomme dans ses métadonnées. Le serveur vérifie l'orientation de la référence sur le film : confirmée, non vérifiable (trop peu de dose), ou enregistrée dans une autre orientation, avec un bouton **Corriger**. L'orientation et l'inversion (image de coups) se règlent aussi à la main. Un gain n'est conservé d'un fichier au suivant que dans le même dossier et pour le même type de fichier, pour ne pas corriger deux fois une image déjà traitée.
- **Spectre de puissance** (touche `P`), à côté de l'image : spectre en échelle logarithmique, anneaux de résolution en Å, anneau de la glace à 3,7 Å, moyenne radiale, résolution sous le curseur, agrandissement du centre ×2 et ×4. Il est calculé sur l'image après moyenne et gain, mais sans les filtres.

| Touche | Action |
|---|---|
| `←` `→` | Plan précédent / suivant |
| `Espace` | Lecture / pause |
| `A` | Contraste automatique |
| `I` | Inverser |
| `R` | Règle ; `Échap` pour revenir au déplacement |
| `P` | Spectre de puissance |
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
  viewer/cryo.ts              mode Cryo : traitements demandés au serveur, géométrie du spectre (fonctions pures)
  viewer/SpectrumView.tsx     spectre de puissance, anneaux de résolution, moyenne radiale
  live/store.ts               mode Live : fusion des changements, grille virtuelle, libellés (fonctions pures)
  live/LiveView.tsx           mode Live : sessions suivies, boucle de suivi, mise en page
  live/ImageGrid.tsx, StatsBar.tsx, RateGraph.tsx   grille de vignettes, cartes d'état, graphe
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

- `e2e/viewer.spec.ts` vérifie les critères du jalon M2 : valeur exacte sous le curseur, aucun plan redemandé quand on règle le contraste, une seule requête de plan en vol pendant la lecture, affichage des images avec NaN et pixel chaud, lien qui reproduit la vue, nom de fichier hostile affiché comme du texte.
- `e2e/live.spec.ts` vérifie le mode Live sur une session de 120 expositions écrite par le microscope simulé : affichage immédiat, tuiles limitées à ce qui est visible, image écrite en deux moitiés qui reste « en cours » entre les deux, aller-retour avec la visionneuse, démarrage et arrêt d'une surveillance.
- `e2e/cryo.spec.ts` vérifie le mode Cryo sur des fichiers dont la réponse est connue : un réseau de période 10 Å (passe-bas, pic du spectre sur l'anneau de 10 Å), une pile en escalier (moyenne des frames 3 à 7), un film EER (regroupement des fractions) et un détecteur synthétique dont le gain est enregistré tête en bas (détection et correction).

La page interdit `eval` et les scripts en ligne (Content-Security-Policy). Dans un test Playwright, passez une fonction à `page.waitForFunction`, pas une chaîne.

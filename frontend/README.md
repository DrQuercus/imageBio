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
- **Lien partageable** : le fichier, le plan, le zoom, la fenêtre et la palette sont dans l'adresse.

| Touche | Action |
|---|---|
| `←` `→` | Plan précédent / suivant |
| `Espace` | Lecture / pause |
| `A` | Contraste automatique |
| `I` | Inverser |
| `R` | Règle ; `Échap` pour revenir au déplacement |
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
  components/                 navigateur de fichiers, contraste, pile, informations
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

Les tests de bout en bout lancent le vrai backend sur des fichiers générés par `e2e/make_fixtures.py`, dont les valeurs sont prévisibles à partir de la position du pixel. Ils vérifient les critères du jalon M2 : valeur exacte sous le curseur, aucun plan redemandé quand on règle le contraste, une seule requête de plan en vol pendant la lecture, affichage des images avec NaN et pixel chaud, lien qui reproduit la vue, nom de fichier hostile affiché comme du texte.

La page interdit `eval` et les scripts en ligne (Content-Security-Policy). Dans un test Playwright, passez une fonction à `page.waitForFunction`, pas une chaîne.

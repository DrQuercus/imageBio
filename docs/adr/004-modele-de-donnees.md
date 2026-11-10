# ADR 004 : modèle de données (axes TCZYX, plans 2D, calibration avec provenance)

**État :** acceptée. Le code de référence est `backend/src/imagebio/core/model.py`.

## Décision

**Un `Dataset` est une image paresseuse à 5 dimensions**, lue un plan 2D à la fois. Ses axes sont toujours `T, C, Z, Y, X` ; un axe absent vaut 1. C'est la sémantique des hyperstacks d'ImageJ.

**Un plan** est un tableau `(Y, X)` contigu, dans l'ordre d'octets de la machine, ligne 0 en premier, tel qu'il est stocké dans le fichier : ni retournement, ni mise à l'échelle.

**Types de pixels servis** : `uint8`, `int8`, `uint16`, `int16`, `uint32`, `int32`, `float32`, `float64`. Les lecteurs convertissent le reste : booléen en `uint8`, `float16` en `float32`, entiers 64 bits en `float64` avec un avertissement. Les données complexes sont refusées.

**Où vont les piles :**

| Contenu | Axe | `stack_kind` |
|---|---|---|
| Image seule | aucun | `image` |
| Frames d'un film (dose fractionnée) | T | `movie` |
| Pile d'images sans lien (particules, classes 2D) | T | `stack` |
| Série d'inclinaison | T | `tilt_series` |
| Volume (carte 3D, tomogramme) | Z | `volume` |
| Plusieurs axes non triviaux | T, C, Z | `hyperstack` |

**Couleur** : une image RGB ou RGBA est exposée avec `C = 3` ou `4` et `rgb = true`.

**Calibration avec provenance.** Chaque taille de pixel porte sa source : `header`, `extended_header`, `metadata`, `sidecar`, `user` ou `default`. Un lecteur qui ne trouve pas la taille de pixel renvoie `default` (1 px) et un avertissement ; il n'invente jamais de valeur. L'unité d'origine est conservée (Å, nm, µm) et la conversion en Å est fournie.

**Options de lecture** : certains formats se lisent de plusieurs façons (EER : regroupement des fractions, super-résolution). Ces options font partie de la clé de cache.

## Conséquences

- Tous les modes (visionneuse, Cryo, Live) parlent le même modèle.
- La règle « un plan à la fois » borne la mémoire : un film de 40 frames 4K ne coûte que les plans demandés.
- L'interface doit signaler clairement une taille de pixel inconnue. Le bug de l'ancien CryoViewer (0,5 Å/px inventé pour les EER) ne peut plus se reproduire.

## Points laissés ouverts

- Pyramides multi-résolution (OME-Zarr, TIFF pyramidaux) : prévues avec le jalon M7 ; les niveaux sont déjà listés dans les métadonnées TIFF.
- Sens d'affichage vertical : les données restent telles que stockées ; un éventuel retournement est une option d'affichage du frontend.

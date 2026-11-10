# ADR 001 : cœur sous licence MIT, code GPL isolé dans un worker

**État :** proposée. C'est la valeur par défaut du plan (`docs/PLAN.md` §8.1) ; elle reste à confirmer.

## Contexte

- CryoViewer est déjà sous licence MIT.
- ImageJ 1.x est dans le domaine public, ImageJ2 et ImgLib2 sont sous BSD-2 : porter leurs algorithmes est libre, en gardant l'attribution.
- Bio-Formats et beaucoup de plugins Fiji sont sous GPL. Certaines bibliothèques Python de lecture le sont aussi (`bioio-czi`, `rosettasciio`).
- Un usage sur le réseau interne du labo n'est pas une distribution au sens de la GPL. Partager l'outil avec d'autres labos en serait une.

## Décision

- Le paquet `imagebio` (backend) et le frontend sont sous licence **MIT**.
- Tout code GPL vit dans `workers/imagej/` (jalon M7) : un processus séparé, avec son propre `pyproject` et sa propre note de licence, appelé par une API étroite.
- Le cœur n'importe jamais une bibliothèque GPL, même en option.

## Conséquences

- On garde la possibilité de distribuer le cœur à d'autres labos sans contrainte.
- Les lecteurs du cœur se limitent aux bibliothèques permissives : `mrcfile`, `tifffile`, `imagecodecs`, Pillow, puis `ncempy`, `nd2`, `liffile`, `pylibCZIrw` (LGPL, utilisable comme bibliothèque).
- Un format couvert seulement par du code GPL passe par le worker, donc avec une JVM ou un processus de plus.

## Alternative écartée

Tout mettre sous GPL-3. C'est plus simple, mais cela ferme la porte à une réutilisation permissive du cœur, sans rien apporter tant que l'outil reste interne.

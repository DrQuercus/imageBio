# imageBio

Application web d'imagerie pour la cryo-EM, à déployer sur le réseau du labo :

- une **visionneuse** (MRC, TIFF, EER, images courantes) avec la vraie valeur des pixels, la taille de pixel et sa provenance, et un export calibré vers Fiji ;
- un panneau **Cryo** : moyenne de frames, référence de gain, filtres en Å, spectre de puissance et défocus, pointés, classes 2D ;
- un mode **Live**, pour suivre une collecte au microscope : images à mesure qu'elles arrivent, glace, défocus, seuils d'acceptation, carte des acquisitions, alertes.

L'éditeur dans l'esprit d'ImageJ du premier plan a été retiré ([ADR 009](docs/adr/009-perimetre-cryo-et-live.md)). Le backend ([`backend/`](backend/README.md)) et l'interface ([`frontend/`](frontend/README.md)) sont écrits ; rien n'a encore tourné sur de vraies données du labo. Pour essayer sur un poste :

```bash
pip install -e "./backend[dev]" && (cd frontend && npm ci && npm run build)
```

```bash
imagebio serve --local /chemin/vers/mes/donnees
```

Puis ouvrir `http://127.0.0.1:8000`. Pour voir le mode Live sans microscope, lancer dans un autre terminal un microscope simulé qui écrit dans un sous-dossier, puis le surveiller depuis le bouton **Live** :

```bash
imagebio simulate /chemin/vers/mes/donnees/session_test
```

Documents à lire :

- [`docs/guide-utilisateur.md`](docs/guide-utilisateur.md) : le mode d'emploi, en une page.
- [`docs/exploitation.md`](docs/exploitation.md) : installer sur un serveur ([`deploy/`](deploy/README.md)), sauvegarder, mettre à jour, surveiller.
- [`docs/PLAN.md`](docs/PLAN.md) : évaluation du projet, architecture cible, feuille de route et choix du modèle.
- [`docs/adr/`](docs/adr/README.md) : décisions d'architecture.
- [`docs/audit/AUDIT.md`](docs/audit/AUDIT.md) : audit vérifié de CryoViewer et de CryoViewer Live.
- [`docs/research/`](docs/research/) : études sur les fondations (ImageJ/Fiji, Bio-Formats, formats) et sur la cryo-EM (EER, EPU, SerialEM, outils de suivi en direct).

Le code existant se trouve dans `legacy/cryoviewer/` (CryoViewer, Python) et `legacy/cryoviewer-live/` (CryoViewer Live, Node/React). Il ne reçoit plus que les correctifs du jalon M0. Les archives d'origine restent disponibles dans l'historique git.

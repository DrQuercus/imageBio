# imageBio

Application web d'imagerie scientifique à déployer sur le réseau du labo. Elle aura trois modes :

- une visionneuse et un éditeur dans l'esprit d'ImageJ, capable d'ouvrir la plupart des formats scientifiques ;
- un mode **Cryo**, avec les outils spécifiques à la cryo-EM ;
- un mode **Live**, pour suivre en temps réel une collecte au microscope.

Le backend (jalon M1, [`backend/`](backend/README.md)) et la visionneuse web (jalon M2, [`frontend/`](frontend/README.md)) sont écrits. Pour essayer :

```bash
pip install -e "./backend[dev]" && (cd frontend && npm ci && npm run build)
```

```bash
imagebio serve --local /chemin/vers/mes/donnees
```

Puis ouvrir `http://127.0.0.1:8000`. Documents à lire :

- [`docs/PLAN.md`](docs/PLAN.md) : évaluation du projet, architecture cible, feuille de route et choix du modèle.
- [`docs/adr/`](docs/adr/README.md) : décisions d'architecture.
- [`docs/audit/AUDIT.md`](docs/audit/AUDIT.md) : audit vérifié de CryoViewer et de CryoViewer Live.
- [`docs/research/`](docs/research/) : études sur les fondations (ImageJ/Fiji, Bio-Formats, formats) et sur la cryo-EM (EER, EPU, SerialEM, outils de suivi en direct).

Le code existant se trouve dans `legacy/cryoviewer/` (CryoViewer, Python) et `legacy/cryoviewer-live/` (CryoViewer Live, Node/React). Il ne reçoit plus que les correctifs du jalon M0. Les archives d'origine restent disponibles dans l'historique git.

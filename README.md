# imageBio

Application web d'imagerie scientifique à déployer sur le réseau du labo. Elle aura trois modes :

- une visionneuse et un éditeur dans l'esprit d'ImageJ, capable d'ouvrir la plupart des formats scientifiques ;
- un mode **Cryo**, avec les outils spécifiques à la cryo-EM ;
- un mode **Live**, pour suivre en temps réel une collecte au microscope.

Le projet est en phase de cadrage. Documents à lire :

- [`docs/PLAN.md`](docs/PLAN.md) : évaluation du projet, architecture cible, feuille de route et choix du modèle.
- [`docs/audit/AUDIT.md`](docs/audit/AUDIT.md) : audit vérifié de CryoViewer et de CryoViewer Live.
- [`docs/research/`](docs/research/) : études sur les fondations (ImageJ/Fiji, Bio-Formats, formats) et sur la cryo-EM (EER, EPU, SerialEM, outils de suivi en direct).

Le code existant est encore fourni sous forme d'archives : `CryoViewer.zip` et `cryoviewer-live.tar.gz`. Il sera décompressé dans `legacy/` au jalon M0.

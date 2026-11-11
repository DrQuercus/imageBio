# ADR 009 : le périmètre se recentre sur le cryo et le suivi en direct

**État :** acceptée (11 novembre 2026).

## Contexte

Le plan d'origine prévoyait trois modes : une visionneuse et éditeur « façon ImageJ » (jalons M5 et M6, plus un worker ImageJ et Bio-Formats dans M7), le mode Cryo et le mode Live. Une fois M3 et M4 faits, la question s'est posée de l'utilité de la partie ImageJ. Elle pesait 9 à 13 sessions, la moitié de ce qui restait, pour refaire des fonctions que Fiji offre déjà, avec l'obligation de retrouver ses résultats à 1e-6 près (mesures, seuils, particules). Ce qui gênait au quotidien, la visionneuse cryo et le suivi d'une collecte, était livré.

## Décision

1. **M5 et M6 sont retirés** : pas de ROI Manager, de mesures à la ImageJ, de seuils, d'Analyze Particles ni d'enregistreur de commandes dans imageBio.
2. **Le worker ImageJ et Bio-Formats de M7 est retiré** avec eux. Il n'y a donc plus de code GPL à isoler pour l'instant ; la règle de l'ADR 001 (cœur MIT, GPL dans un processus séparé) reste valable si un tel worker revient.
3. **Le pont vers Fiji reste** : « Ouvrir dans Fiji » exporte le plan affiché, ou la pile, en TIFF ImageJ avec sa calibration. Pour tout ce qu'imageBio ne fait pas, on ouvre l'image dans Fiji.
4. **Les lecteurs de formats s'ajoutent à la demande** (DM4, par exemple), un par un, avec leur comparaison à une référence.
5. **M7 devient « qualité des acquisitions dans le Live »** : CTF, seuils d'acceptation, carte des acquisitions, puis MotionCor3 et CTFFIND5 en sous-processus. M8 (déploiement) ne change pas.

## Conséquences

- La visionneuse reste ce qu'elle est : contraste, piles, règle, galerie, lien partageable. Elle ne mesure pas de régions.
- Les utilisateurs de microscopie optique ont une visionneuse correcte pour TIFF et hyperstacks, pas un remplaçant de Fiji ; le pilote de M8 devra dire si cela leur suffit.
- Le nom du projet et la mention « ImageJ » de certains documents anciens ne décrivent plus le périmètre : `docs/PLAN.md` fait foi.

## Alternatives écartées

- **Garder M5 seul** (ROI et mesures) : c'est la partie la plus coûteuse à rendre identique à Fiji, et la moins demandée en cryo.
- **Intégrer ImageJ.js ou un Fiji distant dans la page** : une deuxième application dans la première, avec ses propres chemins, droits et licences.

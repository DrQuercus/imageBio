# imageBio : guide de l'utilisateur

imageBio s'ouvre dans le navigateur, à l'adresse donnée par l'administrateur. Rien à installer. Les fichiers restent sur le serveur de données : imageBio les lit, il n'écrit jamais dedans.

Deux écrans, en haut de la page : la **Visionneuse**, avec son panneau **Cryo**, et le **Live**, qui suit une collecte en cours.

## Ouvrir une image

Le panneau de gauche liste les dossiers auxquels vous avez droit. Cliquez sur un fichier : MRC et MRCS, TIFF (y compris ImageJ et OME), EER, PNG, JPEG. La zone « Filtrer… » réduit la liste.

L'adresse de la page décrit ce que vous voyez (fichier, plan, zoom, réglages) : copiez-la pour montrer exactement la même vue à un collègue.

| Action | Souris | Clavier |
|---|---|---|
| Zoom | molette, centrée sur le curseur | `+` `-` |
| Ajuster à la fenêtre, 1 pixel image = 1 pixel écran | | `F`, `1` |
| Déplacer | glisser | `Échap` revient à cet outil |
| Mesurer une distance (Å, nm) | glisser avec la règle | `R` |
| Contraste automatique, inverser | | `A`, `I` |
| Plan suivant, précédent ; lire la pile | | `→` `←`, `Espace` |
| Spectre de puissance | | `P` |
| Toute la pile côte à côte (particules, classes 2D) | | `G` |

La valeur sous le curseur est la vraie valeur du pixel. Le contraste, le gamma et la palette se règlent sans rien recharger. La taille de pixel affichée dit d'où elle vient (en-tête, XML, saisie) ; un avertissement apparaît quand le fichier ne la donne pas : saisissez-la.

**Ouvrir dans Fiji** télécharge le plan affiché, tel qu'il est traité, en TIFF ImageJ avec sa taille de pixel ; « Pile » télécharge tout le fichier. Les mesures de régions, seuillages et comptages se font dans Fiji.

## Panneau Cryo

- **Moyenne de frames** : choisissez la plage ; l'image affichée est la moyenne de ces frames.
- **Référence de gain** : choisissez le fichier. imageBio vérifie son orientation contre le film et vous dit si une autre convient mieux : c'est vous qui l'appliquez (« Corriger »), jamais lui en silence.
- **Passe-bas, passe-haut** : en ångströms.
- **Spectre de puissance** (`P`) : le bouton d'estimation donne le défocus d'après les anneaux de Thon et dessine les zéros attendus. C'est un ajustement rapide, sans astigmatisme : il sert à juger une image, pas à remplacer CTFFIND.
- **Pointés** : ouvrez un fichier STAR, `.box`, `.cbox` ou `.cs` pour superposer les particules à l'image. Si les cercles sont en miroir vertical, cochez l'inversion des lignes : certains programmes comptent depuis le bas. Le curseur de score masque les particules faibles.
- **Classes 2D** : ouvrez la pile des classes d'un job RELION, puis `G`. Les classes peuvent être triées par population.

## Live : suivre une collecte

1. Dans **Sessions suivies**, choisissez le dossier où le microscope écrit (session EPU ou SerialEM) et lancez le suivi. Le suivi continue quand vous fermez la page.
2. La grille montre les images à mesure qu'elles arrivent. Une image n'apparaît que lorsque son fichier est complet.
3. Le bandeau donne le débit, la dernière image, les fichiers non finis, l'espace disque, la glace et la part d'images acceptées.

Sur chaque tuile : le défocus estimé, jusqu'où vont les anneaux, la glace, et un repère pour un anneau de glace cristalline ou un trou vide. La grille se trie et se filtre selon ces mesures. Un clic ouvre l'image dans la visionneuse.

- **Glace** : par défaut, relative aux images les plus claires de la session. Pour une épaisseur en nm, donnez les coups moyens d'un trou vide (I₀) et le libre parcours moyen du microscope (300 à 400 nm).
- **Seuils d'acceptation** : résolution d'ajustement, glace, anneau, dérive, plage de défocus. Les images hors seuils sont marquées « rejetée » ; rien n'est supprimé ni déplacé.
- **Carte des acquisitions** : les positions de platine, colorées selon les seuils. Elle montre quelle zone de la grille donne de bonnes images.
- **Traitement** (si l'administrateur a déclaré des programmes sur le serveur) : cochez la case pour que les films qui arrivent soient alignés et leur CTF ajusté. L'info-bulle de chaque tuile donne alors les deux défocus, l'astigmatisme et le mouvement ; le tri et les seuils s'en servent. Seules les images arrivées pendant le suivi sont traitées, la plus récente d'abord, et les utilisateurs passent à tour de rôle.
- **Alertes** : choisissez un canal (téléphone, courriel : ceux que l'administrateur a déclarés) et des conditions : aucune image depuis N minutes, espace libre sous N %, débit en baisse. Le bouton d'essai envoie un message de test.

« Arrêter » garde l'index de la session ; « Oublier » l'efface.

## Limites à connaître

- Les mesures du Live (défocus, glace, dérive) sont des indicateurs de suivi, calculés vite sur chaque image. Elles ne remplacent pas le traitement.
- Sur une image de moins de 512 pixels de côté, l'anneau de glace et le défocus ne sont pas calculés : ce serait du bruit.
- Une taille de pixel saisie à la main fausse la règle et le défocus si elle est erronée.
- Une tuile marquée « non vérifié » vient d'un dossier déjà au repos quand le suivi a commencé : son fichier n'a pas été contrôlé.

Un problème, une image qui s'affiche mal : notez l'adresse de la page et envoyez-la à l'administrateur.

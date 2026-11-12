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
| Image suivante, précédente du dossier, réglages gardés | ◀ ▶ en haut | `N`, `B` (ou `Page ↓`, `Page ↑` hors d'un curseur) |
| Cercle d'un diamètre en Å autour du curseur | | `C` |
| Couper ou rallumer les filtres | | `L` |

La valeur sous le curseur est la vraie valeur du pixel. Le contraste, le gamma et la palette se règlent sans rien recharger.

**Taille de pixel.** Elle est en haut à droite, en orange, comme dans la première version : la valeur utilisée, d'où elle vient (en-tête, XML, saisie), la taille à l'écran quand l'image est réduite, et Nyquist. Quand le fichier ne la donne pas, saisissez-la. Quand l'en-tête se trompe, tapez la bonne valeur : elle remplace celle du fichier pour la règle, les filtres, le CTF, le lien et l'export vers Fiji ; « Rétablir » revient à celle du fichier. Elle vous suit avec `N` et `B` vers les images de même taille et de même taille de pixel d'origine, pas vers les autres (vignettes, films bruts).

**Règle, profil, cercle.** La longueur mesurée s'affiche en grand sous la taille de pixel, avec le profil d'intensité le long de la règle ; « Copier » met les valeurs (distance en Å, intensité) dans le presse-papier pour un tableur. Le profil est lu sur les pixels affichés : en vue réduite, ce sont des moyennes, et le panneau le dit ; zoomez pour la pleine résolution. Le cercle (`C`) suit le curseur à un diamètre en Å choisi : la taille d'une particule, d'un trou ou d'une boîte.

**Ouvrir dans Fiji** télécharge le plan affiché, tel qu'il est traité, en TIFF ImageJ avec sa taille de pixel ; « Pile » télécharge tout le fichier. Les mesures de régions, seuillages et comptages se font dans Fiji.

## Panneau Cryo

- **Moyenne de frames** : choisissez la plage ; l'image affichée est la moyenne de ces frames.
- **Référence de gain** : choisissez le fichier. imageBio vérifie son orientation contre le film et vous dit si une autre convient mieux : c'est vous qui l'appliquez (« Corriger »), jamais lui en silence.
- **Passe-bas, passe-haut** : en ångströms, au curseur (échelle logarithmique), aux préréglages ou à la valeur tapée. Pendant que vous tenez le curseur, la vue d'ensemble suit ; le détail en pleine résolution arrive quand vous le lâchez. Profil **Doux** (gaussien, sans oscillations) ou **Net** (plat jusqu'à la coupure puis raide, comme on l'attend pour voir des particules). `L` montre l'image sans filtre et la rend avec.
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
- **Graphiques** (à côté de « Carte ») : images et verdicts dans le temps, défocus mesuré face au défocus visé, résolution du CTF et glace dans le temps, histogrammes, tableau par carré de grille. Ils portent sur les images montrées : les filtres s'appliquent.
- **Rapport de qualité** : il montre l'atlas de la grille et la vue de chaque carré où des images ont été prises, encadrée en vert, orange ou rouge selon la part d'images acceptées. L'atlas est celui qu'EPU a mis dans la session (`Atlas/Atlas_1.mrc`) ; s'il est ailleurs (EPU le garde souvent dans une session à part), « Choisir… » permet de le désigner. « Rapport maintenant » l'écrit tout de suite ; il est aussi écrit à l'arrêt de la surveillance et quand la collecte ne reçoit plus d'image depuis un moment (case à décocher pour ne pas le faire). « Lire » l'ouvre dans le navigateur, d'où on peut l'imprimer en PDF ; « ZIP » donne la page, les chiffres (JSON) et une ligne par image (CSV) : c'est ce fichier à glisser dans la bibliothèque des collectes. Le détail : [`rapport-live.md`](rapport-live.md).
- **Microscope** : la tension, le Cs et le contraste d'amplitude de la session, utilisés pour estimer le CTF. La tension écrite dans les fichiers d'acquisition passe avant ; sans elle, réglez-la ici si ce n'est pas celle indiquée (une image à 200 kV lue comme à 300 kV reçoit un défocus faux d'environ 27 %).
- **Traitement** (si l'administrateur a déclaré des programmes sur le serveur) : cochez la case pour que les films qui arrivent soient alignés et leur CTF ajusté. L'info-bulle de chaque tuile donne alors les deux défocus, l'astigmatisme et le mouvement ; le tri et les seuils s'en servent. Seules les images arrivées pendant le suivi sont traitées, la plus récente d'abord, et les utilisateurs passent à tour de rôle.
- **Alertes** : choisissez un canal (téléphone, courriel : ceux que l'administrateur a déclarés) et des conditions : aucune image depuis N minutes, espace libre sous N %, débit en baisse. Le bouton d'essai envoie un message de test.

« Arrêter » garde l'index de la session ; « Oublier » l'efface.

## Limites à connaître

- Les mesures du Live (défocus, glace, dérive) sont des indicateurs de suivi, calculés vite sur chaque image. Elles ne remplacent pas le traitement.
- Sur une image de moins de 512 pixels de côté, l'anneau de glace et le défocus ne sont pas calculés : ce serait du bruit.
- Une taille de pixel saisie à la main remplace celle du fichier : si elle est fausse, la règle, les filtres et le défocus le sont aussi. Le panneau rappelle la valeur du fichier tant qu'elle est remplacée.
- Une tuile marquée « non vérifié » vient d'un dossier déjà au repos quand le suivi a commencé : son fichier n'a pas été contrôlé.

Un problème, une image qui s'affiche mal : notez l'adresse de la page et envoyez-la à l'administrateur.

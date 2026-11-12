# ADR 012 : rapport de qualité d'une collecte, et microscope d'une session

**État :** acceptée (lot B, 12 novembre 2026).

## Contexte

Le laboratoire veut, à la fin d'une collecte, un document à ranger dans sa bibliothèque de collectes, et des chiffres qu'un programme ou une IA locale peut relire. La tension du microscope était une valeur du serveur : fausse pour l'un des deux microscopes d'un laboratoire qui en a deux.

## Décisions

### Un rapport, trois formats, aucune dépendance

- **HTML autonome** : vignettes en JPEG intégrées, graphiques en SVG tracés à partir des nombres, feuille de style dans le fichier. Le PDF s'obtient par l'impression du navigateur : pas de moteur PDF côté serveur (une dépendance lourde de plus, pour un fichier que le navigateur fait aussi bien).
- **JSON** au schéma nommé et versionné (`imagebio.live-report`, [documenté](../rapport-live.md)), pour les programmes et l'IA ; **CSV**, une ligne par image ; un **zip** des trois.

### La page est écrite pour être ouverte n'importe où

Elle sortira d'imageBio : messagerie, bibliothèque, disque. Donc :
- aucun script, et une Content-Security-Policy dans la page qui interdit de rien charger ;
- tout texte venu d'un nom de fichier ou d'un fichier de métadonnées est échappé ;
- servie par imageBio pour la lire dans le navigateur, elle l'est dans un bac à sable (`sandbox`) : pas même l'origine de l'application ;
- dans le CSV, une cellule qu'un tableur prendrait pour une formule est écrite comme du texte.

### Le rapport dit ce que l'utilisateur a vu

Le verdict sur chaque image (seuils, raisons, marques) était calculé seulement par l'interface. Il l'est maintenant aussi par le serveur (`live/quality.py`), écrit de la même façon, et les deux sont vérifiés sur les mêmes cas (`backend/tests/fixtures/verdict_cases.json`) : ils ne peuvent pas diverger sans qu'un test le dise.

### Ce qui n'a pas pu être mesuré est compté, pas deviné

Le rapport commence par ce que ses chiffres ne disent pas, avec les nombres d'images concernées.

### Quand

À la demande, à l'arrêt de la surveillance (désactivable par session), et après un silence de la collecte (`[live] report_after_idle_min`, 120 minutes par défaut ; vérifié chaque minute, une fois par silence), parce qu'on oublie souvent d'arrêter une surveillance. Vingt rapports gardés par surveillance ; écrits entiers ou pas du tout (dossier temporaire, puis renommage).

### Le microscope d'une session

La tension, le Cs et le contraste d'amplitude se règlent pour un dossier de données (`[[roots]]`) et pour une session (le dossier surveillé). L'ordre : le fichier d'acquisition, la session, le dossier de données, le serveur. Les valeurs appartiennent au dossier surveillé, pas à la surveillance : les mesures sont partagées par tous ceux qui le suivent. Chaque mesure garde les valeurs avec lesquelles son CTF a été estimé ; les changer fait mesurer de nouveau ce qui en avait utilisé d'autres.

### L'atlas et les carrés (ajouté le 12 novembre 2026)

La page montre l'atlas de la grille et, pour chaque carré où des images ont été prises, la vue qu'EPU en a prise, encadrée selon la part d'images acceptées (vert 70 % et plus, orange 40 à 70 %, rouge en dessous ; 48 carrés au plus, les plus fournis).

- **L'atlas** est celui qu'EPU assemble (`Atlas/Atlas_1.mrc`, pas ses tuiles) quand il est dans le dossier surveillé. EPU le garde le plus souvent dans une session à part : l'utilisateur peut en **choisir** un (`PUT /api/live/watches/{id}/atlas?path=`).
- Le choix appartient à la **surveillance**, pas au dossier comme le microscope : un autre utilisateur du même dossier n'a peut-être pas le droit de lire ce fichier. Il est autorisé au choix et de nouveau à chaque rapport demandé ; un rapport fait de lui-même (fin, silence) lit le fichier choisi s'il est toujours un fichier ordinaire atteint sans lien, comme la surveillance continue de lire son dossier.
- Un atlas choisi qui ne peut pas être montré est **dit** (illisible, plus accessible), pas omis en silence.
- **Les carrés ne sont pas placés sur l'atlas** : il faut le lien entre coordonnées de la platine et pixels de l'atlas tel qu'EPU l'écrit, à lire sur une vraie session. Les positions de la platine restent dans leur carte.

## Relecture indépendante

Le chemin des rapports a été relu par un agent chargé d'y trouver des défauts réels, chacun reproduit. Cinq, tous corrigés avec un test de régression qui échoue sur l'ancien code :

| Gravité | Défaut | Correction |
|---|---|---|
| haute | Au-delà de 96 h de session (ou avec une seule date fausse), division par zéro : aucun rapport, ni demandé ni automatique, et un nouvel essai chaque minute ; une date en l'an 1 fabriquait 740 000 tranches | heure la plus chargée comptée sur les dates elles-mêmes ; tranches en jours au-delà ; dates hors de 2000…maintenant mises hors de l'axe du temps et signalées |
| moyenne | Des nombres finis mais démesurés lus dans un XML ou un mdoc (1e308) débordaient une fois multipliés ou sommés | au-delà de 10¹², une valeur est tenue pour inconnue |
| moyenne | Une surveillance oubliée pendant qu'on fabriquait son rapport : le rapport pouvait s'attacher à la nouvelle surveillance d'un autre utilisateur, qui reprenait le même numéro | le rapport n'est enregistré que pour la surveillance même (numéro, utilisateur, date de création), dans la transaction qui l'enregistre |
| basse | CSV : une cellule entre guillemets coupée par un tableur réglé sur « ; » donnait une formule ; des espaces devant le signe aussi | textes entre guillemets, et signe de formule neutralisé après tout séparateur et après des espaces |
| basse | Une écriture ratée (disque plein) laissait un dossier partiel à chaque essai, et une erreur interne | rien de partiel ne reste ; erreur 503 au nom public ; nouvel essai automatique une heure plus tard |

## Conséquences

- Index au schéma 8 (rapports, historique des alertes, microscope de la session), puis 9 (atlas choisi par surveillance).
- Les rapports ne sont pas dans les sauvegardes : ils se téléchargent, et la bibliothèque du laboratoire est leur place.
- Le dépôt automatique dans la bibliothèque (le serveur écrivant lui-même dans un dossier) n'est pas fait : il ajouterait un chemin d'écriture, à décider.
- Les graphiques du panneau sont calculés par l'interface (`live/stats.ts`) à la manière du rapport : deux codes pour les mêmes chiffres, testés chacun contre des valeurs calculées à part.

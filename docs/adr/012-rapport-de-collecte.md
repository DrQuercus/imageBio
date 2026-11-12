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

## Conséquences

- Index au schéma 8 (rapports, historique des alertes, microscope de la session).
- Les rapports ne sont pas dans les sauvegardes : ils se téléchargent, et la bibliothèque du laboratoire est leur place.
- Le dépôt automatique dans la bibliothèque (le serveur écrivant lui-même dans un dossier) n'est pas fait : il ajouterait un chemin d'écriture, à décider.
- Les graphiques du panneau sont calculés par l'interface (`live/stats.ts`) à la manière du rapport : deux codes pour les mêmes chiffres, testés chacun contre des valeurs calculées à part.

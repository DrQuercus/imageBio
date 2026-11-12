# ADR 014 : page de paramètres, groupes et rôle d'administrateur gérés par imageBio

**État :** acceptée (lot D, 12 novembre 2026). Elle complète l'ADR 005 (authentification déléguée au proxy) sans la remettre en cause.

## Contexte

La demande : une page de paramètres pour les comptes, les réglages et la configuration. Deux limites de l'existant :

- les comptes locaux de Caddy n'ont pas de groupes : tous les comptes voient tous les dossiers de données ;
- tout réglage passait par `imagebio.toml` et un redémarrage.

Les deux décisions ont été prises par l'utilisateur le 12 novembre 2026 :

- **groupes et rôle d'administrateur gérés par imageBio, mots de passe au proxy** ;
- **réglages modifiables par un administrateur, sauf les programmes externes et les secrets**.

## Décision

### Comptes

- **Les mots de passe restent au proxy** (comptes locaux de Caddy, ou authentification unique). imageBio ne stocke ni ne vérifie aucun mot de passe (ADR 005).
- **Un administrateur donne à un nom de compte des groupes**, qui s'ajoutent à ceux du proxy et décident des dossiers lisibles, ainsi que **le rôle d'administrateur**. Le nom est celui que transmet le proxy ; la personne n'a pas besoin d'être déjà venue.
- **Les administrateurs de départ sont nommés dans le fichier** (`[auth] admins`, `admin_groups`). La page ne peut pas les retirer, et personne ne se retire le rôle à soi-même : le serveur ne se retrouve jamais sans administrateur. En mode autonome, l'unique utilisateur local est administrateur.

### Réglages

- **Ce que la page change est écrit dans `<état>/settings.json`**, pas dans `imagebio.toml`. Ce dernier reste celui de l'installateur, souvent monté en lecture seule. Le fichier de la page remplace le fichier de configuration **section par section** :
  - dossiers de données (la liste entière) ;
  - microscope (clé par clé) ;
  - une partie du Live (clé par clé : rapport après un silence, libre parcours moyen, mesures, CTF, dérive) ;
  - canaux d'alerte (la liste entière).
  
  « Revenir au fichier » oublie une section.
- **Jamais modifiables par la page** :
  - les secrets : ils restent dans l'environnement, et la page dit seulement s'ils sont réglés ;
  - les programmes externes (ADR 011) ;
  - ce qui décide à qui le serveur fait confiance : adresse, noms d'hôte, proxy, en-têtes d'identité, administrateurs du fichier ;
  - les caches.
  
  La page les montre en lecture seule.
- **Chaque changement est vérifié en construisant les réglages qu'il donnerait**, avec les mêmes contrôles que le fichier. Il est ensuite :
  - écrit en entier ou pas du tout (fichier temporaire, puis renommage ; droits 0600) ;
  - gardé dans un historique (50 versions) ;
  - noté dans un journal : qui, quand, quoi, sans adresse de canal ni chemin ;
  - **appliqué aussitôt** : les dossiers sont remplacés dans `RootSet`, et le service Live prend les nouveaux réglages. Rien de ce que la page change ne demande un redémarrage.
- **Versions** : chaque changement porte la version des réglages sur laquelle il a été fait. Un changement fait sur une version dépassée (un autre administrateur a enregistré entre-temps) est refusé (409), et la page recharge.
- **Un dossier ajouté par la page** doit :
  - être un dossier existant ; il est enregistré sous son chemin réel, liens résolus ;
  - se trouver **dans un dossier de données du fichier**, ou sous un dossier que le fichier permet (`server.root_parents`). Rien de plus large par défaut : un administrateur nommé dans la page ne doit pas ouvrir ce que l'installateur n'a pas ouvert ;
  - ne contenir ni être contenu dans le dossier d'état du serveur (réglages, index, rapports), ni le dossier de travail des programmes externes (les sommes de toutes les sessions).
- **Ce contrôle est refait chaque fois que les dossiers sont mis en service** : au démarrage, à chaque changement de la page, et quand un autre serveur sur le même dossier d'état a changé le fichier. Un dossier dont le chemin réel n'est plus celui enregistré (un lien posé à sa place par quelqu'un qui écrit dans les données), ou qui n'est plus dans un emplacement permis (le fichier a été resserré), n'est plus servi ; il est marqué indisponible.
- **En mode `--local`**, les dossiers sont ceux de la ligne de commande : la page ne les remplace pas.
- **Un `settings.json` illisible ou invalide arrête le serveur** avec un message d'une ligne. Démarrer sur le seul fichier rouvrirait aux groupes du fichier un dossier restreint par la page.
- **Les administrateurs voient les chemins du serveur et les adresses des canaux** : ils les configurent. Les utilisateurs, jamais. Les messages d'erreur, eux, ne contiennent toujours aucun chemin.
- **Sauvegarde** : `settings.json` et son journal sont sauvegardés avec l'index. La restauration ne remet les réglages en place qu'avec `--settings` : réparer un index abîmé ne doit pas ramener, sans qu'on le voie, des groupes et des rôles retirés depuis. Les réglages de la sauvegarde sont lus et vérifiés avant que rien ne soit déplacé ; les actuels sont mis de côté ; la version continue de monter ; le journal reçoit une ligne et n'est pas rembobiné.
- **Un serveur tient son dossier d'état tant qu'il tourne**, même s'il n'a jamais ouvert le Live : aucune restauration ne passe sous lui.
- **Deux serveurs sur un même dossier d'état** : chacun relit le fichier quand il a changé (au plus une fois par seconde), et un changement fait sur une version que l'autre a dépassée est refusé.

## Relecture indépendante

Le serveur de ce lot a été relu par un agent chargé d'y trouver des défauts réels. Tous sont corrigés, chacun avec un test qui échoue sur le code d'avant (`backend/tests/admin/test_review.py`) :

| Gravité | Défaut | Correction |
|---|---|---|
| haute | Un dossier de la page n'était contrôlé qu'à l'enregistrement : remplacé ensuite par un lien vers le dossier d'état, il montrait les réglages et l'index à tous ; un `settings.json` écrit à la main pouvait servir `/etc` | chemin réel enregistré, contrôle refait à chaque mise en service ; sinon, indisponible |
| moyenne | Une restauration dont les réglages manquaient laissait le serveur sans réglages : un dossier restreint par la page redevenait ouvert | réglages lus et vérifiés avant tout déplacement |
| moyenne | Un serveur qui n'avait jamais ouvert le Live ne tenait pas son dossier d'état : une restauration passait sous lui | le serveur tient le dossier d'état tant qu'il tourne |
| moyenne | Une restauration `--force` (conseillée pour un index abîmé) ramenait aussi des rôles retirés depuis | réglages restaurés seulement avec `--settings` |
| moyenne | Une sauvegarde contenant des réglages n'était jamais élaguée par `--keep` | élaguée comme les autres |
| moyenne | La commande `restore` plantait sur une sauvegarde sans index | message, et `--settings` |
| moyenne | Par défaut, un dossier pouvait être ajouté sous le parent d'une racine du fichier : une racine `/data` ouvrait `/` ; le dossier de travail des programmes n'était pas exclu | seulement dans les racines du fichier, ou sous `server.root_parents` ; dossier de travail exclu |
| moyenne | `--local` : les dossiers enregistrés par la page remplaçaient ceux d'une autre ligne de commande ; les tests lisaient le dossier d'état du développeur | la ligne de commande décide en `--local` ; les tests ont leur propre dossier |
| basse | Les dossiers et leurs chemins réels étaient remplacés en deux temps : une requête entre les deux pouvait mélanger les deux | un seul remplacement |
| basse | Des corps malformés (valeur d'un autre type, nombre de 400 chiffres, JSON très imbriqué, demi-caractère) donnaient une erreur 500 | vérifiés avant tout, refusés en 400 |
| basse | Les fichiers d'exemple faisaient de « alice » un administrateur | commenté |
| basse | Journal écrit avant l'application ; deux serveurs s'écrasaient ; caractères invisibles et espace insécable dans les noms ; liste des comptes vus élaguée sans verrou | application d'abord, journal ensuite ; relecture du fichier ; noms vérifiés, seuls les espaces et tabulations sont retirés ; verrou |

## Conséquences

- Un laboratoire sans LDAP ni authentification unique peut enfin restreindre des dossiers à des groupes, avec les comptes locaux de Caddy.
- Ajouter un compte reste une ligne dans le fichier `users` de Caddy (ou se fait dans l'authentification unique) ; seuls ses groupes et son rôle se règlent dans imageBio.
- Deux sources de réglages : la page l'indique section par section (« fichier de configuration » ou « changé ici »), et « Revenir au fichier » ramène à une seule.
- Une surveillance du Live dont le dossier a été retiré n'est plus accessible à son utilisateur (son dossier est vérifié à chaque requête) ; son scanner continue jusqu'à l'arrêt.
- Rien n'a été essayé avec un vrai proxy et de vrais comptes : la pile Caddy des tests transmet un nom sans groupes, ce qui est justement le cas que les groupes d'imageBio couvrent.

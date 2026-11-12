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
  - être un dossier existant, liens suivis ;
  - se trouver sous `server.root_parents` (par défaut : les dossiers parents des racines du fichier), ou être l'une des racines du fichier ;
  - ne contenir ni être contenu dans le dossier d'état du serveur (réglages, index, rapports).
- **Un `settings.json` illisible ou invalide arrête le serveur** avec un message d'une ligne. Démarrer sur le seul fichier rouvrirait aux groupes du fichier un dossier restreint par la page.
- **Les administrateurs voient les chemins du serveur et les adresses des canaux** : ils les configurent. Les utilisateurs, jamais. Les messages d'erreur, eux, ne contiennent toujours aucun chemin.
- **Sauvegarde** : `settings.json` et son journal sont sauvegardés avec l'index. La restauration les remet en place avec `--force`, met les actuels de côté, et ajoute une ligne au journal (le journal n'est pas rembobiné).

## Conséquences

- Un laboratoire sans LDAP ni authentification unique peut enfin restreindre des dossiers à des groupes, avec les comptes locaux de Caddy.
- Ajouter un compte reste une ligne dans le fichier `users` de Caddy (ou se fait dans l'authentification unique) ; seuls ses groupes et son rôle se règlent dans imageBio.
- Deux sources de réglages : la page l'indique section par section (« fichier de configuration » ou « changé ici »), et « Revenir au fichier » ramène à une seule.
- Une surveillance du Live dont le dossier a été retiré n'est plus accessible à son utilisateur (son dossier est vérifié à chaque requête) ; son scanner continue jusqu'à l'arrêt.
- Rien n'a été essayé avec un vrai proxy et de vrais comptes : la pile Caddy des tests transmet un nom sans groupes, ce qui est justement le cas que les groupes d'imageBio couvrent.

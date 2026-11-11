# imageBio : évaluation et plan d'attaque

*Rédigé le 10 novembre 2026, à partir d'un audit multi-agents du code existant (vérifié de façon adversariale), de deux études techniques et de trois propositions d'architecture indépendantes. Les annexes détaillées sont dans [`audit/AUDIT.md`](audit/AUDIT.md) et [`research/`](research/).*

---

## 0. En bref

> **Mise à jour du 11 novembre 2026.** Le périmètre a été recentré sur le cryo et le suivi en direct : le cœur ImageJ (M5, M6) et le worker ImageJ sont retirés, il reste un export vers Fiji ([ADR 009](adr/009-perimetre-cryo-et-live.md)). Les points 1 à 3 ci-dessous décrivent le projet d'origine ; la feuille de route du §4 est à jour. **Tous les jalons retenus (M0 à M4, M7, M8) sont écrits et testés sur des données générées ; rien n'a encore tourné sur de vraies données.** La suite est un essai, décrit à la fin du §7.

1. **L'idée est bonne et la forme est la bonne.** On fait une application web servie depuis un serveur du labo, à côté des données (NAS et GPU). Les utilisateurs l'ouvrent dans un navigateur, sans rien installer. Elle propose trois modes : **Visionneuse/éditeur** (façon ImageJ), **Cryo** et **Live**.
2. **« Ultime » et « clone d'ImageJ » sont les deux pièges.** ImageJ représente plus de 25 ans de travail. Le projet est réaliste si « ImageJ » veut dire : les ~20 fonctions utilisées tous les jours, très bien faites, plus un pont vers le vrai Fiji (et un worker ImageJ côté serveur) pour tout le reste.
3. **Stack retenue :**
   - un seul backend **Python (FastAPI)** qui remplace les deux serveurs actuels (stdlib Python et Node/Express) ;
   - un seul frontend **React + TypeScript** avec un canevas **WebGL2** (contraste et LUT calculés sur le GPU du client) ;
   - **ImageJ/Fiji et Bio-Formats** dans un processus Java séparé (pyimagej), qui isole aussi la licence GPL.
4. **L'existant :** on garde les idées, l'UX et quelques centaines de lignes d'algorithmes ; on réécrit les serveurs. Avant tout, il faut des **correctifs de sécurité immédiats** : CryoViewer Live contient une **exécution de commande à distance sans authentification**.
5. **Modèle Claude :** **Fable 5.2** pour construire le projet, avec un effort `high`. **Ultracode** est à réserver aux phases qui en profitent : conception, gros jalons, revue adversariale avant fusion. Voir §7.

---

## 1. Mon avis sur le projet

**Ce qui est juste :**
- **Une seule application, sur le réseau, à côté des données.** Un film EER de 0,5 Go ou un tomogramme de 20 Go ne doit jamais traverser le réseau. Le navigateur ne reçoit que les pixels qu'il affiche, au zoom affiché.
- **Le mode Cryo et le mode Live sont la vraie valeur ajoutée.** Fiji ne les a pas. cryoSPARC Live est lourd et payant pour certains usages, et EPU ne fait pas le tri qualité. Un outil qui ouvre n'importe quoi, l'affiche correctement et permet de surveiller le microscope depuis son téléphone a une vraie utilité au quotidien.
- **Le code actuel a déjà cette forme.** CryoViewer est un serveur Python avec une UI HTML ; Live est un frontend React avec un surveillant de fichiers côté serveur. On unifie, on ne jette pas les idées.

**Ce qui est dangereux :**

| Risque | Réalité | Parade |
|---|---|---|
| « Supporte n'importe quel format » | ~25 lecteurs Python couvrent plus de 95 % des fichiers réels d'un labo. La longue traîne (~150 formats) passe par Bio-Formats, ce qui veut dire une JVM et du GPL. | Formats classés par paliers (§3.4). Un format n'est « supporté » qu'avec un fichier de test et un test de référence. |
| « Clone d'ImageJ » | Réimplémenter le langage macro IJM et l'API des plugins IJ1 ne finit jamais. | Liste figée du MVP (§4, M5-M6). Les vraies macros tournent dans le worker ImageJ. Le bouton « Ouvrir dans Fiji » exporte TIFF, calibration et ROI. Les algorithmes d'IJ1 (domaine public) sont portés et comparés à Fiji. |
| Licence GPL | ImageJ 1.x est dans le domaine public, ImageJ2/ImgLib2 est sous BSD-2, mais Bio-Formats et beaucoup de plugins Fiji sont GPL. Un usage sur le réseau interne n'est pas une distribution. | Le cœur reste **MIT**, comme CryoViewer. Le GPL est confiné dans `workers/imagej`, un processus séparé derrière une API étroite. On garde ainsi la possibilité de partager l'outil avec d'autres labos. |
| Fichiers cryo énormes | Sur un Falcon 4i : 0,4 à 0,9 Go par film, plus de 1000 fractions, jusqu'à ~300 Go/h. | Lecture en mmap ou en seek, cache borné en octets, binning côté serveur, somme EER calculée une seule fois puis mise en cache, Live qui lit d'abord les XML/JPG/en-têtes. |
| Sécurité réseau | Aujourd'hui, les deux applis sont des portes ouvertes (§2). | Authentification devant tout, racines de données autorisées par groupe, aucun chemin absolu venant du client, aucun shell. |
| Réécriture « big bang » | Le labo resterait des mois sans outil. | Remplacement progressif : on corrige l'existant, puis on remplace une surface à la fois. Une ancienne appli n'est éteinte que quand sa remplaçante passe ses critères d'acceptation. |

**Ordre de grandeur :** 30 à 45 sessions de travail assisté pour arriver à M8. Chaque jalon se termine par quelque chose d'utilisable sur le réseau.

---

## 2. État des lieux du code existant

Audit complet : [`audit/AUDIT.md`](audit/AUDIT.md). Il y a 43 constats. Chaque constat de gravité moyenne ou plus a été soumis à un vérificateur indépendant chargé de le réfuter : un seul a été réfuté (LIVE-10), et les vérificateurs ont trouvé 8 problèmes supplémentaires. Les 17 tests existants de CryoViewer passent.

### 2.1 Urgent : CryoViewer Live (si le service tourne actuellement sur le réseau)

**Exécution de commande à distance sans authentification** (`server.js:84`) :
- `updateDiskInfo()` exécute `` exec(`df -B1 "${watchVolumePath}"`) `` à travers un shell.
- `watchVolumePath` vient directement du motif de surveillance envoyé par n'importe quel client WebSocket. Il n'y a ni authentification ni vérification d'origine, et le serveur écoute sur `0.0.0.0`.
- Un motif contenant `$(…)` sur un chemin inexistant fait exécuter la commande sous l'identité de l'utilisateur du service systemd.
- J'ai relu ce code moi-même ; le vérificateur a confirmé que ce chemin d'exécution est atteignable.

**Lecture de fichiers arbitraires** (`server.js:528`) : `/api/image-data?path=…` et `/api/thumbnail?path=…` servent n'importe quel fichier lisible. Le vérificateur l'a reproduit.

> **À faire tout de suite, avant même M0 :** couper le service, ou le restreindre par pare-feu à `127.0.0.1` et y accéder par tunnel SSH. Ensuite, appliquer le correctif minimal : supprimer le repli `exec(df …)` et ne garder que `fs.statfs`, ou `execFile('df', ['-B1', '--', p])` sans shell.
>
> **Mise à jour (10 novembre 2026) :** corrigé dans `legacy/cryoviewer-live/` (voir l'avancement de M0, §4). Il reste à **déployer** la nouvelle version sur le serveur du labo.

### 2.2 CryoViewer : principaux problèmes confirmés

| Gravité | Problème | Où |
|---|---|---|
| critique | Serveur mono-thread avec keep-alive HTTP/1.1 : un deuxième onglet ou un deuxième utilisateur reste **bloqué** indéfiniment. Reproduit avec un vrai Chrome. | `server.py:1869` |
| haute | **EER affiché comme du bruit de Poisson synthétique**, avec 0,5 Å/px codé en dur et aucun avertissement dans l'UI. | `server.py:117` |
| haute | Fichiers en lien symbolique (dossiers `Movies/`, `Import/` de RELION) impossibles à afficher, alors qu'un message « Loaded » s'affiche. | `server.py:391` |
| haute | Le lecteur de frames et les curseurs envoient des requêtes sans limite. Les réponses arrivent dans le désordre, avec des dizaines de secondes d'arriéré à 4K. | `server.py:1665` |
| haute | Les champs de plage de moyenne ne servent à rien : c'est **toujours le film entier** qui est moyenné. | `server.py:859` |
| haute | Mémoire non bornée : cache de 24 frames quelle que soit leur taille (1,6 Go par film 4K), fichiers jamais fermés, TIFF entièrement convertis en float32. | `server.py:299` |
| haute | XSS stockée via les noms de fichiers ; aucune authentification ; n'importe quel chemin est lisible ; les tracebacks sont renvoyés au client. | `server.py:1297`, `:522` |
| moyenne | Taille de pixel : extensions d'en-tête FEI1/FEI2 ignorées (repli silencieux à 1,0 Å/px) ; métadonnées TIFF ImageJ lues comme 0,001 Å/px. | `:54`, `:150` |
| moyenne | Un seul pixel NaN rend l'image inaffichable ; un pixel chaud rend l'histogramme inutilisable ; la valeur affichée sous le curseur est l'octet d'affichage 0-255, pas la vraie valeur. | `:291`, `:1272` |
| moyenne | Pas d'ajustement à la fenêtre ; chaque réglage renvoie un PNG pleine résolution encodé en base64 dans du JSON. | `:1108`, `:274` |

**À garder** (porté et testé) :
- la lecture MRC frame par frame (boutisme et décalage NSYMBT corrects, modes 0/1/2/6/12 identiques à mrcfile) ;
- la définition du passe-bas en Å (gain mesuré de 0,516 à la coupure) ;
- le zoom centré sur le curseur et la règle Å/nm ;
- le navigateur de fichiers façon CryoSPARC, les préréglages de passe-bas et les raccourcis clavier ;
- les 17 tests.

**À réécrire :** le serveur HTTP, l'état global, le transport, l'interface et l'EER.

### 2.3 CryoViewer Live : principaux problèmes confirmés

| Gravité | Problème | Où |
|---|---|---|
| critique | Exécution de commande à distance (§2.1) et lecture de fichiers arbitraires ; CORS grand ouvert ; WebSocket qui accepte n'importe quelle origine. | `server.js:84`, `:528` |
| haute | Si l'écriture fait une pause, un **MRC partiel** est déclaré complet. Il n'y a pas de gestionnaire `change`, donc la taille et la vignette restent fausses. | `server.js:375` |
| haute | Vignettes et aperçus faits par **décimation au plus proche voisin** : sur une micrographie bruitée, le repliement spectral détruit le signal qu'on cherche à juger. | `server.js:139` |
| haute | **Règle fausse d'un facteur 2 à 4** sur les images de 2K et plus : l'aperçu est sous-échantillonné, mais le Å/px d'origine est conservé. | `server.js:239` |
| moyenne | Un seul surveillant global : le Start/Stop d'un utilisateur écrase la session des autres et efface le cache des vignettes. | `server.js:44` |
| moyenne | Parseur MRC en JS sans gestion du boutisme ni du mode 12 ; cache des vignettes sans mtime et non borné ; scrutation chokidar en O(fichiers), coûteuse à 50 000 fichiers sur NFS. | `:109`, `:187`, `:371` |

**À garder :**
- le concept et l'UX : barre de stats, balise colorée, taux d'acquisition, graphes ;
- la **grille virtualisée** `ImageGrid.jsx` (solide) ;
- le client WebSocket avec reconnexion progressive ;
- le schéma des vignettes : lecture partielle, cache disque, déduplication des requêtes en vol, sémaphore.

**À jeter :** tout le décodage d'image en JS, le surveillant global et chokidar. Le `dist/` livré correspond bien aux sources.

---

## 3. Architecture cible (décision)

### 3.1 Schéma

```
navigateur (React + TS, canevas WebGL2) ──HTTPS──▶ Caddy (TLS) ──▶ oauth2-proxy / Authelia ⇄ LDAP/AD
                                                     │
                                       imagebio (FastAPI / uvicorn), lié à 127.0.0.1
                                       ├─ lecteurs : mrcfile(+extension), tifffile+imagecodecs (TIFF/LZW/EER),
                                       │             Pillow, ncempy (DM4), puis nd2 / liffile / pylibCZIrw…
                                       ├─ service plans/tuiles : tableaux typés bruts + zstd, cache LRU en octets (RAM + SSD)
                                       ├─ hub WebSocket (deltas Live, progression des tâches)
                                       ├─ SQLite (index Live, métriques, vues sauvegardées) → Postgres si besoin
                                       └─ file de tâches (pool de processus → arq/Redis plus tard)
                                           ├─ worker CPU  : sommes EER, spectres de puissance, vignettes
                                           ├─ worker GPU  : MotionCor3, CTFFIND5 (un par carte)
                                           └─ worker ImageJ : pyimagej + Bio-Formats (JVM séparée = frontière GPL)
NAS ── montage NFS en lecture seule (/data, actimeo=1 pour Live) ; SSD local ── /cache ; /derived (rw par projet)
notifications : ntfy auto-hébergé (appli téléphone) + SMTP + webhook Mattermost/Slack
```

**Mode autonome obligatoire.** `imagebio serve --local` doit tourner sur un PC seul (microscope, portable), sans authentification et lié à 127.0.0.1. Les utilisateurs ne lâcheront pas le « double-clic sur le PC du microscope » que permet CryoViewer aujourd'hui.

### 3.2 Choix et alternatives rejetées

| Couche | Choix | Pourquoi |
|---|---|---|
| Modèle de données | `Dataset` paresseux à n dimensions (axes TCZYX, sémantique des hyperstacks ImageJ). `Calibration` : Å/px par axe, unités, et **provenance** de la valeur (en-tête, extension d'en-tête, XML, ou défaut avec avertissement visible). `DisplaySettings` (LUT, min/max par canal). `Overlay` (ROI). | C'est ce que signifie l'ImagePlus d'ImageJ, et chaque mode s'appuie dessus. |
| E/S | Protocole `Reader` (`sniff`, `open`), enregistré par priorité. Lecteurs Python rapides d'abord ; Bio-Formats seulement en dernier recours. | « Tous les formats » devient un mécanisme de repli, pas un objectif. |
| Transport | Le serveur envoie des **tableaux typés bruts** (uint8/16, float16/32) binnés au zoom affiché. Contraste, gamma et LUT sont calculés **dans le shader**. | Réglage du contraste sans aucun aller-retour serveur, et la vraie valeur du pixel est disponible sous le curseur. |
| Traitements | Registre d'opérations : fonctions pures `(ndarray, params, calib) → ndarray`, avec un schéma de paramètres pydantic qui génère le formulaire de l'UI, une implémentation CPU et une implémentation CuPy optionnelle. L'affichage est non destructif ; « Apply » valide la chaîne. | On obtient gratuitement l'annulation, l'historique, l'enregistreur de commandes (qui produit du Python) et le traitement par lots. |
| Modes | Cryo et Live sont des modules séparés du cœur (`imagebio.cryo`, `imagebio.live`, et `frontend/src/cryo`, `frontend/src/live`), avec des frontières nettes dès le premier jour. Un vrai chargeur de plugins viendra plus tard. | On prépare l'extensibilité sans construire un framework de plugins trop tôt. |
| Compatibilité ImageJ | Algorithmes IJ1 portés en Python et comparés à Fiji. `workers/imagej` (pyimagej, sans interface graphique ou sous Xvfb) pour les macros et plugins. Export « Ouvrir dans Fiji » : TIFF ImageJ via tifffile + `RoiSet.zip` via roifile. | Une vraie compatibilité, sans réimplémenter IJM. |

**Alternatives rejetées :**
- **Étendre Fiji (Java Swing + FlatLaf).** L'interface d'IJ1 est en AWT et ne se modernise pas. Il faudrait installer Fiji sur chaque PC et faire transiter les gros fichiers par SMB, et il n'y a pas de modèle multi-utilisateur.
- **Garder Node pour Live.** Ce serait un deuxième décodeur MRC, déjà faux, et un deuxième modèle de sécurité.
- **ImageJ.JS / CheerpJ.** La licence est propriétaire et on garde la vieille interface.
- **napari.** Excellent outil sur un poste, mais ce n'est pas une application réseau multi-utilisateur.
- **Electron/Tauri.** Coût de packaging sans résoudre le multi-utilisateur. La même UI React pourra être emballée plus tard si un mode hors ligne devient nécessaire.

### 3.3 Principes qui éliminent par construction les bugs de l'audit

Chaque principe ci-dessous a son test de non-régression.

1. **Aucun chemin absolu venant du client.** Les racines de données sont déclarées côté serveur (par groupe LDAP), résolues avec `realpath` et confinées. Un lien symbolique est suivi seulement s'il reste dans la racine, et un lien cassé ne casse pas le listing.
2. **Aucun appel au shell**, nulle part.
3. **Un seul décodeur**, en Python, comparé bit à bit à `mrcfile` et `tifffile`.
4. **Réduire = moyenner** (binning ou rognage dans Fourier), **jamais décimer**. Le Å/px est toujours recalculé au niveau affiché.
5. **Cache borné en octets**, avec une clé `(chemin, taille, mtime_ns, opération)`. Les poignées de fichiers sont fermées à l'éviction.
6. **Statistiques robustes** : percentiles 0,1 à 99,9, insensibles aux NaN et Inf. L'histogramme est calculé sur la plage robuste.
7. **La dernière requête gagne** : au plus une requête en vol par vue, avec annulation (`AbortController`) et numéro de séquence.
8. **État par utilisateur et par session, persistant** (SQLite) : jamais de singleton global.
9. **Un fichier n'est « complet » que si trois conditions sont vraies** : son XML partenaire EPU (ou la section mdoc SerialEM) existe, sa taille est stable sur deux scrutations espacées de 5 à 10 s, et il passe un contrôle d'intégrité (MRC : taille = 1024 + NSYMBT + nx·ny·nz·octets ; TIFF/EER : chaîne d'IFD complète).
10. **Toujours échapper la sortie** (aucun `innerHTML` avec des noms de fichiers), CSP stricte, contrôle Origin/Host, aucun traceback renvoyé au client.

### 3.4 Formats par paliers

| Palier | Formats | Bibliothèque |
|---|---|---|
| 1 (M1) | MRC/MRCS/MAP/REC/ST (tous les modes), **EER réel**, TIFF/BigTIFF/ImageJ-TIFF/OME-TIFF/K3 LZW, PNG/JPEG | `mrcfile` + extension maison (mode 101 avec remplissage, mode 16 RGB, drapeau uint8 d'IMOD, Å/px FEI1/FEI2, nz vérifié contre la taille), `tifffile` + `imagecodecs ≥ 2025.11.11`, Pillow |
| 2 (M6) | DM3/DM4, ND2, CZI, LIF, OIB/OIF, HDF5 (avec choix du dataset), STAR, `.cs`, mdoc | ncempy, nd2, pylibCZIrw, liffile, oiffile, h5py, starfile, mdocfile, `np.load` |
| 3 (M7) | Longue traîne (~150 formats : VSI, IMS, etc.) | Bio-Formats dans `workers/imagej`, résultat mis en cache en OME-Zarr |

**EER (vérifié pendant l'étude) :**
- `tifffile` + `imagecodecs` décodent l'EER nativement, à ~5 ms par fraction 4K.
- Une somme de 1000 fractions prend 6 à 8 s sur un cœur ; on la calcule dans un pool de processus et on la met en cache.
- Il faut appliquer le gain `.gain` (multiplicatif, sans retournement avec tifffile) et les pixels défectueux (gain à 0).
- Les fractions sont regroupées à environ 1 e/Å² par groupe.
- L'orientation du gain est vérifiée automatiquement sur le premier film de la session : on essaie les 8 rotations et symétries et on garde celle qui donne la variance minimale.

Détails dans [`research/cryoem-live.md`](research/cryoem-live.md).

---

## 4. Feuille de route

Chaque jalon dure de 1 à 7 sessions. Chaque session se termine avec la CI au vert et une version déployable. M2 et M3 peuvent être inversés si Live est la douleur la plus forte au quotidien.

### M0 : urgences et socle (1-2 sessions)

**Livrables :**
- Archives décompressées dans `legacy/cryoviewer/` et `legacy/cryoviewer-live/` (sans `node_modules/` ni `dist/`), puis supprimées de l'arborescence (l'historique git les garde).
- `.gitignore`, CI GitHub Actions (pytest, build Vite, tests de sécurité).
- **Correctifs dans Live :**
  - supprimer l'exécution de commande `exec(df)` ;
  - confiner les chemins à `CVL_ROOTS` ;
  - écouter sur 127.0.0.1 par défaut ;
  - service lancé par un utilisateur dédié ;
  - clé du cache des vignettes incluant mtime et taille ;
  - contrôle taille = taille attendue avant d'émettre un fichier ;
  - erreur HTTP (et non 200) sur un MRC tronqué ou invalide.
- **Correctifs dans CryoViewer :**
  - `ThreadingHTTPServer` avec délai d'expiration des sockets ;
  - échappement XSS et délégation d'événements ;
  - **vrai décodage EER** via tifffile + imagecodecs, ou refus explicite ;
  - liens symboliques (y compris les liens cassés) ;
  - plage de moyenne prise en compte ;
  - statistiques insensibles aux NaN avec plage robuste ;
  - lecteur de frames « la dernière requête gagne » ;
  - options `--root` et `--bind` ;
  - taille d'envoi plafonnée, fichiers temporaires nettoyés, contrôle Origin sur POST ;
  - polices hébergées localement.
- `deploy/Caddyfile` (TLS + authentification basique ou LDAP) devant les deux applis existantes.

**Acceptation :**
- Les 17 tests existants passent, plus un test de non-régression par correctif.
- Le motif `"/x/$(touch /tmp/pwned)/a.mrc"` n'exécute rien.
- `GET /api/thumbnail?path=/etc/passwd` renvoie 403.
- Deux onglets fonctionnent en même temps sur CryoViewer.
- Un fichier nommé `<img src=x onerror=alert(1)>.mrc` s'affiche comme du texte.
- Le fichier EER synthétique s'affiche avec la vraie somme des événements.

**Avancement de M0 (10 novembre 2026) : fait, sauf les points listés plus bas.**

Fait :
- Sources décompressées dans `legacy/` (archives retirées, historique git conservé), `.gitignore`, CI GitHub Actions (`.github/workflows/ci.yml` : pytest sur Python 3.11 et 3.13, `npm ci` + tests + build pour Live).
- **Live** (`security.js`, `server.js`, 17 tests `npm test`) :
  - plus aucun appel au shell ; l'injection a été rejouée contre l'ancien serveur (commande exécutée) et contre le nouveau (refusée) ;
  - racines de données `CVL_ROOTS` ; motifs confinés ; fichiers servis seulement s'ils appartiennent à la surveillance en cours et restent dans une racine (liens symboliques compris) ;
  - écoute sur `127.0.0.1` par défaut ; refus de démarrer sur le réseau sans authentification (Basic auth intégrée en option) ;
  - contrôle du `Host` (DNS rebinding) et de l'`Origin` du WebSocket, compatible avec un reverse proxy (`CVL_ALLOWED_HOSTS`) ;
  - MRC invalide, tronqué ou non pris en charge → HTTP 422 ; cache de vignettes indexé sur taille et mtime, et borné en octets ; nouvelle surveillance limitée à une toutes les 2 s ;
  - une revue de sécurité indépendante a été faite ; ses points ont été corrigés ou documentés comme limites connues dans le README de Live.
- **CryoViewer** (`server.py`, 35 tests `pytest`, vérification dans Chromium avec Playwright) :
  - serveur multi-thread avec délai d'expiration et verrou par fichier ;
  - options `--bind`, `--root`, `--auth-file`, `--allowed-host` ; refus d'écouter sur le réseau sans racines ni authentification ; contrôle du `Host` ; plus de traceback renvoyé au client ;
  - **vrai décodage EER** (somme des fractions par groupes, 50 frames au plus), comparé à la vérité terrain d'un EER synthétique ;
  - liens symboliques (fichiers RELION) affichables ; un lien cassé ne casse plus la liste ; les liens qui sortent des racines sont masqués et refusés ;
  - plage de moyenne réellement utilisée (1-based, inclusive) ; les deux boutons Avg sont synchronisés ;
  - images avec NaN/Inf affichables ; histogramme et curseurs sur une plage robuste (0,1-99,9 %) ;
  - « la dernière requête gagne » et le lecteur attend chaque frame (au plus une requête en vol, mesuré) ; une erreur de chargement n'est plus masquée par « Loaded » ;
  - plus d'`innerHTML` avec des noms de fichiers (XSS) ; la liste « Opened » n'est plus effacée ; raccourcis clavier actifs après usage d'un curseur ;
  - upload en flux vers un dossier temporaire, plafonné, nettoyé, avec contrôle d'origine ; cache de frames borné en octets, 8 fichiers ouverts au plus ;
  - taille de pixel inconnue signalée (« unknown ») ; polices système (plus d'appel à Google Fonts) ; le chemin passé en ligne de commande est bien ouvert.

Reste à faire (reporté) :
- Live : la taille d'un fichier listé alors qu'il était encore en cours d'écriture n'est pas mise à jour (pas de gestionnaire `change`), et il compte dans le taux dès sa première apparition. La vignette et l'aperçu, eux, se régénèrent (422 tant que le fichier est incomplet). Corrigé par conception en M3.
- Live : faire tourner le service sous un compte dédié, et choisir l'option de déploiement (proxy ou Basic auth). C'est à faire sur le serveur, voir le README de Live.
- CryoViewer : défauts de lecture laissés pour M1 (taille de pixel TIFF et extensions FEI, modes 101/16, drapeau IMOD, TIFF palette, LA et hyperstacks) et transport PNG/base64 laissé pour M2 (la valeur sous le curseur reste l'octet affiché).
- `deploy/Caddyfile` : remplacé pour l'instant par les exemples Caddy et nginx du README de Live.

### M1 : cœur du backend `imagebio` (3-4 sessions)

**Livrables :**
- Paquet `backend/` (uv, ruff, mypy, pytest) ; versions épinglées.
- Générateur de fichiers de test déterministe (§6).
- `Dataset`, `Calibration` avec provenance, registre de lecteurs, lecteurs du palier 1, cache LRU en octets.
- `roots.py` : racines par groupe, confinement, liens symboliques sûrs.
- API FastAPI :
  - `GET /api/fs` (navigation confinée) ;
  - `GET /api/datasets/{id}` (métadonnées) ;
  - `GET /api/datasets/{id}/plane?t&z&c&bin` (binaire zstd, dtype et forme dans les en-têtes) ;
  - `GET /api/datasets/{id}/stats` (histogramme robuste) ;
  - `/healthz`.
- Erreurs génériques en JSON.
- Authentification lue dans les en-têtes posés par le proxy.

**Acceptation :**
- Lecteurs identiques bit à bit à `mrcfile` et `tifffile`.
- Somme EER identique à la vérité synthétique.
- Mémoire (RSS) sous le plafond du cache en parcourant une pile de 2 Go.
- Plan 4K avec binning 4 servi en moins de 150 ms à chaud.
- Un chemin hors racine renvoie 403.
- 10 clients simultanés sans dégradation.

**Avancement de M1 (10 novembre 2026) : fait.** Le code est dans `backend/` ([README](../backend/README.md)) ; les décisions sont dans [`adr/`](adr/).

Fait (753 tests, `ruff` et `mypy --strict` propres) :
- **Socle** : `Dataset` paresseux à 5 axes lu plan par plan, calibration avec provenance, erreurs publiques sans chemin serveur, configuration TOML, registre de lecteurs (extension + contenu, points d'entrée pour des lecteurs tiers).
- **Lecteurs du palier 1** : MRC (modes 0, 1, 2, 6, 12, 16, 101 ; extension FEI1/FEI2 ; fichiers tronqués ou en cours d'écriture), EER réel (groupes de fractions, super-résolution, flux tronqués détectés), TIFF (hyperstacks ImageJ, OME, films compressés, RGB, palettes), PNG/JPEG/BMP/GIF/WebP. Chaque lecteur est comparé bit à bit à `mrcfile`, `tifffile` ou Pillow, et soumis à des fichiers abîmés.
- **Traitements** : binning par moyenne de blocs, statistiques robustes insensibles aux NaN.
- **Caches** : plans bornés en octets avec chargement unique par clé ; pool de fichiers ouverts indexé sur l'identité du contenu.
- **Racines** : chemins logiques, confinement par `realpath`, droits par groupe, liens sortants refusés et masqués.
- **API FastAPI** et ligne de commande (`imagebio serve`, `imagebio info`).

Critères d'acceptation :

| Critère | Résultat |
|---|---|
| Lecteurs identiques bit à bit à `mrcfile` et `tifffile` | Oui (tests de référence par mode, boutisme et variante) |
| Somme EER identique à la vérité synthétique | Oui, y compris par groupes et en super-résolution |
| Mémoire sous le plafond du cache en parcourant une pile | Oui : pile de 240 Mio, cache de 32 Mio, +12 Mio de mémoire résidente. Le test à 2 Go n'a pas été fait, le mécanisme est le même |
| Plan 4K avec binning 4 en moins de 150 ms à chaud | Oui : 5 ms à chaud, 85 ms à froid |
| Un chemin hors racine renvoie 403 | Oui, sans révéler si le fichier existe |
| 10 clients simultanés | Oui : données exactes et cache borné sous 10 threads |

Écarts par rapport au plan :
- **Pas de relecture indépendante.** Le workflow multi-agents prévu a été coupé par la limite d'utilisation ; le lecteur MRC et une bonne partie des lecteurs TIFF et EER viennent de ce workflow, le reste a été écrit directement. Aucun module n'a eu de relecture adversariale par un agent neuf. C'est la première chose à faire quand le budget le permet, en priorité sur `fs/roots.py`, `api/` et les lecteurs.
- **L'API est sans état** : les fichiers sont désignés par leur chemin logique à chaque requête (`/api/datasets/plane?path=…`), et non par un identifiant de session comme écrit plus haut. Voir l'ADR 003.
- **Pas de comptes ni de LDAP dans le backend** : l'identité vient du reverse proxy (ADR 005). Le mode autonome `--local` existe déjà.
- **EER** : le gain, les pixels défectueux et le pool de processus sont reportés à M4. Le décodage utilise un petit pool de threads.
- **Non vérifié sur de vrais fichiers** : le mode MRC 101 et l'ancien format 4 bits de SerialEM ne sont testés que sur des fichiers construits d'après la spécification. Des fichiers réels d'EPU, de SerialEM et un EER de Falcon seraient précieux.

### M2 : visionneuse web, qui remplace CryoViewer (3-4 sessions)

**Livrables :**
- Application React + TS avec le canevas WebGL2 (textures R16UI/R32F, LUT en texture 1D, découpage en tuiles au-delà de 8K).
- Ajustement à la fenêtre, zoom centré sur le curseur, vraie valeur du pixel sous le curseur.
- Contraste, gamma et LUT sur le GPU (Grays, Fire, Ice, HiLo, viridis, import de `.lut`), histogramme en échelle log sur la plage robuste.
- Curseurs T/Z/C et lecteur de frames avec annulation.
- Règle et barre d'échelle en Å/nm dessinées en espace écran.
- Navigateur de fichiers façon CryoSPARC.
- Raccourcis de CryoViewer et d'ImageJ.
- **Liens profonds** : l'URL encode le fichier et l'état de la vue, à coller dans Slack ou un cahier de labo électronique.
- Mode `--local`.

**Acceptation (Playwright) :**
- La valeur lue sous le curseur est égale à celle du fichier de test.
- Bouger le curseur de contraste ne déclenche aucune requête de plan.
- Lecture de frames : au plus une requête en vol.
- Les images avec NaN et pixel chaud s'affichent.
- Un lien partagé reproduit la même vue.
- **L'ancien CryoViewer est éteint.**

**Avancement de M2 (10 novembre 2026) : fait, sauf le retrait de l'ancien CryoViewer.** Le code est dans `frontend/` ([README](../frontend/README.md)).

Fait (27 tests unitaires, 10 tests de bout en bout sur le vrai backend) :
- Canevas WebGL2 : valeurs brutes en textures flottantes ; fenêtre, gamma, inversion et palette dans le shader. Les NaN ont leur propre couleur.
- Deux niveaux de détail au lieu d'un découpage en tuiles : vue d'ensemble binnée, puis région visible à la résolution utile. La taille des textures dépend de l'écran, pas de l'image.
- Ajustement à la fenêtre, zoom centré sur le curseur, vraie valeur sous le curseur.
- Histogramme en échelle log sur la plage robuste ; palettes Grays, Fire, Ice, Viridis, HiLo et import de `.lut`.
- Curseurs T, Z et C ; lecteur qui attend l'affichage de chaque plan.
- Règle et barre d'échelle en espace écran ; taille de pixel avec provenance, saisie manuelle si elle est inconnue.
- Navigateur de fichiers sur les chemins logiques ; raccourcis clavier.
- Lien partageable : toute la vue est dans l'adresse.
- Le backend sert l'interface construite : `imagebio serve --local DOSSIER` donne l'application complète.

Critères d'acceptation :

| Critère | Résultat |
|---|---|
| La valeur lue sous le curseur est celle du fichier | Oui, à pleine résolution ; en vue binnée, la valeur est la moyenne du bloc et c'est indiqué |
| Bouger le curseur de contraste ne déclenche aucune requête de plan | Oui (ni de statistiques) |
| Lecture de frames : au plus une requête en vol | Oui, y compris en maintenant la flèche |
| Les images avec NaN et pixel chaud s'affichent | Oui |
| Un lien partagé reproduit la même vue | Oui, vérifié pixel par pixel dans un second onglet |
| L'ancien CryoViewer est éteint | **Non.** Il restait nécessaire pour le filtre passe-bas en Å et la moyenne de frames ; ces deux fonctions sont arrivées avec M4, il peut être éteint après un essai |

Écarts et limites :
- Images RGB : affichage composite avec une fenêtre commune aux trois canaux. Les hyperstacks montrent un canal à la fois ; le composite multi-canal est prévu en M5.
- Les options de lecture (regroupement EER) passent par l'adresse (`opt.group=40`) ; le contrôle dans l'interface est arrivé avec M4.
- Testé sous Chromium, à densité d'écran 1 et 2. Pas encore essayé sous Firefox ni Safari.
- Une image très allongée (plus de 16 000 pixels sur un côté après binning) peut dépasser la taille de texture de certaines cartes graphiques ; un message le signale.

### M3 : Live v2 sur le backend Python, qui remplace le serveur Node (4-5 sessions)

**Livrables :**
- Scrutateur indexé dans SQLite : on ne rescanne que les dossiers dont le mtime a changé (`GridSquare_*/Data` actifs), avec un rescan complet toutes les 5 à 10 min.
- Test de complétude (§3.3, principe 9).
- Parseurs EPU XML (XPaths dans [`research/cryoem-live.md`](research/cryoem-live.md) §2) et mdoc SerialEM/Tomo5.
- **Une surveillance par session et par utilisateur**, avec reprise après redémarrage.
- Métriques peu coûteuses par image :
  - dose, défocus visé, horodatage, taux et trous d'acquisition ;
  - comptes moyens et épaisseur de glace par diffusion limitée par l'ouverture (ALS) ;
  - indicateur de trou vide ;
  - spectre de puissance rapide et rapport de l'anneau de glace ;
  - dérive estimée sur les deux demi-sommes.
- Vignettes correctement binnées. WebSocket qui n'envoie que les deltas.
- UI Live existante portée : `ImageGrid` virtualisée, StatsBar, RateGraph. Un clic sur une vignette ouvre l'image dans la visionneuse M2.
- Alertes ntfy/webhook/e-mail : plus d'image depuis X min, disque sous Y %, chute du taux, Live lui-même arrêté.

**Acceptation :**
- `tools/microscope_sim.py` rejoue une session de type EPU à 600 images/h avec écritures partielles et pauses : aucun fichier partiel n'est jamais marqué complet.
- 50 000 fichiers sont indexés sans scruter chaque fichier individuellement.
- Après un arrêt brutal du serveur, rien n'est perdu.
- Deux utilisateurs sur deux sessions ne se gênent pas.
- Les alertes partent en moins de 2 cycles de scrutation.
- **Le serveur Node est éteint.**

**Avancement de M3 (11 novembre 2026) : fait, sauf l'essai sur une vraie session.** Cinq critères d'acceptation sur six passent ; le sixième (éteindre le serveur Node) attend cet essai. La conception est dans l'[ADR 007](adr/007-live.md).

Fait (222 tests côté backend pour le Live, 30 tests unitaires et 6 tests de bout en bout côté interface) :
- **Scrutateur indexé dans SQLite** (`live/scanner.py`, `live/index.py`) : un dossier n'est relu que si sa date a changé ; seuls les fichiers pas encore complets sont suivis un par un ; passage complet toutes les 10 minutes ; un passage est une transaction.
- **Test de complétude** (`live/completeness.py`) : taille stable sur deux passages espacés de 10 s, contrôle d'intégrité du format, et `.xml` d'EPU présent pour une acquisition EPU.
- **Parseurs** EPU XML (`cryo/epu.py`) et mdoc SerialEM/Tomo5 (`cryo/mdoc.py`) : taille de pixel, dose, défocus visé, horodatage, position de platine, référence de gain EER.
- **Expositions** : l'image intégrée, son film, son aperçu et ses métadonnées forment une seule tuile ; les vues d'ensemble d'EPU (carrés, trous, atlas) sont classées à part.
- **Une surveillance par utilisateur**, conservée dans l'index ; un scrutateur par dossier, partagé par ceux qui le suivent. Reprise au redémarrage. Une surveillance arrêtée garde son index et peut être oubliée ; chacun garde ses 32 dernières surveillances arrêtées, et l'index d'un dossier que plus personne ne suit est effacé.
- **Vignettes** binnées par moyenne de blocs, étirées entre percentiles robustes, gardées en mémoire et dans l'index.
- **API** `/api/live/…` : surveillances, résumé (compteurs, débit des arrivées, dernière image, disque, état du scrutateur), changements depuis un numéro de séquence, acquisitions par minute, vignettes.
- **Interface** : mode Live à côté de la visionneuse, choix du dossier, cartes d'état, graphe des acquisitions, **grille virtualisée** ; une exposition en cours d'écriture est montrée comme telle, sans vignette ; un clic ouvre l'image dans la visionneuse.
- **Microscope simulé** (`live/simulator.py`, `imagebio simulate DOSSIER`) : session de type EPU avec films écrits image par image, pauses au milieu des fichiers, XML en dernier.
- **Alertes** (`live/alerts.py`) : par surveillance, plus d'image depuis N minutes, espace libre sous un pourcentage, débit en chute, et scrutateur en échec. Envoi vers ntfy, un webhook ou un relais de messagerie. Les canaux sont déclarés par l'administrateur ; l'utilisateur les choisit par leur nom et ne saisit jamais d'adresse. Un message quand la condition commence, un quand elle cesse, rien entre les deux. L'envoi est suivi canal par canal : un canal en panne est retenté seul, cinq fois au plus, et le message de fin va aux canaux qui ont reçu celui du début. Une valeur qui oscille autour de son seuil ne déclenche pas un message à chaque passage : il faut une marge et deux minutes pour quitter une condition. Les alertes d'une surveillance cessent une semaine après la dernière visite de son utilisateur. Panneau dans l'interface, avec un bouton d'essai.

Critères d'acceptation :

| Critère | Résultat |
|---|---|
| Session de type EPU à 600 images/h avec écritures partielles et pauses : aucun fichier partiel marqué complet | Oui. 30 minutes simulées, 300 expositions, 30 % des films en pause de 15 à 45 s en cours d'écriture, puis arrêt brutal du microscope : aucun fichier partiel n'est jamais complet dans l'index, et tout fichier fini l'est 10 à 27 s après son dernier octet |
| 50 000 fichiers indexés sans scruter chaque fichier | Oui. 50 100 fichiers d'une session terminée depuis deux jours : un listing par dossier et un `stat` par dossier ; seuls les fichiers du dossier encore récent sont regardés un par un. 0,9 s sur disque local, puis 10 ms par passage. Depuis la relecture, les dossiers modifiés depuis moins d'un jour coûtent un `stat` par fichier à la première indexation, une seule fois : c'est le prix pour ne jamais prendre un fichier en cours d'écriture pour un fichier ancien |
| Après un arrêt brutal du serveur, rien n'est perdu | Oui : l'index est tel qu'avant, y compris si l'arrêt survient au milieu d'un passage ; ce qui s'est passé pendant l'arrêt est retrouvé, sans doublon |
| Deux utilisateurs sur deux sessions ne se gênent pas | Oui : chacun ne voit que ses surveillances ; arrêter la sienne ne touche pas celle de l'autre, même sur le même dossier |
| Les alertes partent en moins de 2 cycles de scrutation | Oui : les règles sont jugées juste après chaque passage. Avec le microscope et l'horloge simulés, le message part au plus 10 s après le franchissement du seuil, et le message « résolu » au plus 10 s après la première image acceptée |
| Le serveur Node est éteint | **Pas encore.** L'interface couvre la grille, l'état, le débit et les alertes ; à éteindre après un essai sur une vraie session |

- **Mesures sur chaque image** (`live/metrics.py`) : coups moyens et écart-type sans les pixels chauds, et **rapport de l'anneau de glace** (puissance entre 3,40 et 3,95 Å rapportée aux bandes voisines, sur le périodogramme moyenné du champ central). Elles sont faites après chaque passage, dans un budget de temps, et arrivent à l'interface comme n'importe quel changement. L'interface affiche sur chaque tuile la **glace relative** ln(I₀/I), où I₀ est le niveau des images les plus claires de la session, marque la glace cristalline (rapport au-dessus de 1,2) et les images dix fois plus sombres que les autres, et résume les 50 dernières images dans une carte. Environ 0,3 s par image 4K.

- **Épaisseur de glace étalonnée** : chaque surveillance peut recevoir les coups moyens d'un trou vide (I₀) et un libre parcours moyen en nm (le serveur peut en porter un par défaut, `[live] mean_free_path_nm`). L'épaisseur est alors donnée en nanomètres, et une image aussi claire que le trou est marquée « trou vide ». Sans I₀, la glace reste relative aux images les plus claires de la session et rien n'est appelé trou vide.
- **Tri et filtres de la grille** : par heure, glace, anneau de glace, défocus, dose ou dérive ; images marquées ou sans marque ; et les **vues d'ensemble d'EPU** (trous, carrés, atlas) dans la même grille.
- **Dérive** (`[live] drift = true`, désactivée par défaut) : déplacement du spécimen entre la moyenne des quatre premières frames du film et celle des quatre dernières, par corrélation croisée. Le motif fixe du détecteur, qui tirerait la mesure vers zéro, est retiré ; sans rien à suivre, aucune valeur n'est donnée. Précision de quelques dixièmes de pixel binné sur des spécimens synthétiques : c'est un regard grossier sur le mouvement d'ensemble, pas une correction de mouvement.

Reste à faire dans M3 :
- **L'essai sur une vraie session**, puis l'extinction du serveur Node.
- Visibilité des anneaux de Thon, saturation ; carte d'un carré avec les trous acquis ; graphes cumulés. Ces points iront mieux avec M7, qui apporte le CTF.

Écarts par rapport au plan :
- **Pas de WebSocket** : l'interface demande toutes les trois secondes ce qui a changé depuis le dernier numéro de séquence qu'elle connaît. Ce sont les mêmes deltas, sans connexion longue à faire passer par le reverse proxy (ADR 007).
- **Le simulateur est dans le paquet** (`imagebio simulate`), pas dans `tools/microscope_sim.py` : il sert aussi de démonstration.
- **Première indexation d'une session ancienne** : les dossiers inchangés depuis plus d'un jour sont indexés sans regarder leurs fichiers ; leurs expositions sont marquées « non vérifié » dans la grille. Un fichier resté tronqué n'y est découvert qu'à l'ouverture.
- **Le repos se mesure sur l'horloge du serveur de fichiers** (la date la plus récente de l'arborescence), pas sur celle de ce serveur : elles peuvent différer.
- **Vignette d'un film seul** (EER sans image intégrée) : moyenne de ses quatre premières frames, calculée à la demande. Pas de somme complète en tâche de fond.
- **Non essayé sur une vraie session.** Les XML d'EPU, les mdoc et l'arborescence viennent de la documentation ; un essai sur un dossier de session réel (même terminé) est le premier à faire. Le comportement sur NFS ou SMB (cache d'attributs, dates des dossiers) n'a pas été mesuré : tout a tourné sur disque local.
- **Alertes non essayées contre de vrais services.** L'envoi vers ntfy suit sa forme JSON documentée et a été vérifié contre un récepteur local, pas contre ntfy.sh ; l'e-mail contre un faux relais. Le bouton « Tester » du panneau est fait pour cette vérification, une fois le canal configuré. Pas d'alerte « le Live lui-même est arrêté » : elle demande un témoin extérieur au serveur.
- **Le message d'alerte part chez un tiers** quand le canal est un service public (ntfy.sh) : il contient le chemin logique de la session et le nombre d'images. C'est l'administrateur qui choisit les canaux.
- **La dérive lit des frames** : huit par film (quelques centaines de Mo pour un film 4K), d'où l'option. Elle suppose un spécimen qui se voit à basse résolution et n'a pas été confrontée à de vrais films ; sur des films EER très peu dosés par frame, elle peut ne rien donner.
- **Mesures limitées pour une session ouverte après coup** : chaque mesure lit l'image en entier. Toutes les images qui arrivent pendant la surveillance sont mesurées ; d'une session trouvée déjà écrite, seulement les 200 plus récentes. `[live] metrics = false` coupe les mesures.
- **Le rapport de l'anneau de glace n'est donné que si le champ fait au moins 512 pixels de côté et le pixel au plus 1,5 Å.** En dessous, les bandes contiennent trop peu de points du spectre : sur du bruit pur, le rapport varie de 20 % sur une image de 64 pixels, et une image sur cinq passait pour de la glace cristalline (trouvé par un test de bout en bout). Le seuil de 1,2 vient des usages de cryoSPARC et n'a pas été confronté à de vraies images.
- **Débit en chute** : la règle ne s'applique qu'au-dessus de 60 images dans l'heure. En dessous, cinq minutes contiennent trop peu d'images pour distinguer une chute du hasard ; la règle « plus d'image depuis N minutes » est celle des sessions lentes.
- **Relecture indépendante du Live** (un agent neuf, sur le scrutateur, l'index, le service, les alertes et les routes). Le test de complétude au fil de l'eau, les contrôles d'intégrité, le cloisonnement entre utilisateurs, l'absence d'adresse ou de chemin serveur dans les réponses, les requêtes SQL, les numéros de séquence et la migration de l'index ont été vérifiés et trouvés corrects. Treize défauts ont été trouvés et corrigés, chacun avec un test de régression qui échoue sur l'ancien code :
  - **Grave : un lien symbolique mis à la place d'un dossier déjà indexé était suivi**, ce qui faisait indexer, et donc lire par vignette, un dossier hors de la racine. Chaque dossier est maintenant revérifié à chaque listing.
  - Première indexation : un fichier en cours d'écriture dans un dossier calme, ou à côté d'un fichier daté dans le futur, pouvait être marqué complet sans contrôle. Seuls les dossiers inchangés depuis un jour sont indexés sans `stat`, et les dates futures sont ignorées.
  - Un passage en échec remettait le scrutateur en mode « première indexation » ; une erreur de lecture passagère pendant un passage complet effaçait un sous-arbre de l'index.
  - Un passage était une seule longue transaction : pendant l'indexation d'une très grande session, les autres écritures attendaient trente secondes puis échouaient en erreur 500. Le parcours se fait hors transaction, l'indexation est validée par tranches, et une requête qui trouve l'index occupé reçoit un 503 avec `Retry-After`.
  - Alertes : avec un canal en panne, les autres recevaient le message à chacune des cinq tentatives et jamais le message de fin ; un débit ou un disque proche du seuil envoyait des messages à chaque passage ; une heure après l'arrêt du microscope, « le débit est revenu » partait à tort ; une surveillance relancée annonçait aussitôt des heures de silence ; les règles continuaient indéfiniment pour un utilisateur parti ou privé d'accès ; la limite du bouton d'essai se contournait avec plusieurs surveillances.
  - Arrêter puis relancer une surveillance pendant un passage laissait deux scrutateurs sur le même dossier.
  - Le contrôle d'origine des requêtes qui modifient ne comparait que le nom d'hôte : un autre programme de la même machine, sur un autre port, passait. Il compare maintenant l'hôte, le port et, quand il est connu, le schéma.
  - Un identifiant au-delà de 64 bits donnait une erreur 500.
  - Caches de vignettes : celui de l'index dépassait plusieurs fois sa limite, et celui en mémoire comptait deux fois une vignette demandée deux fois en même temps.
  - Rien ne sortait jamais de l'index : surveillances arrêtées et dossiers s'y accumulaient ; le dossier le plus récent était relu à chaque passage pour toujours.

  Le frontend du Live n'a pas eu de relecture indépendante, ni les modules de M1 et M2.

### M4 : mode Cryo (3-5 sessions)

**Livrables :**
- EER complet : gain, défauts, regroupement par dose, vérification de l'orientation du gain.
- Spectre de puissance (périodogramme sur tuiles de 512) avec **anneaux de résolution en Å** et anneau de glace à 3,7 Å, moyenne radiale.
- Filtres passe-bas, passe-haut et passe-bande en Å (avec bordure : remplissage ou apodisation), binning dans Fourier.
- Moyenne de frames sur une plage, somme pondérée par la dose (plus tard, avec frames alignées).
- Superposition des pointés STAR, `.cs`, `.box` et `.cbox`.
- Galerie paresseuse de particules et de classes 2D (`.mrcs`, `_model.star` de RELION, `.cs` de cryoSPARC).

**Acceptation :**
- Un EER synthétique avec gain volontairement retourné est détecté.
- Rayon d'anneau = N·apix/d à 0,5 px près.
- Gain du passe-bas de 20 Å = 0,5 ± 0,03.
- La moyenne des frames 3 à 7 est égale à la moyenne numpy de ces frames.

**Avancement de M4 (11 novembre 2026) : fait, sauf la somme pondérée par la dose.** Les quatre critères d'acceptation passent. Les décisions de conception sont dans l'[ADR 006](adr/006-traitements-cote-serveur.md) et l'[ADR 008](adr/008-pointes-et-galeries.md).

Fait (1148 tests côté backend en tout, dont 202 pour le mode Cryo ; 86 tests unitaires et 28 tests de bout en bout côté interface, tous modes confondus) :
- **Filtres en Å** (`proc/fourier.py`) : passe-bas, passe-haut et passe-bande gaussiens, gain ½ à la coupure. L'image est prolongée en miroir avant la transformée, ce qui corrige le mélange des bords de l'ancien CryoViewer (CV-18). Le passe-haut conserve la moyenne ; les pixels non carrés sont pris en compte ; les NaN et Inf sont remis en place après le filtre.
- **Moyenne de frames** sur une plage, accumulée en `float64`.
- **Spectre de puissance** : périodogramme moyenné sur des tuiles de 512 pixels, moyenne radiale. Dans l'interface : anneaux de résolution en Å, anneau de la glace à 3,7 Å, résolution sous le curseur, agrandissement du centre.
- **Gain** (`cryo/gain.py`) : référence multiplicative ou image de coups à inverser, pixels défectueux comblés par la médiane de leurs voisins valides, référence plus fine ou plus grossière que l'image d'un facteur entier, **vérification des huit orientations** sur le film.
- **Chaîne de traitement côté serveur**, dans un ordre fixe : moyenne, gain, filtres, puis découpe et binning. Le résultat en pleine résolution est mis en cache ; la clé contient tous les paramètres et l'identité du fichier de gain.
- **Panneau Cryo** : préréglages de passe-bas, passe-haut, moyenne de frames, fractions EER par frame (à la main, ou calculées pour environ 1 e/Å² par frame à partir de la dose du film et de la taille de pixel), choix du gain parmi les fichiers voisins avec correction de l'orientation en un clic. Tous les réglages sont dans le lien.
- **Valeur sous le curseur** : elle suit maintenant le plan affiché même quand la souris ne bouge pas (filtre, moyenne, lecteur). Elle restait auparavant celle du plan précédent.

Critères d'acceptation :

| Critère | Résultat |
|---|---|
| Un EER synthétique avec gain volontairement retourné est détecté | Oui. Un film EER synthétique issu d'un détecteur à motif fixe, avec son gain enregistré tête en bas : l'API répond `flip_y`, et l'interface propose la correction. Les huit orientations sont retrouvées sur un détecteur synthétique |
| Rayon d'anneau = N·apix/d à 0,5 px près | Oui : un réseau de période 10 Å à 1,25 Å/px donne un pic à 64 px du centre d'un spectre de 512 px |
| Gain du passe-bas de 20 Å = 0,5 ± 0,03 | Oui, mesuré sur des sinusoïdes à la fréquence de coupure |
| La moyenne des frames 3 à 7 est égale à la moyenne numpy | Oui, à l'identique |

Temps mesurés sur une image 4096 × 4096 (un cœur) : passe-bas 0,7 s, spectre de puissance 0,6 s, application du gain avec 9 000 pixels défectueux 0,06 s, vérification de l'orientation 1,7 s (faite une fois par couple film et gain).

- **Pointés** (`cryo/picks.py`, `cryo/star.py`) : les coordonnées de particules des fichiers STAR de RELION (un fichier par micrographie, ou le `particles.star` d'un jeu entier, filtré sur l'image affichée), `.box` d'EMAN, `.cbox` de crYOLO (ses deux formes) et `.cs` de cryoSPARC sont superposées à l'image en cercles à leur taille réelle. Le fichier se choisit dans la liste de gauche, ou est proposé quand il porte le nom de l'image. Seuil de score, diamètre, et **retournement de Y à la main** : rien n'est retourné d'office.
- **Galerie d'une pile** (touche G) : tous les plans côte à côte, pour les particules extraites, les sections d'un volume et les **moyennes de classes 2D**. Pour celles de RELION (`*_classes.mrcs`), la part des particules et la résolution de chaque classe sont lues dans le fichier `_model.star` voisin, et les classes sont triées par population.
- **Binning dans Fourier** (« Réduction dans Fourier » du panneau Cryo) : les vues réduites gardent les basses fréquences de la transformée au lieu de moyenner des blocs. Pas de repliement, et les détails plus gros que le nouveau pixel gardent tout leur contraste. 0,4 s pour un plan 4K, mis en cache.
- **Liste de défauts des fichiers `.gain`** (tag 65100) : elle est maintenant comptée et signalée dans le panneau, au lieu d'être ignorée en silence. Elle n'est pas appliquée (voir plus bas).

Reste à faire dans M4 :
- **Somme pondérée par la dose** : elle n'a de sens que sur des frames alignées. MotionCor3 la calcule, mais le serveur ne la garde pas : la somme alignée sert à l'ajustement du CTF puis disparaît avec le dossier de l'exécution ([ADR 011](adr/011-programmes-externes.md)).
- **Appliquer la liste de défauts** d'un fichier `.gain` : son format n'est pas documenté, et un utilisateur de Falcon 4i rapporte une liste vide. Il faut un vrai fichier pour écrire le lecteur.

Écarts et limites :
- **La moyenne de frames est une fenêtre glissante** (N frames à partir de la frame courante) plutôt qu'une plage fixe. Elle couvre le cas « frames 3 à 7 » et permet de lire un film en moyenne glissante.
- **L'orientation du gain n'est jamais corrigée d'office** : le serveur mesure, l'interface montre, l'utilisateur corrige. Si la marge entre les deux meilleures orientations est inférieure à 2 % (trop peu de dose, gain sans motif), la vérification est déclarée non concluante.
- **Recherche du gain** : fichiers dont le nom contient « gain », dans le dossier du film et le dossier parent, parmi les 1 000 premières entrées. Quand le film EER nomme sa référence dans ses métadonnées (`eerGainReference`), ce fichier est proposé en premier. Un gain rangé ailleurs se désigne par le lien (`gain=<chemin logique>`) ; la recherche à partir du XML d'EPU viendra avec le parseur de M3.
- **Regroupement par dose** : il demande la dose par fraction (métadonnées du film) et la taille de pixel, que l'EER ne contient pas et qu'il faut donc saisir.
- **Un gain n'est conservé d'un fichier au suivant** que dans le même dossier et pour le même type de fichier, pour ne pas corriger deux fois une image déjà traitée.
- **Conventions des pointés non vérifiées sur de vrais fichiers.** Une coordonnée (x, y) est prise comme la colonne x et la ligne y de l'image telle qu'elle est enregistrée, la ligne 0 en haut. Certains programmes comptent les lignes depuis le bas ou retournent les images à l'import : le bouton « Retourner Y » est là pour cela. Les noms de micrographies sont rapprochés en retirant ce que les programmes y ajoutent (`_fractions`, `_patch_aligned_doseweighted`, l'identifiant de cryoSPARC en tête) ; la liste est courte et se complétera à l'usage.
- **Un `particles.star` est relu à chaque image** (une lecture ligne à ligne, environ une seconde par million de particules). Un index par micrographie sera à ajouter si cela gêne.
- **Aucun essai sur de vraies données.** Les conventions (gain `.gain` multiplicatif, sans retournement) viennent de l'étude et de RELION ; elles sont à confirmer sur un film et un gain du Falcon du labo. C'est le premier essai à faire.
- **L'ancien CryoViewer peut être éteint** : les deux fonctions qui le retenaient (passe-bas en Å, moyenne de frames) sont dans imageBio. À faire après un essai sur vos fichiers.
- **Relecture indépendante du backend Cryo** (un agent neuf, sur `proc/fourier.py`, `cryo/gain.py`, la chaîne de traitement et les routes). Les nombres, les clés de cache, l'autorisation du chemin de gain et la tenue sous 16 threads ont été vérifiés et trouvés corrects. Cinq défauts ont été trouvés et corrigés, chacun avec son test de régression :
  - une coupure plus grossière que le champ de vue faisait transformer neuf fois la surface de l'image (jusqu'à 72 fois la mémoire du plan) : le remplissage en miroir est maintenant limité à une demi-image de chaque côté ;
  - la moyenne de frames contournait la limite de taille des plans lus en mémoire ;
  - des valeurs extrêmes de coupure ou de taille de pixel donnaient une erreur 500 ou une image entièrement NaN : elles sont refusées (coupures de 0,001 Å à 1 mm) ;
  - la moyenne radiale d'un spectre dépassant la plage du `float32` donnait une erreur 500 ;
  - la vérification de l'orientation se trompait pour un gain d'échelle très éloignée de 1, et se disait sûre d'elle sur des images parfaitement plates.
  Le frontend, les lecteurs et les modules de M1 et M2 n'ont toujours pas eu de relecture indépendante.

### M5 : cœur ImageJ, partie 1 (retiré du périmètre le 11 novembre 2026)

> **Retiré** (décision 6 du §8, [ADR 009](adr/009-perimetre-cryo-et-live.md)). De ce jalon ne reste que **« Ouvrir dans Fiji »**, fait sous la forme d'un export TIFF ImageJ calibré. Le texte ci-dessous est gardé pour mémoire.

**Livrables :**
- Hyperstacks : navigation C/Z/T, composite de canaux, vues orthogonales.
- Brightness/Contrast complet (Auto/Set/Reset/Apply).
- ROI : rectangle, ovale, polygone, main levée, ligne, polyligne, point, angle.
- **ROI Manager** avec lecture et écriture de `RoiSet.zip`.
- **Measure / Set Measurements** avec la sémantique d'ImageJ, tableau de résultats exportable en CSV.
- Set Scale, profil de ligne.
- **« Ouvrir dans Fiji »** : TIFF ImageJ avec calibration et `RoiSet`, ou une petite macro `.ijm` qui ouvre le chemin NAS.

**Acceptation :**
- Mesures égales à celles de Fiji à 1e-6 près sur les images de référence (valeurs produites une fois par pyimagej et stockées en JSON).
- Aller-retour d'un `RoiSet` sans perte dans Fiji.

### M6 : cœur ImageJ, partie 2 + formats du palier 2 (retiré du périmètre le 11 novembre 2026)

> **Retiré** avec M5. Les lecteurs du palier 2 (DM3/DM4, ND2, CZI, LIF, HDF5) s'ajouteront à la demande, un par un. Le texte ci-dessous est gardé pour mémoire.

**Livrables :**
- Seuils : « Default » d'ImageJ porté depuis le code d'IJ1, plus Otsu, Li, Huang, Triangle, Yen, etc.
- **Analyze Particles.**
- Filtres (gaussien, médian, moyenne, masque flou, soustraction de fond par rolling ball).
- Z Project, Image Calculator, conversions 8/16/32 bits, recadrage, rotation, redimensionnement, binning, FFT et passe-bande.
- Historique d'annulation, **enregistreur de commandes en Python**.
- Lecteurs du palier 2.

**Acceptation :**
- Même seuil et même nombre de particules que Fiji sur les images de référence.
- Le script enregistré reproduit l'image à l'identique, bit à bit.

### M7 : qualité des acquisitions dans le Live (périmètre revu le 11 novembre 2026)

Le worker ImageJ et Bio-Formats de la première version de ce jalon est retiré avec M5 et M6 (ADR 009). Reste ce qui sert le cryo et le suivi en direct.

**Livrables :**
- **Estimation du CTF intégrée** : défocus et résolution jusqu'à laquelle les anneaux de Thon sont retrouvés, à partir du spectre de puissance, sans GPU ni programme externe. Dans le mode Cryo (à la demande) et pour chaque image du Live.
- **Seuils d'acceptation et de rejet** par session (résolution du fit CTF, glace, anneau de glace, dérive, défocus), part d'images acceptées, filtres.
- **Carte des acquisitions** : positions de platine colorées selon la qualité.
- **MotionCor3 et CTFFIND5 en sous-processus**, déclarés par l'administrateur : astigmatisme, mouvement corrigé, somme pondérée par la dose. Résultats remontés dans le Live.

**Acceptation :**
- Le défocus d'images synthétiques de CTF connu est retrouvé à 2 % près ; une image sans anneaux ne reçoit pas de défocus.
- Le plantage ou le blocage d'un programme externe est isolé : l'application reste disponible.
- Un utilisateur ne peut pas accaparer le calcul (quota par utilisateur).

**Avancement de M7 (11 novembre 2026) : fait, les trois critères passent. Les programmes externes n'ont jamais tourné pour de vrai.** Décisions : [ADR 011](adr/011-programmes-externes.md).

Fait :
- **Estimation du CTF intégrée** (`proc/ctf.py`) : ajustement à une dimension du spectre moyenné, sans astigmatisme. À la demande dans le spectre de puissance, et pour chaque image du Live.
- **Seuils d'acceptation**, part d'images acceptées, filtres ; **carte des acquisitions** colorée selon le verdict.
- **Programmes externes** (`imagebio/tools/`) : alignement du film puis ajustement du CTF sur la somme, déclarés par l'administrateur en listes d'arguments, lancés sans shell avec une échéance, sur les images qui arrivent dans une session dont l'utilisateur le demande. Résultats sur les tuiles (les deux défocus, l'astigmatisme, la résolution de l'ajustement, le mouvement), utilisés par le tri et les seuils à la place de l'estimation rapide.

Critères d'acceptation :
- **Défocus retrouvé à 2 % : passé**, sur sept images synthétiques de CTF connu (quatre tailles de pixel, 200 et 300 kV, 0,6 à 4,5 µm) ; aucune valeur annoncée sur du bruit.
- **Plantage ou blocage isolé : passé.** Avec un programme qui plante, un qui se bloque et un qui répond, le serveur continue de répondre pendant le blocage, chaque image reçoit son résultat ou son motif, le programme bloqué est tué avec ses enfants, et l'arrêt du serveur tue celui qui tourne.
- **Pas d'accaparement : passé.** Les utilisateurs passent à tour de rôle : avec douze images en attente chez l'un et deux chez l'autre, le second est servi une fois sur deux jusqu'à ce qu'il n'ait plus rien.

Limites :
- **Ni MotionCor3 ni CTFFIND n'ont été lancés** : pas de binaire, pas de carte graphique. Les options de `deploy/processing.example.toml` et les formats lus viennent de la documentation ; ils sont à vérifier sur votre installation avant de s'y fier.
- **La somme pondérée par la dose n'est pas conservée** : la somme alignée sert au CTF puis disparaît. C'était le dernier reste de M4.
- L'estimation intégrée et les mesures de dérive n'ont été validées que sur des images synthétiques.

### M8 : exploitation et déploiement pilote (2-3 sessions)

L'authentification et les racines par groupe existent dès M1. Ce jalon durcit l'exploitation.

**Livrables :**
- Pile docker-compose (Caddy, oauth2-proxy, application), et unités systemd pour l'installation sans Docker. Redis, les workers et Alembic du premier plan sont retirés avec le worker ImageJ ([ADR 010](adr/010-exploitation.md)).
- `/metrics`, sauvegarde de l'index et de la configuration (les données sont en lecture seule et le cache est jetable).
- Guide utilisateur d'une page et fiche d'exploitation d'une page.

**Acceptation :**
- Exercice de restauration réussi.
- Mise à jour avec retour arrière en moins de 10 min.
- **Pilote de 2 semaines avec 3 à 5 utilisateurs** et journal des retours.

**Avancement de M8 (11 novembre 2026) : fait, sauf le pilote, qui est à vous.** Deux critères d'acceptation sur trois passent. Les décisions sont dans l'[ADR 010](adr/010-exploitation.md) ; le mode d'emploi est la [fiche d'exploitation](exploitation.md).

Fait :
- **Image et pile Docker** (`deploy/`) : Caddy devant imageBio, avec des comptes locaux ou l'authentification unique (oauth2-proxy, OIDC). Conteneur non root, système de fichiers en lecture seule, données montées en lecture seule.
- **Installation sans Docker** : unité systemd durcie, sauvegarde quotidienne par un timer, Caddyfile.
- **Secret partagé entre le proxy et le serveur** (`IMAGEBIO_PROXY_SECRET`). Sans lui, tout compte local de la machine pouvait se faire passer pour n'importe quel utilisateur en écrivant l'en-tête d'identité lui-même. Obligatoire dans la pile fournie.
- **`/metrics`** (format Prometheus) : caches, suivis, et par dossier suivi l'âge et la durée du dernier balayage, son échec, les fichiers incomplets. Fermé par défaut, jamais servi par le proxy, sans chemin ni nom d'utilisateur.
- **`imagebio backup` et `imagebio restore`** : copie cohérente de l'index serveur en marche, sans les vignettes ; restauration refusée sous un serveur qui tourne ; l'index remplacé est mis de côté, pas détruit ; `--keep N` élague les anciennes sauvegardes.
- **Un serveur refuse au démarrage, en une ligne, un index plus récent que lui** (cas du retour arrière) ou illisible, au lieu d'échouer à la première requête du Live.
- **Guides** : [guide de l'utilisateur](guide-utilisateur.md) et [fiche d'exploitation](exploitation.md).

Critères d'acceptation :
- **Exercice de restauration : passé, et rejoué à chaque modification.** L'intégration continue construit l'image, lance la pile, suit une session simulée, sauvegarde, détruit l'index, restaure et retrouve le suivi et ses images (`deploy/compose/smoke-test.sh`). Le même scénario existe en test du backend, avec les seuils, la calibration de glace et les mesures.
- **Mise à jour avec retour arrière : passé, hors Docker.** Exercice fait avec deux versions réellement installées, dont une au schéma d'index plus récent : sauvegarde, mise à jour (index migré), retour à l'ancienne version (qui refuse l'index migré), restauration, état identique. Quelques secondes de temps machine ; le reste des 10 minutes est le temps de reconstruire ou de retrouver l'image.
- **Pilote : à faire.**

Limites, à connaître avant le pilote :
- **La pile d'authentification unique n'a jamais tourné** : il faut un fournisseur d'identité. Sa syntaxe est vérifiée, et la documentation d'oauth2-proxy n'était pas accessible à la rédaction. La fiche d'exploitation décrit la vérification à faire (identité forgée).
- Le Caddyfile de l'installation systemd n'a pas tourné non plus ; il reprend les directives de celui de la pile Docker, qui est testé.
- Rien n'a tourné sur un vrai montage NFS ou SMB, ni avec le vrai compte du labo.
- Les comptes locaux de Caddy n'ont pas de groupes : toutes les racines sont visibles de tous les comptes. Les droits par groupe demandent l'authentification unique.

### Plus tard

- Cartes d'atlas.
- Pointage (crYOLO, Topaz, ou détecteur DoG).
- 3D : visionneuse de cartes Mol\*, coupes orthogonales, tomogrammes en pyramides, navigation dans les séries d'inclinaison.
- AreTomo3 pour la tomographie en direct.
- Export RELION/cryoSPARC (groupes optiques déduits des déplacements de faisceau AFIS).
- Débruitage (Topaz-Denoise, cryoCARE, Noise2Void) sur le GPU du serveur.
- Vrai système de plugins public.
- Annotations et figures (SVG/PDF), sessions partagées.

---

## 5. Organisation du dépôt (cible)

```
imageBio/
  README.md  LICENSE (MIT)  CLAUDE.md
  docs/  PLAN.md  audit/  research/  adr/  guide-utilisateur.md  exploitation.md
  deploy/  Dockerfile  compose/  systemd/
  legacy/cryoviewer/          # CryoViewer.zip décompressé : correctifs M0 seulement, éteint après M2
  legacy/cryoviewer-live/     # tarball décompressé sans node_modules/dist : éteint après M3
  backend/
    pyproject.toml            # paquet imagebio
    src/imagebio/
      core/{dataset,calibration,display,overlay,config,security,cache}.py
      io/{registry,base}.py   io/readers/{mrc,tiff,eer,raster,dm,...}.py
      proc/{filters,contrast,fourier,project,measure,threshold}.py
      cryo/{gain,powerspec,epu_xml,mdoc,star,cs}.py
      live/{scanner,completeness,db,metrics,alerts,hub}.py
      api/{app,fs,datasets,live,jobs,ws}.py   roots.py
    tests/{unit,golden,security,perf}/  conftest.py
  frontend/                   # React + TS + Vite
    src/{shell,viewer/webgl,overlay,browser,cryo,live,imagej}/
    e2e/                      # Playwright
  workers/{cpu,gpu,imagej}/   # imagej = frontière GPL (pyproject et note de licence à part)
  fixtures/gen_fixtures.py    # générateur déterministe ; les gros fichiers ne sont jamais versionnés
  tools/microscope_sim.py     # rejoue une session EPU (écritures partielles, pauses)
  deploy/{compose.yml,Caddyfile,oauth2-proxy.cfg,roots.example.yaml,systemd/}
  .github/workflows/ci.yml
```

---

## 6. Stratégie de tests

**Fichiers de test synthétiques, générés et non versionnés :**
- **MRC :**
  - modes 0, 1, 2, 6 et 12, en petit et grand boutisme ;
  - mode 101 avec nx impair, mode 16 RGB ;
  - extension d'en-tête FEI1, drapeau IMOD ;
  - NaN, Inf et pixels chauds ;
  - fichiers tronqués et fichiers en cours d'écriture.
- **TIFF :**
  - hyperstacks ImageJ (ZCYX, TZCYX), LZW de type K3, OME avec tailles physiques ;
  - unités cm et pouce ;
  - palette, niveaux de gris avec alpha, BigTIFF.
- **EER :** à partir de l'encodeur synthétique [`research/eer_synthetic_encoder.py`](research/eer_synthetic_encoder.py) (vérifié bit à bit), plus un `.gain` avec défauts et une version retournée.
- **Session EPU complète** : XML + JPG + film, produite par le simulateur.
- **Noms hostiles :** `<script>`, guillemets, `../`, unicode, lien cassé, lien qui sort de la racine.

**Tests de référence :**
- Les lecteurs sont comparés bit à bit à `mrcfile` et `tifffile`.
- Les traitements sont comparés à des attentes analytiques : gain à la coupure, rayon des anneaux, binning de Fourier, moyenne sur une plage.
- Mesures et seuils comparés à Fiji, par pyimagej en CI de nuit.

**Robustesse et sécurité :**
- Hypothesis : un en-tête aléatoire ou un fichier tronqué doit donner une erreur 4xx propre, jamais un 500.
- Suite de sécurité : traversée de répertoire, évasion par lien symbolique, injection dans chaque champ, Origin/Host.
- La liste des routes accessibles sans authentification doit être exactement `/healthz`.

**E2E (Playwright) :**
- ouvrir → contraste → règle → partager un lien ;
- maintenir le lecteur de frames enfoncé sans créer d'arriéré ;
- rejouer une session Live à 600 images/h avec redémarrage en cours ;
- alerte reçue par un faux serveur ntfy.

**Budgets de performance en CI :** latence des plans, plafond mémoire, coût de scrutation à 50 000 fichiers, temps de la somme EER.

---

## 7. Quel modèle Claude, et comment travailler

**Recommandation : Claude Fable 5.2** pour construire le projet.
- C'est le modèle le plus capable actuellement disponible. Il est nettement meilleur sur le travail agentique long : tenir une architecture cohérente sur des dizaines de fichiers, écrire les tests d'abord, et ne pas s'arrêter au premier brouillon.
- Ici, la difficulté est surtout l'architecture : le modèle de données, la couche de lecture, le moteur de rendu WebGL, la chaîne EER et gain, le scrutateur Live. Une erreur de conception au départ coûte des dizaines de sessions ensuite.
- **Effort :** `high` par défaut. Passer à `xhigh` seulement pour les morceaux les plus difficiles (M1 `Dataset`/lecteurs, moteur de rendu de M2, scrutateur de M3). `max` est inutile ici.
- **Coût :** Fable 5.2 consomme environ deux fois plus que Claude Opus 5.5. Au tarif de l'API : 8 $ / 40 $ par million de tokens en entrée/sortie, contre 4 $ / 20 $. Dans un abonnement, cela veut dire qu'on atteint les limites d'usage plus vite. Je ne connais pas votre forfait.
- **Opus 5.5** est un très bon second choix, deux fois moins cher. Il suffit largement pour M0 (correctifs bien cadrés) et pour les tâches répétitives.
- **Sonnet 5.5** convient pour les retouches d'UI et la documentation.

**Ultracode : oui, mais par jalon, pas en permanence, et à une taille qui tient dans votre forfait.**
- **Retour d'expérience de M1 :** un workflow de 16 agents a épuisé la fenêtre d'utilisation de 5 heures après un seul module terminé (environ 1,2 million de tokens), et les 8 agents en cours ont été perdus. Écrire directement les modules restants a coûté beaucoup moins. Pour ce projet, préférez un travail direct, avec **un ou deux agents ciblés** pour la relecture adversariale d'un module à risque.
- **Utile pour :**
  - la conception d'un jalon (plusieurs propositions concurrentes, puis un juge) ;
  - les implémentations qui se parallélisent (lecteurs de formats, opérations, suites de tests) ;
  - surtout, la **revue adversariale avant fusion** (sécurité, exactitude scientifique).
- **Inutile pour :** un petit correctif ou une retouche CSS.
- **Coût :** cette évaluation seule (9 agents, environ 30 min) a consommé environ 1,2 million de tokens de sous-agents.

**Méthode de travail :**
1. Une session par jalon (ou demi-jalon). On commence en pointant vers ce plan et les critères d'acceptation du jalon, pas avec des micro-instructions. Fable travaille mieux avec un objectif et des critères qu'avec une recette pas à pas.
2. Tests d'abord pour les lecteurs, la sécurité et la complétude des fichiers.
3. Une branche et une PR par jalon, avec revue (ultracode) avant fusion.
4. Mettre à jour ce plan à la fin de chaque jalon : ce qui est fait, ce qui a changé, les décisions prises (dans `docs/adr/`).

**Où en est le projet (11 novembre 2026).** Tous les jalons du périmètre retenu sont écrits et testés sur des données générées : M0 à M4, M7 et M8 (M5 et M6 sont retirés). Rien n'a encore tourné sur de vraies données ni sur le serveur du labo. La suite ne s'écrit plus, elle s'essaie :

1. **Essai sur une vraie session** : dossier EPU sur le NAS (NFS ou SMB), un film du Falcon avec sa référence de gain, des fichiers de pointés, un canal d'alerte réel. C'est ce qui permet d'éteindre l'ancien serveur Node.
2. **Installation sur le serveur du labo** avec la [fiche d'exploitation](exploitation.md), choix entre comptes locaux et authentification unique, puis le pilote de deux semaines.
3. **Si vous voulez MotionCor3 et CTFFIND dans le Live** : les lancer à la main une fois, puis adapter `deploy/processing.example.toml`.

**Premier message suggéré pour la prochaine session (après l'essai) :**

> Lis `docs/PLAN.md` (§4 avec l'avancement de chaque jalon, et « Où en est le projet » au §7), `docs/exploitation.md`, les README et les ADR. J'ai essayé imageBio sur de vraies données ; voici ce que j'ai vu : [liste : ce qui ne s'ouvre pas, ce qui est faux, ce qui est lent, les messages d'erreur]. Corrige chaque point avec un test de régression, en commençant par ce qui empêche de s'en servir. Travaille directement, commits atomiques, puis pousse la branche.

---

## 8. Décisions à prendre

Elles bloquent peu, mais il vaut mieux les trancher avant M1.

1. **Licence du cœur :** MIT (recommandé, c'est déjà celle de CryoViewer) avec le GPL isolé dans le worker, ou GPL-3 partout pour simplifier.
2. **Authentification :** existe-t-il un LDAP ou un AD au labo ou à l'institut, ou faut-il commencer avec des comptes locaux (Authelia) ?
3. **Serveur cible :** machine, GPU, montage des données (NFS ou SMB ? le surveillant tourne-t-il sur le serveur de fichiers lui-même ?). Ces réponses fixent la stratégie de scrutation de Live.
4. **Logiciel d'acquisition :** EPU seulement, ou aussi SerialEM/Tomo5 ? Quels détecteurs (Falcon 4i/EER, K3) ? Des sessions réelles anonymisées (quelques XML, un EER, un gain) seraient précieuses pour les tests.
5. **Priorité M2 ou M3 :** qu'est-ce qui fait le plus mal au quotidien, la visionneuse ou le suivi en direct ?
6. **Périmètre : faut-il encore le cœur ImageJ (M5 et M6) ?** **Tranché le 11 novembre 2026 : non** ([ADR 009](adr/009-perimetre-cryo-et-live.md)). La raison : M5 et M6 pèsent 9 à 13 sessions, la moitié de ce qui reste, pour refaire ce que Fiji fait déjà, avec l'obligation de retrouver ses résultats à 1e-6 près. Ce qui fait mal au quotidien est le cryo et le suivi en direct, et ils sont là. À garder de ces deux jalons : un bouton « Ouvrir dans Fiji » (TIFF calibré ou macro qui ouvre le chemin du NAS), et les lecteurs de formats à la demande (DM4, par exemple). Le temps gagné va à M7 (qualité des acquisitions dans le Live), revu en conséquence, puis à M8 (déploiement et pilote).

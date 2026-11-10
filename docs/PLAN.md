# imageBio : évaluation et plan d'attaque

*Rédigé le 10 novembre 2026, à partir d'un audit multi-agents du code existant (vérifié de façon adversariale), de deux études techniques et de trois propositions d'architecture indépendantes. Les annexes détaillées sont dans [`audit/AUDIT.md`](audit/AUDIT.md) et [`research/`](research/).*

---

## 0. En bref

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

### M5 : cœur ImageJ, partie 1 (5-7 sessions)

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

### M6 : cœur ImageJ, partie 2 + formats du palier 2 (4-6 sessions)

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

### M7 : workers de calcul (4-6 sessions)

**Livrables :**
- `workers/imagej` : JVM séparée ; Bio-Formats pour le palier 3 ; exécution de macros IJM, isolée par utilisateur ; cache OME-Zarr.
- `workers/gpu` : MotionCor3 et CTFFIND5 lancés comme sous-processus. Le fichier `-FmIntFile` est généré à partir de la dose de l'XML ou de l'EER. Résultats remontés dans Live : résolution du fit CTF, défocus, astigmatisme, épaisseur, dérive.
- Tableaux de bord Live avec seuils d'acceptation et de rejet.
- Carte de grid square avec les trous acquis colorés selon la qualité.

**Acceptation :**
- Un fichier CZI ou LIF de test s'ouvre via le worker.
- Le plantage d'un worker est isolé : l'application reste disponible.
- Quotas GPU par utilisateur.

### M8 : exploitation et déploiement pilote (2-3 sessions)

L'authentification et les racines par groupe existent dès M1. Ce jalon durcit l'exploitation.

**Livrables :**
- Pile docker-compose (Caddy, oauth2-proxy, application, Redis, workers), et unités systemd pour le mode autonome.
- `/metrics`, Prometheus/Grafana ou Uptime Kuma, sauvegarde de la base et de la configuration (les données sont en lecture seule et le cache est jetable).
- Environnement de recette, migrations Alembic.
- Guide utilisateur d'une page et fiche d'exploitation d'une page.

**Acceptation :**
- Exercice de restauration réussi.
- Mise à jour avec retour arrière en moins de 10 min.
- **Pilote de 2 semaines avec 3 à 5 utilisateurs** (cryo et microscopie optique) et journal des retours.

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
  docs/  PLAN.md  audit/  research/  adr/  user-guide.md  runbook.md
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

**Ultracode : oui, mais par jalon, pas en permanence.**
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

**Premier message suggéré pour la session M0 :**

> Lis `docs/PLAN.md` (sections 2, 3.3 et 4/M0) et `docs/audit/AUDIT.md`. Réalise le jalon M0 : décompresse les deux archives dans `legacy/` (sans `node_modules`/`dist`), mets en place la CI, puis applique les correctifs de sécurité et d'exactitude listés pour CryoViewer Live et CryoViewer, avec un test de non-régression par correctif. Les critères d'acceptation de M0 doivent tous passer. Commits atomiques, puis pousse la branche.

---

## 8. Décisions à prendre

Elles bloquent peu, mais il vaut mieux les trancher avant M1.

1. **Licence du cœur :** MIT (recommandé, c'est déjà celle de CryoViewer) avec le GPL isolé dans le worker, ou GPL-3 partout pour simplifier.
2. **Authentification :** existe-t-il un LDAP ou un AD au labo ou à l'institut, ou faut-il commencer avec des comptes locaux (Authelia) ?
3. **Serveur cible :** machine, GPU, montage des données (NFS ou SMB ? le surveillant tourne-t-il sur le serveur de fichiers lui-même ?). Ces réponses fixent la stratégie de scrutation de Live.
4. **Logiciel d'acquisition :** EPU seulement, ou aussi SerialEM/Tomo5 ? Quels détecteurs (Falcon 4i/EER, K3) ? Des sessions réelles anonymisées (quelques XML, un EER, un gain) seraient précieuses pour les tests.
5. **Priorité M2 ou M3 :** qu'est-ce qui fait le plus mal au quotidien, la visionneuse ou le suivi en direct ?

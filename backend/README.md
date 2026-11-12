# imagebio : le backend

Le cœur de l'application : lecture paresseuse des images scientifiques, traitements d'affichage et API HTTP. Voir [`../docs/PLAN.md`](../docs/PLAN.md) pour le plan d'ensemble et [`../docs/adr/`](../docs/adr/) pour les décisions d'architecture.

## Installation et lancement

Python 3.11 ou plus récent.

```bash
pip install -e ".[dev]"
```

Mode autonome, sur un poste, sans authentification (écoute sur `127.0.0.1` uniquement) :

```bash
imagebio serve --local /chemin/vers/mes/donnees
```

Sur un serveur, derrière un reverse proxy qui authentifie les utilisateurs :

```bash
imagebio serve --config imagebio.toml
```

```toml
[server]
allowed_hosts = ["imagebio.lab.example"]   # nom public, vu par le proxy
# metrics = true                           # servir /metrics (voir « Exploitation »)

[auth]
mode = "proxy"                             # l'identité arrive dans X-Forwarded-User / X-Forwarded-Groups

[cache]
plane_bytes = "4GiB"

[live]
state_dir = "/var/lib/imagebio"            # index des sessions suivies et vignettes (SQLite)
interval = 5                               # secondes entre deux passages sur un dossier surveillé
min_stable = 10                            # un fichier doit garder sa taille ce temps pour être complet
# metrics = false                          # ne pas mesurer les images (chaque mesure lit l'image en entier)
# drift = true                             # mesurer aussi la dérive de chaque film (lit huit frames par film)
# mean_free_path_nm = 395                  # libre parcours moyen du microscope : épaisseur de glace en nm
# ctf = false                              # ne pas estimer le CTF de chaque image

[microscope]                               # pour le CTF, quand un fichier ne le dit pas
kv = 300
cs_mm = 2.7
amplitude_contrast = 0.07

[[alerts.channels]]                        # où le mode Live peut envoyer ses alertes
name = "telephone"                         # le nom que les utilisateurs choisissent
kind = "ntfy"                              # "ntfy", "webhook" ou "email"
url = "https://ntfy.sh/un-sujet-difficile-a-deviner"

[[alerts.channels]]
name = "equipe"
kind = "email"
to = ["cryoem@lab.example"]

[alerts.smtp]                              # seulement pour les canaux "email"
host = "smtp.lab.example"
from = "imagebio@lab.example"

# [processing]                             # programmes lancés sur les images qui arrivent : voir « Programmes externes »

[[roots]]
name = "glacios"                           # premier élément des chemins vus par les clients
path = "/srv/glacios"
groups = ["cryoem"]                        # vide ou absent : tous les utilisateurs authentifiés
```

Le proxy doit dire à l'application comment elle s'appelle dehors, sans quoi les requêtes qui modifient (surveillances, alertes) sont refusées comme venant d'un autre site : soit il transmet l'en-tête `Host` tel quel, soit il envoie `X-Forwarded-Host`, soit le nom public figure dans `allowed_hosts`. S'il envoie `X-Forwarded-Proto`, le schéma est vérifié aussi. Avec nginx : `proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme;`.

En mode `proxy`, la variable d'environnement `IMAGEBIO_PROXY_SECRET` (24 caractères au moins) fait exiger ce secret dans l'en-tête `X-Imagebio-Proxy-Secret` de chaque requête : c'est le proxy qui l'ajoute. Sans lui, tout compte local de la machine peut écrire lui-même l'en-tête d'identité ([ADR 010](../docs/adr/010-exploitation.md)).

L'installation sur un serveur (Docker ou systemd, proxy, sauvegardes, surveillance) est décrite dans la [fiche d'exploitation](../docs/exploitation.md) ; les fichiers sont dans [`deploy/`](../deploy/).

Pour voir ce qu'un lecteur comprend d'un fichier :

```bash
imagebio info movie.eer
```

Pour essayer le mode Live sans microscope, un microscope simulé écrit une session de type EPU (films écrits image par image, pauses, XML en dernier) :

```bash
imagebio simulate /chemin/vers/mes/donnees/session_test --rate 600
```

## API

Les routes sont en `GET`, sauf le démarrage et l'arrêt d'une surveillance. Les fichiers sont désignés par un chemin logique, `<racine>/<chemin relatif>`, jamais par un chemin du serveur.

| Route | Réponse |
|---|---|
| `/healthz` | État du service (sans authentification) |
| `/api/me` | Utilisateur, groupes, mode d'authentification |
| `/api/fs/roots` | Racines visibles par l'utilisateur |
| `/api/fs/list?path=` | Contenu d'un dossier (`offset`, `limit`, `hidden`) |
| `/api/datasets/info?path=` | Description du fichier : dimensions, type, calibration et sa provenance, métadonnées, avertissements |
| `/api/datasets/plane?path=&t=&c=&z=&bin=&x=&y=&w=&h=` | Un plan en octets bruts, petit-boutiste. En-têtes `X-Dtype`, `X-Shape`, `X-Bin`, `X-Region`, `X-Pixel-Size` |
| `/api/datasets/stats?path=&t=&c=&z=` | Statistiques robustes et histogramme d'un plan |
| `/api/datasets/spectrum?path=&t=&c=&z=&tile=` | Spectre de puissance d'un plan : image carrée `float32`, fréquence nulle au centre |
| `/api/datasets/spectrum/profile?…` | Moyenne radiale du spectre : fréquences (cycles par pixel) et puissances |
| `/api/datasets/ctf?path=&t=&c=&z=&kv=&cs=&amp=` | Défocus estimé d'après les anneaux de Thon du plan, résolution jusqu'à laquelle ils suivent, zéros attendus ; `found: false` quand le spectre n'en montre pas. Accepte `t_end`, `apix` et le gain |
| `/api/datasets/export?path=&t=&c=&z=&scope=` | Le plan tel qu'il est traité (`scope=plane`) ou tout le fichier tel qu'il est enregistré (`scope=stack`, 1 Gio au plus) en TIFF ImageJ avec sa calibration : « Ouvrir dans Fiji » |
| `/api/datasets/thumb?path=&t=&z=&size=` | Un plan en petite image JPEG, binnée par moyenne et étirée sur ses propres valeurs : une tuile de la galerie d'une pile |
| `/api/cryo/gain/check?path=&gain=` | Vérification de l'orientation d'une référence de gain sur un film |
| `/api/cryo/picks?path=&picks=` | Particules qu'un fichier de pointés (STAR, `.box`, `.cbox`, `.cs`) contient pour l'image `path` : centres en pixels, scores, taille des boîtes |
| `/api/cryo/classes?path=` | Ce que RELION dit des classes 2D dont `path` (`*_classes.mrcs`) contient les moyennes : part des particules et résolution, lues dans `_model.star` |

- `bin=N` renvoie la moyenne des blocs N×N en `float32` (jamais un pixel sur N). `x, y, w, h` découpe une région, en pixels pleine résolution.
- Les options d'un lecteur passent en paramètres préfixés par `opt.` : `opt.group=40&opt.superres=1` pour un film EER.
- Les traitements du mode Cryo (voir plus bas) s'ajoutent aux routes `plane` et `stats` : `t_end`, `gain`, `gain_orient`, `gain_inv`, `lp`, `hp`, `apix`. Le spectre accepte `t_end` et le gain.
- Les plans sont compressés en zstd ou gzip quand le client l'accepte et que les données s'y prêtent.
- Chaque réponse porte un `ETag` lié au contenu du fichier (taille et date) : un fichier modifié est relu.
- Les erreurs sont en JSON, `{"error": {"code": …, "message": …}}`, sans chemin du serveur.

## Mode Live

Le mode Live suit une session d'acquisition à mesure que ses fichiers arrivent (conception : [ADR 007](../docs/adr/007-live.md)).

| Route | Réponse |
|---|---|
| `GET /api/live/watches` | Les surveillances de l'utilisateur |
| `POST /api/live/watches?path=` | Surveiller un dossier ; le refaire rend la même surveillance |
| `GET /api/live/watches/{id}` | Résumé : compteurs, débit des arrivées, dernière image, disque, état du scrutateur |
| `DELETE /api/live/watches/{id}` | Arrêter sa surveillance (l'index est conservé). Avec `?forget=true`, l'oublier : elle quitte la liste, et l'index du dossier est effacé quand plus personne ne le suit |
| `GET /api/live/watches/{id}/items?since=&limit=&category=` | Expositions qui ont changé depuis le numéro de séquence `since` |
| `GET /api/live/watches/{id}/history?bucket=` | Acquisitions par tranche de temps |
| `GET /api/live/thumb?path=&size=` | Vignette JPEG d'un fichier, binnée par moyenne |
| `GET /api/live/alerts/channels` | Canaux d'alerte configurés : noms et types, jamais les adresses |
| `GET /api/live/watches/{id}/alerts` | Règles d'alerte de la surveillance et conditions en cours |
| `PUT /api/live/watches/{id}/alerts?channels=&no_image=&disk_free=&rate_drop=` | Remplacer les règles : canaux (noms séparés par des virgules), minutes sans image, pourcentage d'espace libre, pourcentage du débit horaire |
| `POST /api/live/watches/{id}/alerts/test` | Envoyer un message d'essai aux canaux de la surveillance |
| `PUT /api/live/watches/{id}/ice?i0=&mfp=` | Étalonner la glace : coups moyens d'un trou vide et libre parcours moyen en nm. Ce qui est omis est oublié |
| `GET /api/live/watches/{id}/reports` | Les rapports de qualité de la surveillance, du plus récent au plus ancien, et si un rapport est écrit à sa fin (`auto`, `idle_min`) |
| `POST /api/live/watches/{id}/reports` | Écrire le rapport maintenant (redemandé dans les dix secondes : celui qui vient d'être fait) |
| `PUT /api/live/watches/{id}/reports?auto=` | Écrire ou non un rapport à la fin de la surveillance et après un silence de la collecte |
| `GET /api/live/watches/{id}/reports/{rid}/{html,json,csv,zip}` | Un fichier du rapport, en téléchargement ; `?view=true` pour lire la page dans le navigateur, dans un bac à sable |
| `PUT /api/live/watches/{id}/microscope?kv=&cs=&amp=` | Le microscope de la session (tension, Cs, contraste d'amplitude) ; ce qui est omis est oublié. Rend les valeurs en vigueur, leur origine, et combien d'images seront mesurées de nouveau |
| `PUT /api/live/watches/{id}/processing?on=` | Demander, ou ne plus demander, les programmes déclarés sur le serveur pour ce qui arrive dans la session. `processing` du résumé dit s'il y en a (`available`, `steps`), si la session les demande (`on`) et où ils en sont (`done`, `waiting`) |
| `PUT /api/live/watches/{id}/quality?fit=&ice=&ring=&drift=&defocus_min=&defocus_max=` | Seuils d'acceptation de la session, rendus dans `quality` du résumé. Ce qui est omis est oublié |

- **Scrutation.** Un dossier n'est relu que si sa date de modification a changé ; seuls les fichiers pas encore complets sont regardés un par un. Une session de 50 000 fichiers coûte un `stat` par dossier à chaque passage.
- **Complétude.** Un fichier est complet quand sa taille et sa date n'ont pas changé sur deux passages espacés de `min_stable`, qu'il passe le contrôle d'intégrité de son format, et, pour une acquisition EPU, que son `.xml` est là. Une image dont l'écriture fait une pause reste « en cours ».
- **Expositions.** L'image intégrée, son film (`_Fractions`, `_EER`), son aperçu et ses métadonnées (`.xml` d'EPU, `.mdoc` de SerialEM) forment une exposition. Les métadonnées lues (taille de pixel, dose, défocus visé, horodatage) sont dans `meta`.
- **Suivi.** Chaque changement d'une exposition reçoit un numéro de séquence ; un client redemande `items?since=<dernier numéro>` et ne reçoit que ce qui a changé, suppressions comprises.
- **Session ancienne.** À la première indexation, les dossiers inchangés depuis plus d'un jour sont indexés sans regarder leurs fichiers ; leurs expositions ont `verified: false`. Dans les dossiers plus récents, chaque fichier est regardé une fois.
- **Par utilisateur.** Une surveillance appartient à celui qui l'a créée ; le dossier est autorisé de nouveau à chaque requête. Huit surveillances actives au plus par utilisateur, et les 32 dernières surveillances arrêtées ; les plus anciennes sont oubliées.
- **Index occupé.** Une requête qui doit écrire pendant que l'index est tenu par une autre écriture attend, puis reçoit `503` (`live_index_busy`) avec `Retry-After` : elle peut être renvoyée telle quelle.
- **Sur NFS ou SMB**, le cache d'attributs du client retarde la détection ; monter le dossier surveillé avec `actimeo=1` le réduit. Non mesuré sur stockage réseau pour l'instant.
- **Mesures.** Après chaque passage, quelques images sont mesurées : coups moyens et écart-type (pixels chauds écartés), rapport de l'anneau de glace (puissance entre 3,40 et 3,95 Å sur celle des bandes voisines ; donné si le champ fait au moins 512 pixels et le pixel au plus 1,5 Å). Elles apparaissent dans `metrics` de chaque exposition (`null` : pas encore mesurée ; `{}` : image illisible), avec un nouveau numéro de séquence. Le résumé donne dans `ice` le nombre d'images mesurées et la référence de la session (les coups des images les plus claires) : la glace relative d'une image est `ln(référence / mean)`. Toutes les images arrivées pendant la surveillance sont mesurées, et les 200 plus récentes d'une session trouvée déjà écrite.
- **Glace et dérive.** `ice` du résumé donne la référence en vigueur (`reference` : l'I₀ de l'utilisateur, sinon le niveau de la session), si elle est étalonnée (`calibrated`) et le libre parcours moyen connu (`mfp_nm`). L'épaisseur d'une image est `mfp_nm × ln(reference / mean)`. Avec `[live] drift = true`, `metrics` contient aussi `drift` et `drift_unit` (« A » ou « px ») : le déplacement entre les quatre premières et les quatre dernières frames du film, absent quand rien ne se laisse suivre.
- **Rapport de qualité** ([ADR 012](../docs/adr/012-rapport-de-collecte.md), [schéma](../docs/rapport-live.md)). `live/report.py` le calcule (fonction pure), `live/report_render.py` l'écrit (page HTML autonome sans script, JSON, CSV protégé contre les formules, zip). Écrit à la demande, à l'arrêt de la surveillance et après `[live] report_after_idle_min` minutes de silence ; vingt gardés par surveillance dans `<état>/reports/`. Le verdict de chaque image est celui de l'interface, calculé par `live/quality.py` (cas communs : `tests/fixtures/verdict_cases.json`).
- **Microscope d'une session.** Tension, Cs et contraste d'amplitude du CTF : le fichier d'acquisition (tension seulement), puis la session, puis le dossier de données (`[[roots]] kv`, `cs_mm`, `amplitude_contrast`), puis `[microscope]`. Chaque mesure garde dans `metrics.microscope` ce qu'elle a utilisé ; un changement fait mesurer de nouveau les images concernées.
- **CTF.** Chaque image mesurée reçoit aussi `ctf` : `defocus` (µm), `fit` (Å, jusqu'où les anneaux suivent le défocus trouvé) et `score`. Ajustement à une dimension du spectre moyenné (`proc/ctf.py`) : pas d'astigmatisme, pas de lame de phase ; absent quand le spectre ne montre pas d'anneaux ou que le champ fait moins de 512 pixels. La tension vient du XML d'EPU, sinon de `[microscope]`.
- **Métadonnées tardives.** Une session trouvée déjà écrite est indexée sans ouvrir ses fichiers ; le XML d'EPU d'une image est alors lu quand elle est mesurée (dose, défocus visé, position de platine).
- **Seuils d'acceptation.** Ils sont gardés avec la surveillance ; le verdict est rendu par l'interface, sur les mesures que chaque exposition porte déjà. Une mesure absente ne compte pas contre une image.
- **Alertes.** Les règles sont jugées après chaque passage : plus d'image depuis N minutes, espace libre sous un pourcentage, débit sur 5 minutes sous un pourcentage du débit horaire, scrutateur en échec. Un message part quand une condition commence et un autre quand elle cesse ; un canal en panne est retenté seul, cinq fois au plus, sans que les autres reçoivent le message de nouveau. Pour quitter une condition, il faut une marge (disque : un dixième du seuil, au moins un demi-point) et deux minutes sans elle ; une chute de débit doit durer deux minutes avant d'être annoncée, et ne se juge qu'au-dessus de 60 images dans l'heure. Les règles s'arrêtent une semaine après la dernière visite de la surveillance par son utilisateur. Les adresses restent dans la configuration : un utilisateur choisit un canal par son nom. En mode autonome, `imagebio serve --local DOSSIER --ntfy URL` (ou `--webhook URL`) déclare un canal. Le mot de passe d'un relais de messagerie se donne par une variable d'environnement (`password_env`), jamais dans le fichier.

## Programmes externes

Facultatif ([ADR 011](../docs/adr/011-programmes-externes.md)). L'administrateur déclare un alignement de film (`[processing.motion]`) et un ajustement du CTF (`[processing.ctf]`) ; un exemple commenté pour MotionCor3 et CTFFIND est dans [`deploy/processing.example.toml`](../deploy/processing.example.toml). **Jamais essayé avec les vrais programmes** : les tests les remplacent par des programmes écrits pour l'occasion (`tests/fixtures/programs.py`).

- **Déclaration.** `argv` est une liste d'arguments, le programme en tête par son chemin absolu ; jamais de shell. Jokers : `{movie}`, `{image}`, `{out}`, `{apix}`, `{kv}`, `{cs}`, `{amp}`, `{dose}`, `{frames}`. `stdin` : les réponses tapées au programme, une par ligne. `result` : le fichier lu ensuite, dans `{out}/` (un `*` dans son nom est permis) ; `parser` : `ctffind`, `motion-star` ou `motion-log`. `formats` donne une autre liste d'arguments par extension de film. `timeout` en secondes ; `slots` programmes à la fois.
- **Déroulement.** Pour chaque image arrivée pendant le suivi d'une session qui le demande : alignement du film s'il y en a un, puis CTF sur la somme écrite (`image`), sinon sur l'image intégrée par le microscope. Chaque exécution a son dossier, supprimé ensuite. Avec un alignement, une image attend son film deux minutes au plus, et est traitée de nouveau si le film arrive après.
- **Résultats.** `processing` de chaque exposition : `motion` (`total`, `early`, `max_step`, `frames`, `unit`), `ctf` (`defocus`, `defocus_u`, `defocus_v`, `astigmatism`, `angle`, `cc`, `fit`, et `tilt`, `thickness` avec CTFFIND 5), `seconds`. Une étape sans résultat contient `{"error": "<motif>"}` : un état, jamais la sortie du programme ni un chemin.
- **Isolement.** Échéance, groupe de processus tué en entier (aussi à l'arrêt du serveur), environnement sans les secrets du serveur, valeurs à caractère de contrôle refusées, résultat lu seulement dans le dossier de l'exécution.
- **Tour de rôle.** Les utilisateurs passent chacun leur tour, puis les sessions de chacun ; dans une session, la plus récente d'abord.

## Exploitation

```bash
imagebio backup /var/backups/imagebio --config imagebio.toml --keep 14   # serveur en marche
imagebio restore /var/backups/imagebio/DATE --config imagebio.toml       # serveur arrêté ; --force pour remplacer
```

- **Sauvegarde** : un dossier daté avec l'index du Live (copie cohérente, sans les vignettes), la configuration et un manifeste. **Restauration** : refusée tant qu'un serveur tient le dossier d'état ; l'index remplacé est mis de côté, pas détruit. Les migrations de l'index ne vont que vers l'avant : revenir à une version précédente, c'est restaurer la sauvegarde prise avant la mise à jour.
- **`/metrics`** (format Prometheus, `[server] metrics = true`) : caches, suivis par état, et par dossier suivi l'âge et la durée du dernier passage, son échec, les fichiers incomplets, le nombre d'expositions ; taille de l'index. Ni chemin ni nom d'utilisateur. Servi aux clients de la machine elle-même, ou au porteur du jeton `IMAGEBIO_METRICS_TOKEN` (`Authorization: Bearer …`).
- Au démarrage, un index écrit par une version plus récente, ou illisible, arrête le serveur avec un message d'une ligne.

## Traitements du mode Cryo

Ils sont appliqués par le serveur au plan en pleine résolution, toujours dans cet ordre, puis viennent la découpe (`x, y, w, h`) et le binning. Un plan traité est en `float32`.

| Paramètres | Traitement |
|---|---|
| `t_end=N` | Le plan devient la moyenne des frames `t` à `N` incluses (accumulée en `float64`) |
| `gain=<chemin logique>` | Multiplication par une référence de gain ; les pixels de gain nul sont des défauts, remplacés par la médiane de leurs voisins valides. `gain_orient=0..7` : orientation dans laquelle la référence est enregistrée. `gain_inv=true` : la référence est une image de coups (champ plat), convertie en `moyenne / coups` |
| `lp=D`, `hp=D` | Passe-bas et passe-haut gaussiens, coupures en Å (gain ½ à la coupure) ; les deux ensemble font un passe-bande. L'image est prolongée en miroir avant la transformée pour que les bords ne se mélangent pas ; le passe-haut conserve la moyenne |
| `apix=A` | Taille de pixel en Å, pour un fichier qui ne la contient pas. Sans elle, un filtre en Å sur un tel fichier est refusé |
| `fbin=true` | Le binning (`bin` > 1) garde les basses fréquences de la transformée de Fourier au lieu de moyenner des blocs : pas de repliement, détails plus gros que le nouveau pixel à plein contraste. Une région doit commencer sur un multiple du bin |

- La référence de gain est un fichier comme un autre : chemin logique, mêmes droits, mêmes lecteurs (`.gain`, TIFF, MRC). Elle peut être un nombre entier de fois plus fine ou plus grossière que l'image (référence 8K pour un rendu 4K, référence 4K pour un rendu en super-résolution). Un fichier dont plus d'un dixième des pixels sont inutilisables est refusé.
- **Pointés** ([ADR 008](../docs/adr/008-pointes-et-galeries.md)). Une coordonnée (x, y) est la colonne x et la ligne y de l'image telle qu'elle est enregistrée ; rien n'est retourné par le serveur. Un fichier de jeu entier (`particles.star`, `.cs`) est filtré sur le nom de la micrographie, une fois retiré ce que les programmes y ajoutent (`_fractions`, `_patch_aligned_doseweighted`, identifiant de cryoSPARC). La réponse est bornée à 50 000 points et ses `notes` disent ce qui a été laissé de côté. Conventions non vérifiées sur de vrais fichiers.
- La réponse de `/api/cryo/gain/check` contient `listed_defects` : le nombre d'entrées de la liste de défauts qu'un fichier `.gain` porte dans son tag TIFF 65100. Cette liste n'est **pas appliquée** (son format n'est pas documenté) ; elle est signalée pour ne pas être ignorée en silence.
- `/api/cryo/gain/check` corrige la moyenne des 20 premières frames avec la référence dans ses huit orientations et mesure chaque fois la planéité de l'image (médiane, sur des blocs de 64 pixels, de la variance divisée par le carré de la moyenne). La bonne orientation annule le motif fixe du détecteur, les autres en ajoutent un second. La réponse donne les huit scores, la meilleure orientation, si la marge est significative (`confident`) et si le gain améliore l'image par rapport à l'absence de gain (`improves`).
- Le spectre de puissance est un périodogramme moyenné sur des tuiles de `tile` pixels (512 par défaut) fenêtrées et recouvrantes. Une période de `d` Å se trouve à `côté × Å-par-pixel / d` pixels du centre. Il est calculé après la moyenne et le gain, sans les filtres.

## Formats lus

| Format | Lecteur | Notes |
|---|---|---|
| MRC, MRCS, MAP, REC, ST, ALI | `mrc` | Modes 0, 1, 2, 6, 12, 16 et 101 ; taille de pixel de l'en-tête ou de l'extension FEI1/FEI2 ; fichiers en cours d'écriture |
| EER | `eer` | Fractions sommées par groupes (`group`), super-résolution (`superres`) ; la taille de pixel n'est pas dans le fichier |
| TIFF, BigTIFF, OME-TIFF, TIFF ImageJ, `.gain` | `tiff` | Hyperstacks, films multi-pages compressés, RGB, palettes ; références de gain d'EPU |
| PNG, JPEG, BMP, GIF, WebP | `raster` | PNG 16 bits exacts, animations sur l'axe T |

Un lecteur est choisi par l'extension et par le contenu : un TIFF nommé `.mrc` est ouvert comme un TIFF.

## Organisation du code

```
src/imagebio/
  core/model.py      Dataset (axes T, C, Z, Y, X), DatasetInfo, calibration avec provenance
  core/errors.py     erreurs publiques, associées à un statut HTTP
  core/config.py     configuration (racines, authentification, caches)
  core/cache.py      ByteLRU (plans, borné en octets) et DatasetPool (fichiers ouverts)
  io/registry.py     contrat des lecteurs et registre
  io/readers/        mrc.py, eer.py, tiff.py, raster.py
  proc/              binning.py (moyenne par blocs), stats.py (statistiques robustes),
                     fourier.py (filtres en Å, spectre de puissance, moyenne radiale, réduction dans Fourier),
                     ctf.py (défocus d'après les anneaux de Thon)
  cryo/gain.py       références de gain : application, défauts, vérification de l'orientation
  cryo/epu.py        métadonnées EPU (.xml par image), nom commun des fichiers d'une exposition
  cryo/mdoc.py       métadonnées SerialEM et Tomo5 (.mdoc)
  cryo/star.py       tables des fichiers STAR, lues ligne à ligne
  cryo/picks.py      pointés : STAR, .box, .cbox, .cs vers des centres en pixels de l'image
  cryo/classes.py    classes 2D de RELION (fichier _model.star)
  live/layout.py     rôle de chaque fichier et exposition à laquelle il appartient, d'après son nom
  live/completeness.py  un fichier est-il complet ? taille stable et contrôle d'intégrité
  live/index.py      index SQLite : surveillances, dossiers, fichiers, expositions, vignettes
  live/scanner.py    scrutateur d'un dossier surveillé (un passage = une transaction)
  live/service.py    surveillances par utilisateur, scrutateurs partagés, résumé, vignettes
  live/thumbs.py     vignettes binnées par moyenne
  live/alerts.py     règles d'alerte, jugement, envoi (ntfy, webhook, e-mail)
  live/metrics.py    mesures sur une image : coups moyens, rapport de l'anneau de glace, CTF, dérive d'un film
  live/quality.py    seuils d'acceptation d'une session, et verdict sur une image (comme l'interface)
  live/report.py     rapport de qualité d'une session (calcul pur)
  live/report_render.py  le rapport en fichiers : page HTML autonome, JSON, CSV, zip
  live/backup.py     sauvegarde et restauration de l'index et de la configuration
  live/simulator.py  microscope simulé (tests et démonstration)
  fs/roots.py        chemins logiques et confinement dans les racines
  service/planes.py  service de plans : pool, cache, chaîne de traitement, binning, statistiques
  service/export.py  export en TIFF ImageJ calibré
  tools/             programmes externes : runner.py (lancement sans shell, échéance, groupe de processus),
                     templates.py (jokers), parsers.py (résultats de CTFFIND et d'un alignement),
                     pipeline.py (alignement puis CTF sur une exposition)
  api/               app.py (FastAPI), live.py (routes du Live), security.py (identité, Host, secret
                     du proxy, requêtes venues d'un autre site), metrics.py (/metrics), cli.py
tests/               un dossier par module ; fixtures/ génère les fichiers de test
```

## Tests

```bash
python -m pytest            # suite complète (une à deux minutes)
python -m pytest -m perf    # budgets de performance (plus lents, fichiers de 4K)
ruff check src tests && ruff format --check src tests
python -m mypy              # typage strict
```

Les fichiers de test sont générés par `tests/fixtures/` ; aucun binaire n'est versionné. Chaque lecteur est comparé bit à bit à sa bibliothèque de référence (`mrcfile`, `tifffile`, Pillow) et soumis à des fichiers abîmés ou hostiles.

## Ajouter un lecteur

Un lecteur est un module qui expose `READER`, une instance d'une sous-classe de `imagebio.io.registry.Reader` : `sniff()` reconnaît le contenu, `open()` renvoie un `Dataset`. Les lecteurs intégrés sont listés dans `BUILTIN_READER_MODULES` ; un paquet tiers s'enregistre par le groupe de points d'entrée `imagebio.readers`.

Règles à respecter :
- lire un seul plan à la fois, avec des lectures positionnées (pas de position de fichier partagée entre threads) ;
- valider les dimensions contre la taille réelle du fichier avant d'allouer ;
- ne jamais inventer une taille de pixel : si elle est inconnue, le dire (`CalibrationSource.DEFAULT` et un avertissement) ;
- lever `CorruptFileError` pour tout problème de contenu, avec le nom du fichier mais sans son chemin.

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

[auth]
mode = "proxy"                             # l'identité arrive dans X-Forwarded-User / X-Forwarded-Groups

[cache]
plane_bytes = "4GiB"

[[roots]]
name = "glacios"                           # premier élément des chemins vus par les clients
path = "/srv/glacios"
groups = ["cryoem"]                        # vide ou absent : tous les utilisateurs authentifiés
```

Pour voir ce qu'un lecteur comprend d'un fichier :

```bash
imagebio info movie.eer
```

## API

Toutes les routes sont en `GET`. Les fichiers sont désignés par un chemin logique, `<racine>/<chemin relatif>`, jamais par un chemin du serveur.

| Route | Réponse |
|---|---|
| `/healthz` | État du service (sans authentification) |
| `/api/me` | Utilisateur, groupes, mode d'authentification |
| `/api/fs/roots` | Racines visibles par l'utilisateur |
| `/api/fs/list?path=` | Contenu d'un dossier (`offset`, `limit`, `hidden`) |
| `/api/datasets/info?path=` | Description du fichier : dimensions, type, calibration et sa provenance, métadonnées, avertissements |
| `/api/datasets/plane?path=&t=&c=&z=&bin=&x=&y=&w=&h=` | Un plan en octets bruts, petit-boutiste. En-têtes `X-Dtype`, `X-Shape`, `X-Bin`, `X-Region`, `X-Pixel-Size` |
| `/api/datasets/stats?path=&t=&c=&z=` | Statistiques robustes et histogramme d'un plan |

- `bin=N` renvoie la moyenne des blocs N×N en `float32` (jamais un pixel sur N). `x, y, w, h` découpe une région, en pixels pleine résolution.
- Les options d'un lecteur passent en paramètres préfixés par `opt.` : `opt.group=40&opt.superres=1` pour un film EER.
- Les plans sont compressés en zstd ou gzip quand le client l'accepte et que les données s'y prêtent.
- Chaque réponse porte un `ETag` lié au contenu du fichier (taille et date) : un fichier modifié est relu.
- Les erreurs sont en JSON, `{"error": {"code": …, "message": …}}`, sans chemin du serveur.

## Formats lus

| Format | Lecteur | Notes |
|---|---|---|
| MRC, MRCS, MAP, REC, ST, ALI | `mrc` | Modes 0, 1, 2, 6, 12, 16 et 101 ; taille de pixel de l'en-tête ou de l'extension FEI1/FEI2 ; fichiers en cours d'écriture |
| EER | `eer` | Fractions sommées par groupes (`group`), super-résolution (`superres`) ; la taille de pixel n'est pas dans le fichier |
| TIFF, BigTIFF, OME-TIFF, TIFF ImageJ | `tiff` | Hyperstacks, films multi-pages compressés, RGB, palettes |
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
  proc/              binning.py (moyenne par blocs), stats.py (statistiques robustes)
  fs/roots.py        chemins logiques et confinement dans les racines
  service/planes.py  service de plans : pool, cache, binning, statistiques
  api/               app.py (FastAPI), security.py (identité, Host), cli.py
tests/               un dossier par module ; fixtures/ génère les fichiers de test
```

## Tests

```bash
python -m pytest            # suite complète (moins de 30 s)
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

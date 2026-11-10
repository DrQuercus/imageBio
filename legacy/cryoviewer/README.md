# CryoViewer

Visionneuse légère pour le screening d'images et de films cryo-EM, dans le navigateur.
Un seul fichier Python (`server.py`), aucune dépendance front-end à installer.

## Fonctionnalités

- **Formats** : `.mrc` `.mrcs` `.st` `.rec` `.ali` `.map` (modes 0, 1, 2, 6, 12, 16, 101, little/big-endian),
  `.eer` (aperçu uniquement, voir limites), `.tif` / `.tiff` (8/16/32 bits, piles multi-pages), `.jpg`, `.png`
- **Navigateur de fichiers côté serveur** (comme CryoSPARC) : on parcourt le disque depuis l'interface et on clique sur un fichier
- **Drag & drop** d'un fichier depuis le Finder / l'Explorateur
- **Filtre passe-bas** de 0 à 100 Å (saisie jusqu'à 500 Å), gaussien dans l'espace de Fourier, 20 Å par défaut
- **Règle** : mesure en pixels, Å et nm, à partir de la taille de pixel du header ou d'une valeur saisie
- **Zoom** à la molette (centré sur le curseur) et **outil Pan**
- Contraste (black/white point), gamma, inversion, histogramme synchronisé avec l'image affichée
- Lecture des films frame par frame, moyenne sur une plage de frames

## Installation

Python 3.9+ requis.

```bash
git clone https://github.com/drquercus/CryoViewer.git
cd CryoViewer
pip install -r requirements.txt
python3 server.py
```

Le navigateur s'ouvre sur `http://127.0.0.1:7473`.

```bash
python3 server.py /chemin/vers/mes/films     # ouvre directement un dossier
python3 server.py micrograph.mrc             # ou un fichier
python3 server.py --port 8000 --no-browser
```

### macOS (double-clic)

```bash
chmod +x CryoViewer.command
xattr -cr .          # retire la quarantaine Gatekeeper
```

Puis double-clic sur `CryoViewer.command`.

## Raccourcis

| Touche | Action |
|---|---|
| `←` `→` | Frame précédente / suivante |
| `Espace` | Lecture / pause |
| `A` | Contraste auto |
| `I` | Inverser |
| `L` | Activer / désactiver le passe-bas |
| `R` | Outil règle |
| `P` | Outil pan |
| `Échap` | Outil par défaut |
| `S` | Moyenne de frames |
| `+` `-` `0` | Zoom avant / arrière / reset |

## Tests

```bash
pip install pytest
pytest -q
```

## Limites connues

- **EER** : seul le comptage de frames et un aperçu synthétique (bruit de Poisson) sont fournis ; le décodage réel
  des événements nécessite RELION / MotionCor.
- Pas de pondération par la dose ni de correction CTF : affichage uniquement.
- Le filtre passe-bas est recalculé à chaque requête (pas de cache des frames filtrées).
- La taille de pixel des TIFF est lue depuis les tags de résolution quand c'est possible ; sinon elle vaut 1 Å/px et
  se corrige à la main dans le panneau Ruler.
- Le serveur écoute uniquement sur `127.0.0.1` et n'a aucune authentification : ne pas l'exposer sur un réseau.

## Licence

MIT, voir [LICENSE](LICENSE).

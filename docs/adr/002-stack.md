# ADR 002 : backend Python FastAPI, frontend React/TypeScript WebGL2

**État :** acceptée.

## Contexte

Le projet doit servir plusieurs utilisateurs depuis un serveur proche des données (NAS, GPU), sans installation sur les postes. Les fichiers sont trop gros pour voyager : 0,5 Go par film EER, 20 Go pour un tomogramme. Les deux applis existantes ont déjà cette forme (serveur Python + interface web, serveur Node + React), mais avec deux décodeurs MRC différents, tous deux fautifs. Détails dans `docs/PLAN.md` §3 et `docs/research/foundation-imagej-formats.md`.

## Décision

- **Un seul backend** : Python, FastAPI, uvicorn. Il contient l'unique décodeur de chaque format, le cache, les traitements et l'API.
- **Un seul frontend** : React et TypeScript. Le canevas est en WebGL2 ; contraste, gamma et palettes sont calculés dans le shader.
- **Transport** : le serveur envoie les plans en tableaux typés bruts (petit-boutiste), binnés au zoom affiché, compressés en zstd ou gzip selon ce que le navigateur accepte.
- **ImageJ et Bio-Formats** : dans un worker séparé (ADR 001).

## Conséquences

- Régler le contraste ne coûte aucun aller-retour serveur, et la vraie valeur du pixel est disponible côté client.
- Le serveur Node de CryoViewer Live disparaît au jalon M3 ; son interface React est portée.
- Les calculs lourds (FFT, sommes EER) tournent côté serveur, là où sont le CPU et le GPU.

## Alternatives écartées

- **Étendre Fiji** (Java Swing) : l'interface AWT d'ImageJ 1 ne se modernise pas, il faut une installation par poste, et il n'y a pas de modèle multi-utilisateur.
- **Garder Node pour Live** : un deuxième décodeur et un deuxième modèle de sécurité.
- **ImageJ.JS / CheerpJ** : licence propriétaire et ancienne interface.
- **napari** : très bon outil sur un poste, mais pas une application réseau.
- **Electron ou Tauri** : coût de packaging sans résoudre le multi-utilisateur. La même interface pourra être emballée plus tard.

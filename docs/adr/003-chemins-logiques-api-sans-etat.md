# ADR 003 : chemins logiques et API de datasets sans état

**État :** acceptée.

## Contexte

L'audit a montré que les deux applis existantes acceptaient des chemins absolus venant du client (lecture de fichiers arbitraires) et gardaient un état global partagé entre utilisateurs (un fichier « courant », une surveillance unique). Voir `docs/audit/AUDIT.md` : CV-10, LIVE-2, LIVE-7.

## Décision

1. **Le client ne voit et n'envoie jamais de chemin serveur.** Il utilise des chemins *logiques* : `<nom de racine>/<chemin relatif>`, par exemple `glacios/session12/movie_001.eer`. Les racines sont déclarées dans la configuration du serveur, avec les groupes autorisés à les lire.
2. **Résolution** (`imagebio.fs.roots`) : le chemin réel (`realpath`) de la cible doit se trouver dans une racine visible par l'utilisateur. Un lien symbolique qui sort des racines est refusé et n'apparaît pas dans les listes.
3. **L'API des datasets est sans état.** Chaque requête désigne le fichier par son chemin logique : `GET /api/datasets/plane?path=…&t=…&bin=…`. Il n'y a ni identifiant de session côté serveur, ni fichier « ouvert » par utilisateur.
4. **L'autorisation est vérifiée à chaque requête**, avant toute lecture du cache.
5. **Les caches sont indexés sur l'identité du contenu** (`FileId` : chemin réel, taille, date de modification), jamais sur le chemin seul.
6. **Les messages d'erreur sont publics** : ils ne contiennent jamais de chemin serveur.

## Conséquences

- Un lien vers une vue (fichier, plan, zoom) peut se partager : il suffit de l'URL.
- Le serveur peut redémarrer ou tourner en plusieurs processus sans perdre d'état.
- Un fichier modifié sur le disque est relu : son `FileId` change, donc son ETag et ses entrées de cache aussi.
- Les handles de fichiers restent ouverts dans un pool borné (`DatasetPool`), ce qui garde le coût d'une requête sans état proche de celui d'une session.

## Alternative écartée

Une session côté serveur (`POST /open` qui renvoie un identifiant). Elle économise une résolution de chemin par requête, mais elle réintroduit un état à nettoyer, des fuites entre utilisateurs possibles et des liens non partageables.

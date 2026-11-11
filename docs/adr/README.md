# Décisions d'architecture (ADR)

Une décision par fichier. Chaque fichier donne le contexte, la décision, ses conséquences et les alternatives écartées. Une décision se remplace par un nouvel ADR ; on ne réécrit pas l'ancien, on le marque « remplacé par ».

| N° | Décision | État |
|---|---|---|
| [001](001-licence.md) | Cœur sous licence MIT, code GPL isolé dans un worker | Proposée (à confirmer) |
| [002](002-stack.md) | Backend Python FastAPI, frontend React/TypeScript WebGL2 | Acceptée |
| [003](003-chemins-logiques-api-sans-etat.md) | Chemins logiques et API de datasets sans état | Acceptée |
| [004](004-modele-de-donnees.md) | Modèle de données : axes TCZYX, plans 2D, calibration avec provenance | Acceptée |
| [005](005-authentification.md) | Authentification déléguée au reverse proxy, écoute en local | Proposée (à confirmer) |
| [006](006-traitements-cote-serveur.md) | Traitements du mode Cryo côté serveur, dans un ordre fixe | Acceptée |
| [007](007-live.md) | Mode Live : scrutation par dossier, index SQLite, suivi par numéro de séquence | Acceptée |
| [008](008-pointes-et-galeries.md) | Pointés et galeries : lus par le serveur, jamais retournés d'office | Acceptée |
| [009](009-perimetre-cryo-et-live.md) | Périmètre recentré sur le cryo et le suivi en direct : pas de cœur ImageJ | Acceptée |

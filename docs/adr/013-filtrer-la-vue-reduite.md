# ADR 013 : une vue réduite filtrée est réduite dans Fourier, puis filtrée

**État :** acceptée (lot A, 12 novembre 2026). Elle amende les points 2 et 3 de l'ADR 006.

## Contexte

L'ADR 006 filtre le plan en pleine résolution, puis le réduit. Chaque nouveau réglage d'un filtre coûte donc une transformée du plan entier : 1,1 s sur une image 4096 × 4096, 1,5 s sur un K3 (5760 × 4092), mesurés sur le serveur de développement. Un curseur ne peut pas suivre la main à ce prix, et l'utilisateur trouvait les filtres moins efficaces que ceux de cryoSPARC.

L'ADR 006 avait écarté « filtrer la vue binnée », parce que le filtre agirait sur des pixels moyennés. L'objection tient pour la moyenne par blocs, pas pour la réduction dans Fourier.

## Décision

1. **Une vue filtrée à un bin supérieur à 1 est réduite dans Fourier, puis filtrée** à la taille de pixel de la grille réduite, axe par axe (`service/planes.py`, `_reduced`). La réduction garde telles quelles les basses fréquences de la transformée, et le filtre multiplie chacune d'elles par son gain : les deux opérations commutent. Seuls les bords diffèrent, parce que chaque calcul prolonge l'image à sa façon.
2. **La réduction non filtrée est faite une fois par plan et gardée en cache** (0,4 à 0,7 s). Déplacer ensuite un filtre coûte la transformée du plan réduit : 35 ms au bin 4 d'une image 4k, 0,2 s au bin 2.
3. **La moyenne par blocs n'est jamais filtrée** : elle replie sous la coupure le bruit qu'elle ne retire pas. Avec un filtre, une vue réduite passe donc par Fourier, que la case « Réduction dans Fourier » soit cochée ou non ; la case ne change plus que les vues non filtrées.
4. **Une région hors de la grille du bin** est calculée comme avant (filtre en pleine résolution, découpe, moyenne par blocs) : le visualiseur aligne toujours ses régions, l'ancien calcul reste pour les autres clients.
5. **Les statistiques suivent ce qui est montré quand le client le demande** (`/api/datasets/stats?bin=`) : pendant qu'un filtre bouge, l'histogramme et la fenêtre automatique viennent de la vue réduite, pas d'une nouvelle transformée en pleine résolution.
6. **Le détail en pleine résolution vient au relâchement du curseur** : le visualiseur ne demande pas de région de détail tant que le curseur est tenu.
7. **Un second profil de filtre** (`sharp=true`, Butterworth d'ordre 4) s'ajoute au profil gaussien. Il fait partie de `Processing.key()`, comme le reste.

## Vérification

`backend/tests/service/test_reduced_filter.py` compare la vue servie à la référence de l'ADR 006, le plan filtré en pleine résolution puis réduit par une découpe de Fourier écrite en numpy seul, sur une micrographie de synthèse (taches de 60 à 150 Å, pente d'épaisseur, bruit fort), pour les deux profils, aux bins 2 et 4, en passe-bas, passe-haut et passe-bande :

| Mesure (écart quadratique rapporté à l'écart-type de l'image) | Mesuré | Borne du test |
|---|---|---|
| À plus de 16 pixels réduits des bords, après retrait d'un décalage uniforme | 0,1 à 1,4 % | 1,5 % |
| Décalage uniforme (passe-haut : il garde la moyenne de l'image prolongée en miroir) | jusqu'à 3,1 % | 4 % |
| Image entière, bords compris | 3 à 10 % | 12 % |
| Moyenne par blocs puis filtre, au centre, coupure à moins de 5 pixels réduits | 4 à 45 % | au moins 5 fois l'écart de la méthode retenue |

Le décalage uniforme ne change rien à l'affichage : la fenêtre de contraste automatique l'absorbe.

## Conséquences

- Un curseur de filtre suit la main sur la vue d'ensemble ; en zoom, le détail arrive une à deux secondes après le relâchement.
- Le cache du serveur garde en plus une vue réduite non filtrée par plan (4 à 6 Mio au bin 4) et une vue filtrée par réglage essayé ; il reste borné en octets.
- Les étiquettes (`ETag`) des plans et des statistiques portent un numéro de version : une réponse gardée par un navigateur avant ce changement n'est pas resservie.
- Les bords d'une vue réduite filtrée ne sont plus ceux d'une réduction du plan filtré. Au centre, l'image ne change pas.

# ADR 007 : mode Live — scrutation sans notifications, index SQLite, suivi par numéro de séquence

**État :** acceptée.

## Contexte

L'ancien CryoViewer Live surveillait un motif de fichiers avec chokidar : un `stat` par fichier toutes les trois secondes (intenable à 50 000 fichiers sur NFS), une seule surveillance pour tous les utilisateurs, aucun état conservé, et un fichier déclaré complet dès que sa taille ne bougeait plus pendant deux secondes — donc à moitié lu quand le PC du microscope faisait une pause (`docs/audit/AUDIT.md`, LIVE). Les notifications du noyau (inotify) ne voient pas les écritures faites par une autre machine sur un montage NFS ou SMB, ce qui est précisément le cas d'usage.

## Décision

1. **Scruter, mais par dossier.** Un dossier n'est relu que si sa date de modification a changé, ou si elle était récente au dernier passage (un fichier ajouté dans la même seconde passerait sinon inaperçu), ou lors du passage complet périodique (10 min). Seuls les fichiers **pas encore complets** sont regardés un par un, à chaque passage ; ceux qui viennent d'être complétés le sont encore cinq minutes, au cas où ils changeraient.
2. **Un fichier est complet quand trois conditions tiennent** (`live/completeness.py`) : taille et date inchangées sur deux passages espacés d'au moins `min_stable` (10 s) ; contrôle d'intégrité du format réussi (les lecteurs refusent déjà les fichiers tronqués : sections MRC annoncées, chaîne de pages TIFF/EER, fractions EER annoncées, marqueur de fin JPEG, XML bien formé) ; et, pour une acquisition EPU, son `.xml` présent et complet. Sans XML au bout de deux minutes, l'image est acceptée avec un avertissement : les deux premières conditions ne sont jamais levées.
3. **L'unité affichée est l'exposition**, pas le fichier : l'image intégrée, son film, son aperçu et ses métadonnées portent le même nom à un suffixe près (`live/layout.py`). La vignette vient de l'image, sinon du film, sinon de l'aperçu.
4. **Première rencontre d'une arborescence.** Les dossiers au repos depuis plus de 15 minutes sont indexés sur leur seul listing, sans `stat` par fichier ; leurs expositions sont marquées *non vérifiées*. « Au repos » se mesure par rapport à la date la plus récente trouvée dans l'arborescence elle-même, donc sur l'horloge du serveur de fichiers : celle de ce serveur peut en différer de plusieurs minutes. Ce qui arrive ensuite passe toujours le test complet.
5. **Tout l'état est dans un fichier SQLite** (`live/index.py`) : surveillances, dossiers, fichiers, expositions, vignettes. Un passage est une transaction ; s'il échoue, la base est comme avant et le scrutateur recharge sa mémoire depuis elle. Un serveur redémarré fait de même.
6. **Suivi par numéro de séquence.** Chaque changement d'une exposition reçoit le numéro suivant. Le client demande « ce qui a changé depuis N » (`/items?since=N`) toutes les trois secondes et ne reçoit que cela ; une exposition supprimée est annoncée.
7. **Une surveillance appartient à un utilisateur** ; le scrutateur, lui, est partagé par dossier et tourne tant qu'au moins une surveillance active le désigne. Arrêter la sienne ne touche pas celle des autres. À chaque requête, le dossier surveillé est résolu de nouveau pour l'utilisateur courant : perdre l'accès à une racine fait perdre l'accès à son index.
8. **Le temps est un argument** du scrutateur (`cycle(now)`), jamais lu dans l'horloge : les tests rejouent des heures d'acquisition en secondes avec un microscope simulé (`live/simulator.py`) qui sait quand chaque fichier a réellement reçu son dernier octet.
9. **Les requêtes qui modifient** (démarrer, arrêter une surveillance) sont refusées quand un autre site les envoie (`Origin`, `Sec-Fetch-Site`) : l'identité vient du proxy, pas d'un secret de la page.

10. **Alertes : des canaux nommés par l'administrateur, des règles par surveillance.** Les adresses (sujet ntfy, webhook, relais de messagerie) sont dans la configuration du serveur ; l'utilisateur choisit un canal par son nom et ne peut pas faire écrire le serveur ailleurs. Les règles sont jugées juste après chaque passage du scrutateur. Chaque condition est une petite machine à états conservée dans l'index : un message quand elle commence, un quand elle cesse, rien entre les deux, et rien de répété après un redémarrage.

## Conséquences

- Indexer 50 100 fichiers d'une session terminée coûte un listing par dossier et un `stat` par fichier du seul dossier récent (0,9 s sur disque local) ; un passage au repos coûte un `stat` par dossier (10 ms pour 102 dossiers).
- La latence de détection est celle du passage (5 s) plus le délai de stabilité (10 s) : une image complète apparaît 10 à 27 s après son dernier octet. Sur NFS, le cache d'attributs du client (`acdirmin`, `acregmin`) s'y ajoute ; monter le dossier surveillé avec `actimeo=1` le réduit.
- Un fichier complété depuis plus de cinq minutes n'est plus regardé : s'il change ensuite, l'index ne le sait pas. La visionneuse et les vignettes, elles, identifient toujours le contenu par sa taille et sa date.
- Les expositions *non vérifiées* d'une vieille session peuvent contenir un fichier resté tronqué : il est signalé à l'ouverture, pas à l'indexation.
- Le débit affiché ne compte que ce qui est arrivé pendant la surveillance : une session ouverte après coup n'a pas de « débit ».

- Une alerte part au plus un passage après le franchissement de son seuil. Le silence se compte sur l'horloge de ce serveur (le moment où le scrutateur a accepté la dernière image), pas sur celle du microscope.

## Alternatives écartées

- **Laisser l'utilisateur saisir l'adresse de ses alertes** : le serveur enverrait alors des requêtes vers une adresse choisie par un utilisateur, y compris à l'intérieur du réseau du labo.

- **inotify / fanotify** : aveugles aux écritures venues d'autres clients NFS ou SMB. Utilisables seulement si le serveur tourne sur le serveur de fichiers lui-même ; ce pourra être une optimisation, pas le fonctionnement de base.
- **Un `stat` par fichier à chaque passage** (chokidar) : 17 000 appels par seconde pour 50 000 fichiers à 3 s d'intervalle.
- **WebSocket pour pousser les changements** : prévu par le plan, remplacé par l'interrogation par numéro de séquence, qui donne les mêmes deltas sans connexion longue à faire passer par le reverse proxy ni état par client. Un WebSocket pourra s'y ajouter comme simple signal « il y a du nouveau ».
- **Se fier à l'horloge de ce serveur** pour décider qu'un dossier est au repos : un écart d'horloge de quelques minutes avec le serveur de fichiers ferait prendre des fichiers en cours d'écriture pour des fichiers anciens.

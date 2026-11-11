# ADR 011 : programmes externes (MotionCor3, CTFFIND) lancés par le serveur

**État :** acceptée (M7, 11 novembre 2026). **Jamais essayé avec les vrais programmes** : ni MotionCor3, ni CTFFIND, ni carte graphique n'étaient disponibles. Ce qui est testé est tout ce qui les entoure, contre des programmes de remplacement.

## Contexte

Le mode Live estime déjà le défocus de chaque image, vite et sans astigmatisme (`proc/ctf.py`). Pour l'astigmatisme et le mouvement du film, les programmes de référence existent : il faut les lancer, pas les réécrire. Lancer un programme depuis un serveur web est ce qui a donné l'exécution de commande à distance de l'ancien CryoViewer Live (`exec` d'une chaîne passée à un shell). La conception part de là.

## Décisions

### Une liste d'arguments déclarée par l'administrateur, jamais une ligne de commande

`[processing.motion]` et `[processing.ctf]` donnent chacun `argv` : le chemin absolu du programme, puis ses arguments, un par élément. Le serveur appelle `execve` avec cette liste. Il n'y a ni shell, ni découpage, ni recherche dans `PATH`.

Un argument peut nommer des **jokers**, dans un ensemble fermé : `{movie}`, `{image}`, `{out}`, `{apix}`, `{kv}`, `{cs}`, `{amp}`, `{dose}`, `{frames}`. Un joker est remplacé à l'intérieur de son argument, qui reste un seul argument quoi que contienne la valeur. Les jokers inconnus, les accolades isolées et les chemins relatifs sont refusés au démarrage.

Un utilisateur ne fournit rien de tout cela : il demande, ou non, que les programmes tournent sur sa session.

### Les noms de fichiers sont des données hostiles

Les réponses de CTFFIND passent par son entrée standard, une par ligne. Un nom de fichier contenant un saut de ligne y ajouterait une réponse. Toute valeur portant un caractère de contrôle arrête donc l'étape avant le lancement, avec un motif lisible. Les chemins sont toujours absolus : un nom commençant par un tiret ne peut pas passer pour une option.

### Un dossier par exécution, rien n'en sort que des nombres

Chaque exécution reçoit un dossier neuf (`{out}`, mode 0700) sous `work/` du dossier d'état, supprimé à la fin, y compris après un échec ; ceux qu'un serveur tué a laissés sont supprimés au démarrage. Le fichier de résultat doit se trouver dans ce dossier une fois les liens suivis : un programme qui y dépose un lien vers un autre fichier n'obtient pas qu'il soit lu. Les fichiers de résultat sont lus jusqu'à 1 Mio.

La somme alignée sert à l'étape CTF puis disparaît avec le dossier : **la somme pondérée par la dose n'est pas conservée**. La garder demanderait un dossier d'écriture borné et autorisé comme une racine ; ce n'est pas fait.

### Un programme ne peut ni bloquer ni faire tomber le serveur

- Chaque programme tourne dans sa propre session de processus. À l'échéance (`timeout`), ou quand le serveur s'arrête, tout le groupe reçoit `SIGKILL`, enfants compris ; de même après une fin normale, pour ce qu'il aurait laissé tourner.
- Sa sortie va dans un fichier et ses réponses viennent d'un fichier : aucun des deux côtés n'attend l'autre sur un tube plein.
- Un échec (code de retour, échéance, résultat illisible) est **enregistré** pour l'exposition, avec un motif court : elle n'est pas retentée indéfiniment. L'autre étape tourne quand même si elle a de quoi.
- L'utilisateur lit un état (« le programme s'est terminé avec le code 139 »), jamais la sortie du programme ni un chemin ; la sortie va au journal du serveur.
- L'environnement du programme est celui du serveur sans ses secrets (`IMAGEBIO_*`, mot de passe du relais de messagerie).

### À tour de rôle

`slots` programmes tournent à la fois (un par carte graphique). Le suivant est choisi en faisant tourner les **utilisateurs**, puis, pour chacun, ses sessions ; dans une session, l'image la plus récente d'abord, parce qu'un suivi sert à savoir ce qui se passe maintenant. Celui qui suit huit sessions, ou dont la session a mille films en attente, ne prive personne.

Seules les images **arrivées pendant le suivi** sont traitées : une session trouvée déjà écrite n'est pas reprise. Avec une étape d'alignement, une image attend son film deux minutes au plus ; si le film arrive plus tard, elle est traitée de nouveau.

### Les résultats passent par le scrutateur

Comme les mesures (ADR 007), un résultat est remis au scrutateur du dossier, qui l'inscrit à son prochain passage avec un nouveau numéro de séquence, sauf si le fichier a changé entre-temps. L'index passe au schéma 7 (`exposures.processing`, `watches.processing`).

### Ce que lisent les analyseurs

- CTFFIND : le fichier `<sortie>.txt`. Les colonnes sont retrouvées par leur nom dans l'en-tête, ce qui couvre les versions 4 et 5 (inclinaison, épaisseur) ; sans en-tête, les sept premières colonnes, communes aux deux.
- Mouvement : la table `global_shift` d'un fichier STAR à la manière de RELION, ou le journal à trois colonnes de MotionCor. Le serveur en tire le chemin total, celui des premières frames et le plus grand pas.

Ces formats sont ceux des publications et de ce que lit RELION ; ils sont vérifiés sur des fichiers écrits à ces formats, pas sur des sorties réelles.

## Conséquences

- Avant toute mise en service : lancer chaque programme à la main une fois, puis ajuster `argv`, les réponses de CTFFIND (elles changent d'une version à l'autre) et le nom du fichier de résultat. `deploy/processing.example.toml` est un point de départ, pas une configuration validée.
- La taille de pixel donnée aux programmes est celle de l'image intégrée. Un film en super-résolution demande de l'écrire en dur dans `argv`, comme la référence de gain.
- Un programme peut remplir le disque pendant qu'il tourne : rien ne borne ce qu'il écrit dans son dossier avant sa fin. À surveiller.
- Dans la pile Docker, les programmes, leurs bibliothèques et l'accès à la carte graphique doivent être ajoutés à l'image : ce n'est pas fourni.
- Windows n'est pas pris en charge pour cette partie (groupes de processus POSIX).

## Alternatives écartées

- **Une file de tâches (Celery, Redis) et des workers séparés.** Justifiée par plusieurs machines de calcul ; pour un serveur à une ou deux cartes, des fils du serveur suffisent et il n'y a rien de plus à exploiter.
- **Piloter cryoSPARC Live ou un schéma RELION.** C'est le bon outil pour traiter ; ici il s'agit de suivre. Leurs sorties se lisent déjà (pointés, classes).
- **Un modèle de ligne de commande** (`"MotionCor3 -InMrc {movie} ..."`). Plus court à écrire, et c'est exactement la forme qui se découpe mal dès qu'un chemin contient une espace.

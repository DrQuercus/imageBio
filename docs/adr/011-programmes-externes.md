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

Le nom revient aussi par les fichiers de résultat : CTFFIND cite son fichier d'entrée dans un commentaire. Seule la ligne qui commence par « Columns: » nomme les colonnes, et leurs numéros sont bornés.

Entre deux passages du scrutateur, qui écrit dans le dossier peut remplacer une image indexée par un lien vers le fichier de quelqu'un d'autre, ou par un tube nommé. Avant chaque usage, le serveur vérifie donc que le fichier est toujours un fichier ordinaire atteint sans lien ; sinon il ne le donne à aucun programme et attend le passage suivant. Il reste l'instant entre cette vérification et l'ouverture par le programme, qu'un serveur ne peut pas fermer pour un programme qui ouvre par chemin.

Une taille de pixel hors de 0,01 à 1000 Å dans un fichier de métadonnées n'est pas crue : celle de l'en-tête de l'image sert à sa place.

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

Comme les mesures (ADR 007), un résultat est remis au scrutateur du dossier, qui l'inscrit à son prochain passage avec un nouveau numéro de séquence. Il est écarté si l'image **ou le film** dont il a été tiré a changé entre-temps (film arrivé, réécrit ou disparu) : l'image est alors traitée de nouveau. L'index passe au schéma 7 (`exposures.processing`, `watches.processing`).

Un résultat est vérifié au moment où il est remis : un nombre non fini est refusé là, pas au milieu d'un passage du scrutateur, qui échouerait alors à chaque passage suivant.

Un programme **introuvable ou non exécutable** n'est pas un échec de l'image : rien n'est inscrit contre elle, les lancements s'arrêtent une minute, et `imagebio serve` refuse de démarrer tant que le chemin déclaré ne mène pas à un programme.

### Ce que lisent les analyseurs

- CTFFIND : le fichier `<sortie>.txt`. Les colonnes sont retrouvées par leur nom dans l'en-tête, ce qui couvre les versions 4 et 5 (inclinaison, épaisseur) ; sans en-tête, les sept premières colonnes, communes aux deux.
- Mouvement : la table `global_shift` d'un fichier STAR à la manière de RELION, ou le journal à trois colonnes de MotionCor. Le serveur en tire le chemin total, celui des premières frames et le plus grand pas.

Ces formats sont ceux des publications et de ce que lit RELION ; ils sont vérifiés sur des fichiers écrits à ces formats, pas sur des sorties réelles.

## Relecture indépendante

Ce code a été relu par un agent chargé d'y trouver des défauts réels, chacun reproduit. Il en a trouvé neuf, tous corrigés avec un test de régression qui échoue sur l'ancien code :

| Gravité | Défaut | Correction |
|---|---|---|
| haute | Une taille de pixel absurde dans un XML donnait un mouvement infini, que le scrutateur ne pouvait pas inscrire : il échouait ensuite à chaque passage, pour tout le dossier | bornes sur la taille de pixel, nombres non finis refusés par l'analyseur et à la remise |
| moyenne | Un nom de fichier contenant « columns #600000000 », cité par CTFFIND dans son résultat, faisait allouer des gigaoctets | seule la ligne « Columns: » compte, numéros bornés |
| moyenne | Une image indexée remplacée par un lien (résultat tiré du fichier d'un autre) ou par un tube nommé (fil bloqué pour toujours) | fichier ordinaire sans lien exigé avant chaque usage, aussi pour les mesures et les métadonnées |
| moyenne | Résultat inscrit alors que le film avait changé pendant l'exécution | le résultat porte l'identité du film |
| moyenne | Un film jamais terminé empêchait pour toujours le CTF de son image | attente bornée à deux minutes ; aucune attente sans étape d'alignement |
| moyenne | Au démarrage, tous les dossiers `run-*` de `work_dir` étaient supprimés, ceux d'autres programmes compris | un sous-dossier propre à chaque serveur |
| basse | Traitement coupé puis rétabli pendant une exécution : la même image lancée deux fois | une exécution en cours reste connue |
| basse | Programme impossible à tuer (autre compte) : attente sans fin | attente bornée, puis abandon signalé au journal |
| basse | Chemin de programme erroné : toutes les images marquées en échec, jamais reprises | voir ci-dessus |

## Conséquences

- Avant toute mise en service : lancer chaque programme à la main une fois, puis ajuster `argv`, les réponses de CTFFIND (elles changent d'une version à l'autre) et le nom du fichier de résultat. `deploy/processing.example.toml` est un point de départ, pas une configuration validée.
- La taille de pixel donnée aux programmes est celle de l'image intégrée. Un film en super-résolution demande de l'écrire en dur dans `argv`, comme la référence de gain.
- Un programme peut remplir le disque pendant qu'il tourne : rien ne borne ce qu'il écrit dans son dossier avant sa fin. À surveiller.
- Un programme qui détache volontairement un enfant (nouvelle session) le soustrait à l'arrêt du groupe : le serveur ne le rattrape pas. MotionCor3 et CTFFIND ne le font pas.
- Dans un conteneur, le service tourne sous un petit `init` (`init: true` dans la pile fournie) : sans lui, les orphelins d'un programme tué resteraient en zombies.
- `work_dir` peut être un disque de travail partagé : le serveur n'y touche que son propre sous-dossier `imagebio-runs-…`.
- Dans la pile Docker, les programmes, leurs bibliothèques et l'accès à la carte graphique doivent être ajoutés à l'image : ce n'est pas fourni.
- Windows n'est pas pris en charge pour cette partie (groupes de processus POSIX).

## Alternatives écartées

- **Une file de tâches (Celery, Redis) et des workers séparés.** Justifiée par plusieurs machines de calcul ; pour un serveur à une ou deux cartes, des fils du serveur suffisent et il n'y a rien de plus à exploiter.
- **Piloter cryoSPARC Live ou un schéma RELION.** C'est le bon outil pour traiter ; ici il s'agit de suivre. Leurs sorties se lisent déjà (pointés, classes).
- **Un modèle de ligne de commande** (`"MotionCor3 -InMrc {movie} ..."`). Plus court à écrire, et c'est exactement la forme qui se découpe mal dès qu'un chemin contient une espace.

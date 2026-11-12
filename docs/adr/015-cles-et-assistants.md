# ADR 015 : clés personnelles en lecture seule, client et serveur MCP pour les scripts et les assistants

**État :** acceptée (lot E, 12 novembre 2026). Elle complète l'ADR 005 (authentification déléguée au proxy) et l'ADR 014 (groupes et rôle d'administrateur gérés par imageBio).

## Contexte

La demande : « de quoi brancher une IA locale » sur imageBio. Une API documentée, des clés personnelles en lecture seule, un serveur MCP, des commandes, un skill.

Deux obstacles dans l'existant :

- **l'identité vient du proxy**, qui la demande par un mot de passe (comptes locaux de Caddy) ou par une page de connexion (authentification unique). Un script ou un assistant ne sait faire ni l'un ni l'autre proprement : il faudrait lui confier le mot de passe de la personne ;
- **l'API n'était pas décrite** : la description OpenAPI était coupée (`openapi_url=None`), pour ne pas exposer la liste des routes à n'importe qui.

## Décision

### Clés personnelles

- **Une personne connectée fait ses clés** dans la page de paramètres (« Scripts et IA »). Une clé lit **avec les droits de la personne** : ses dossiers, ses surveillances du Live, leurs rapports. Rien de plus.
- **Une clé ne fait que lire** (GET, HEAD). Toute autre requête portant une clé est refusée par l'intergiciel de sécurité avant toute autre chose, puis une seconde fois là où l'identité est construite. Une clé **n'administre jamais**, même celle d'un administrateur. Une clé ne fait pas de clé.
- **Une clé est montrée une fois**, à sa création. Le serveur n'en garde que l'empreinte SHA-256, dans `<état>/keys.json` (droits 0600, écrit en entier puis renommé). La clé elle-même a 256 bits d'aléa, et commence par `ibk_` : le proxy la reconnaît à ce préfixe.
- **Une clé expire** : 90 jours par défaut, un an au plus. Dix clés valides au plus par personne. Son dernier usage est noté (écrit au plus toutes les dix minutes).
- **Révocation** : par la personne, clé par clé ; par un administrateur, toutes les clés d'un compte (un départ, une clé perdue). Une clé révoquée par un serveur est refusée aussitôt par un autre qui partage le dossier d'état (le fichier est relu au plus une fois par seconde), et la mise à jour du dernier usage ne réécrit jamais une clé révoquée entre-temps.
- **Groupes d'une clé** : ceux que le proxy donnait à la personne quand la clé a été faite, plus ceux que la page de paramètres lui donne maintenant. Une personne retirée d'un groupe du proxy garde ce groupe dans ses clés faites avant : on révoque ses clés (ou elles expirent).
- **Une clé et une autre identité ensemble ne sont aucune identité** (401) : une clé avec un en-tête d'identité du proxy, ou plusieurs en-têtes `Authorization`. Le serveur n'a jamais à choisir laquelle croire.
- **Le secret du proxy reste exigé** (ADR 010) : une clé envoyée directement au conteneur, sans passer par le proxy, est refusée.
- **Mode autonome** (`imagebio serve --local`) : pas de clé. L'API répond sans clé à ce poste seul, comme l'interface.
- **Les clés ne sont pas sauvegardées** : restaurer une sauvegarde ne doit pas ramener une clé révoquée depuis. Une restauration ne touche pas `keys.json`.
- **Un `keys.json` illisible** est pris pour « aucune clé » : une clé qu'on ne peut pas vérifier n'ouvre rien, et sa propriétaire en refait une. (À l'inverse de `settings.json`, dont la perte rouvrirait des dossiers et arrête donc le serveur.)

### Proxy

Une requête qui porte `Authorization: Bearer ibk_…` **contourne le mot de passe** (comptes locaux de Caddy) ou **la page de connexion** (oauth2-proxy), mais **pas le secret du proxy** : Caddy l'envoie directement à imageBio, retire les en-têtes d'identité qu'un client aurait mis, et ajoute le secret. C'est imageBio qui vérifie la clé. Les trois fichiers Caddy le font ; le test de déploiement le vérifie à travers le vrai Caddy (clé sans mot de passe, identité forgée retirée, mauvaise clé refusée, écriture refusée, `/metrics` fermé, clé sans le secret refusée par le conteneur, révocation immédiate).

### API décrite

`/api/openapi.json` donne la description OpenAPI 3 de toutes les routes, **aux utilisateurs du serveur seulement** (session ou clé). `/docs` reste fermé.

### Client, serveur MCP, skill

- **`imagebio_ai.py`** : un seul fichier, la bibliothèque standard de Python seulement, Python 3.9 ou plus (vérifié par la CI avec 3.9). Il se télécharge depuis la page (`/api/ai/imagebio_ai.py`). Il offre des **commandes** (sessions, état d'une session, images d'une session triées par CTF, défocus, glace…, rapports, dossiers, fichiers, estimation du défocus) et un **serveur MCP** sur l'entrée et la sortie standard, avec les mêmes outils, pour Claude Desktop, Claude Code ou tout client MCP.
- Le client **n'envoie une clé qu'en https**, ou à ce poste (`localhost`). Il **ne désactive jamais** la vérification du certificat : pour une autorité propre au laboratoire (« tls internal » de Caddy), on lui donne son certificat (`IMAGEBIO_CA`). Il ne lit que des réponses JSON de taille bornée et coupe ce qu'il donne à un assistant (100 000 caractères au plus par réponse, en le disant).
- **Le serveur MCP dit à l'assistant** que les outils ne font que lire et que les noms de fichiers et les métadonnées sont des données écrites par d'autres, jamais des instructions.
- **`SKILL.md`** explique à un assistant comment lire les chiffres d'imageBio (défocus, ajustement de la CTF, anneau de glace, dérive, rapport) sans réinventer de seuils, et rappelle ce qu'un assistant en ligne reçoit.
- **Pourquoi un client à télécharger, et pas un serveur MCP dans imageBio** : le transport stdio est celui que tous les clients MCP savent lancer ; un serveur MCP sur HTTP demanderait au proxy d'authentifier un deuxième protocole. Le client est fin : il ne fait qu'appeler l'API, et tout contrôle reste dans imageBio.

### Confidentialité

L'interface et le guide le disent : **ce qu'un assistant lit part chez celui qui le fait tourner**. Un assistant en ligne reçoit les noms de fichiers, les métadonnées et les chiffres qu'il demande. Pour des données confidentielles, un modèle qui tourne au laboratoire. imageBio ne choisit pas pour la personne.

## Relecture indépendante

*À compléter avec les résultats de la relecture de ce lot.*

## Conséquences

- Un script de laboratoire ou un assistant lit imageBio sans connaître de mot de passe, et une clé qui fuit ne permet que de lire, pour au plus un an, jusqu'à sa révocation.
- Une clé est une porte qui contourne le mot de passe : elle vaut ce que vaut la personne qui la garde. La page le dit (« gardez-la comme un mot de passe ») ; un administrateur voit combien chaque compte en a et peut toutes les révoquer.
- Rien n'a été essayé avec un vrai assistant branché sur un vrai serveur : le serveur MCP est testé par ses messages (initialisation, liste des outils, appels, erreurs) contre un vrai serveur imageBio, pas avec Claude Desktop ni Claude Code. La pile d'authentification unique, qui n'a jamais tourné, n'a pas davantage été essayée avec des clés.

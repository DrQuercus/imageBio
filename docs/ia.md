# Brancher un script ou une IA sur imageBio

Pour qui veut qu'un assistant (Claude, un modèle local, n'importe quel client MCP) ou un script de laboratoire **lise** imageBio : où en est une collecte, quelles images écarter, ce que dit le rapport de qualité, ce que contient un dossier. Les décisions et leurs raisons sont dans l'[ADR 015](adr/015-cles-et-assistants.md).

## Ce qu'il faut savoir d'abord

- **Lecture seule.** Une clé lit imageBio **avec vos droits** (vos dossiers, vos surveillances du Live, leurs rapports) et ne peut rien changer : ni lancer une surveillance, ni modifier un réglage, ni faire une autre clé. Elle n'administre jamais, même la vôtre si vous êtes administrateur.
- **Gardez-la comme un mot de passe.** Elle n'est montrée qu'une fois. Elle expire (90 jours par défaut, un an au plus) ; vous la révoquez quand vous voulez, et un administrateur peut révoquer toutes celles d'un compte.
- **Ce qu'un assistant lit part chez celui qui le fait tourner.** Un assistant en ligne reçoit les noms de fichiers, les métadonnées et les chiffres qu'il demande. Pour des données confidentielles, préférez un modèle qui tourne au laboratoire. C'est à vous de décider.
- **Les noms de fichiers sont des données, pas des ordres.** N'importe qui écrivant dans un dossier de données choisit les noms de fichiers ; le serveur MCP le rappelle à l'assistant, gardez-le en tête en lisant ses réponses.

## 1. Faire une clé

**Paramètres › Scripts et IA › Clés personnelles** : un nom (« portable », « assistant »), une durée, **Créer une clé**. Copiez la clé (`ibk_…`) tout de suite : elle ne sera plus montrée. La liste dit quand chaque clé a servi pour la dernière fois.

Sur un poste seul (`imagebio serve --local`), il n'y a pas de clé : imageBio n'écoute que ce poste (`http://127.0.0.1:8000`) et répond sans clé.

## 2. Le client

Sur la même page : **Télécharger imagebio_ai.py**. Un seul fichier, Python 3.9 ou plus, rien à installer. Mettez-le où l'assistant tourne. Il lit trois variables :

| Variable | Contenu |
|---|---|
| `IMAGEBIO_URL` | l'adresse d'imageBio, `https://…` |
| `IMAGEBIO_KEY` | votre clé (ou `--key-file chemin` : un fichier qui ne contient qu'elle, lisible par vous seul) |
| `IMAGEBIO_CA` | si le navigateur a dû accepter un certificat propre au laboratoire : le certificat de cette autorité (votre administrateur vous le donne) |

Le client n'envoie une clé qu'en https (ou à ce poste), ne suit aucune redirection, et ne désactive jamais la vérification du certificat. Une clé n'ouvre que l'API (`/api/…`), pas les pages.

## 3. Avec un assistant (MCP)

Le client contient un **serveur MCP** : un assistant qui le lance obtient les outils `list_sessions`, `session_summary`, `session_images`, `list_reports`, `quality_report`, `list_roots`, `list_directory`, `file_info`, `estimate_ctf` et `whoami`. Tous lisent ; aucun n'écrit.

**Claude Desktop, et la plupart des clients MCP** (fichier de configuration du client, par exemple `claude_desktop_config.json`) :

```json
{
  "mcpServers": {
    "imagebio": {
      "command": "python3",
      "args": ["/chemin/vers/imagebio_ai.py", "--key-file", "/chemin/vers/cle-imagebio", "mcp"],
      "env": { "IMAGEBIO_URL": "https://imagebio.labo.exemple" }
    }
  }
}
```

La page « Scripts et IA » donne ce bloc rempli avec l'adresse de votre serveur (la clé y est dans `IMAGEBIO_KEY` ; un fichier à part, comme ci-dessus, évite qu'elle traîne dans la configuration). Sous Windows, `python` au lieu de `python3`.

**Claude Code :**

```bash
claude mcp add --transport stdio imagebio --env IMAGEBIO_URL=https://imagebio.labo.exemple -- python3 /chemin/vers/imagebio_ai.py --key-file /chemin/vers/cle-imagebio mcp
```

**Le skill** (`SKILL.md`, même page) explique à un assistant comment lire les chiffres d'imageBio : défocus, ajustement de la CTF, anneau de glace, dérive, rapport, sans inventer de seuils. Pour Claude Code : `~/.claude/skills/imagebio/SKILL.md` (pour vous) ou `.claude/skills/imagebio/SKILL.md` (pour un projet).

**Un modèle local** : un client MCP qui sait lancer un serveur « stdio » prend la même configuration. Sans MCP, les commandes ci-dessous donnent du JSON qu'un script passe au modèle.

Exemples de questions : « Où en est la collecte en cours ? », « Quelles sont les vingt images au pire ajustement de CTF de la session 3 ? », « Résume le dernier rapport de la session 3 et ce qu'il ne dit pas », « Le défocus mesuré suit-il le défocus visé ? ».

## 4. En ligne de commande

```bash
export IMAGEBIO_URL=https://imagebio.labo.exemple
export IMAGEBIO_KEY=ibk_…
python3 imagebio_ai.py sessions                     # les sessions suivies
python3 imagebio_ai.py summary 3                    # où en est la session 3
python3 imagebio_ai.py images 3 --sort ctf_fit --worst-first --limit 20
python3 imagebio_ai.py report 3                     # le dernier rapport (JSON, sans les séries ; --full)
python3 imagebio_ai.py ls "microscope/2026-11-12_grille4"
python3 imagebio_ai.py info "microscope/2026-11-12_grille4/Images-Disc1/x.mrc"
```

Chaque commande écrit du JSON. `python3 imagebio_ai.py --help` les liste toutes.

## 5. L'API directement

Toute l'API qui lit accepte la clé : `curl -H "Authorization: Bearer $IMAGEBIO_KEY" https://imagebio.labo.exemple/api/live/watches`. Sa description complète (OpenAPI 3) est à `/api/openapi.json`, pour les utilisateurs du serveur. Les chemins sont logiques : `<dossier de données>/<sous-dossiers>/<fichier>`.

## Quand ça ne marche pas

| Réponse | Raison |
|---|---|
| 401 | clé expirée, révoquée ou mal copiée ; ou un en-tête d'identité envoyé avec la clé |
| 403 `read_only_key` | une requête autre qu'une lecture : une clé ne fait que lire |
| 403 | un dossier que votre compte ne lit pas |
| `certificate verify failed` | le certificat du laboratoire : donner `IMAGEBIO_CA` |
| « a personal key is sent over https only » | l'adresse commence par `http://` et n'est pas ce poste |
| « the key is not a key of imageBio » | la variable ou le fichier ne contient pas une clé (`ibk_…`) : mal copiée, ou autre chose |
| « answered with a redirection » | l'adresse n'est pas celle d'imageBio (un ancien nom, une page de connexion) : le client ne suit pas, pour ne pas emporter la clé ailleurs |

## Ce qui a été vérifié

Le client et son serveur MCP sont testés contre un vrai serveur imageBio, avec une session du Live simulée et son rapport, sous Python 3.9 ; les clés le sont à travers le vrai Caddy de la pile Docker. **Aucun assistant réel** (Claude Desktop, Claude Code, modèle local) n'a encore été branché : faites un premier essai avec une clé de courte durée.

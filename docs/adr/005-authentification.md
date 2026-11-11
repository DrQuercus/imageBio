# ADR 005 : authentification déléguée au reverse proxy, écoute en local

**État :** proposée. C'est la valeur par défaut du plan (`docs/PLAN.md` §8.2) ; le choix du fournisseur d'identité (LDAP, AD, comptes locaux) reste à faire.

## Décision

- `imagebio` **n'implémente pas** de gestion de mots de passe. Deux modes :
  - `auth.mode = "none"` : mode autonome (`imagebio serve --local DOSSIER`). Un seul utilisateur local, qui voit toutes les racines. Écoute sur une adresse de bouclage uniquement.
  - `auth.mode = "proxy"` : un reverse proxy (Caddy, nginx) termine TLS, authentifie l'utilisateur (oauth2-proxy ou Authelia, branché sur LDAP/AD) et transmet son identité dans des en-têtes (`X-Forwarded-User`, `X-Forwarded-Groups`, noms configurables).
- Le serveur **écoute sur une adresse de bouclage**. Il refuse de démarrer sur une autre adresse, sauf en mode `proxy` avec `server.trust_network = true` : ce réglage sert au cas où le proxy et l'application sont sur un réseau privé (conteneurs), et c'est l'exploitant qui s'en porte garant.
- L'en-tête `Host` est filtré : noms de bouclage, plus les noms publics déclarés dans `server.allowed_hosts`.
- Les droits de lecture se règlent par racine de données, avec une liste de groupes (ADR 003).

> **Complément (M8) :** « le proxy est le seul client possible » ne tient pas face aux comptes locaux de la machine. L'[ADR 010](010-exploitation.md) ajoute un secret partagé entre le proxy et le serveur.

## Conséquences

- Pas de mots de passe à stocker, pas de protection anti-force-brute à écrire : c'est le métier du proxy.
- Les en-têtes d'identité ne sont fiables que si le proxy est le seul client possible. C'est la raison de l'écoute en local.
- Le déploiement demande un proxy. Le jalon M8 fournit la pile complète (docker-compose).

## Alternative écartée

Une authentification Basic intégrée, comme dans les correctifs M0 des anciennes applis. Elle dépanne, mais sans TLS le mot de passe circule en clair, et elle ne connaît pas les groupes.

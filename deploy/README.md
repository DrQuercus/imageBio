# Déploiement

Mode d'emploi : [`docs/exploitation.md`](../docs/exploitation.md).

| Fichier | Rôle |
|---|---|
| `Dockerfile` | l'image : interface construite par Node, servie par le paquet Python |
| `compose/docker-compose.yml` | Caddy + imageBio, comptes locaux (`caddy/users`) |
| `compose/docker-compose.sso.yml` | à ajouter pour l'authentification unique (oauth2-proxy, OIDC) |
| `compose/env.example`, `compose/imagebio.toml` | réglages à copier et adapter |
| `compose/smoke-test.sh` | construit et lance la pile sur une machine vierge, puis la vérifie de l'extérieur (lancé par l'intégration continue) |
| `systemd/` | installation sans Docker : unité durcie, sauvegarde quotidienne, Caddyfile, configuration |

La pile avec comptes locaux est testée à chaque modification. La pile SSO et le Caddyfile de `systemd/` ne l'ont jamais été : voir la fin de la fiche d'exploitation.

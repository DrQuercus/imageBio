# Le rapport de qualité d'une collecte

Le mode Live écrit, pour une session suivie, un rapport de qualité : une page à lire et à ranger dans la bibliothèque des collectes, les mêmes chiffres pour un programme, et une ligne par image.

## Quand il est écrit

- **À la demande** : bouton « Rapport maintenant » du panneau « Rapport de qualité », ou `POST /api/live/watches/{id}/reports`.
- **À l'arrêt de la surveillance**, sauf si la case « à la fin de la surveillance » est décochée pour cette session.
- **Quand la collecte se tait** : une session qui a reçu des images et n'en reçoit plus depuis `[live] report_after_idle_min` minutes (120 par défaut, 0 pour jamais). Une fois par silence, vérifié chaque minute.

Les vingt derniers rapports d'une surveillance sont gardés dans `<dossier d'état>/reports/`. Oublier la surveillance les efface. Ils ne sont **pas** dans les sauvegardes (`imagebio backup` ne copie que l'index) : téléchargez ce que vous voulez garder.

## Les fichiers

| Fichier | Pour quoi |
|---|---|
| `report.html` | La page, en français : à lire, à imprimer (A4, ou « Imprimer → PDF » du navigateur), à glisser dans la bibliothèque. Un seul fichier : vignettes et graphiques sont dedans. Aucun script, rien n'est chargé d'ailleurs. |
| `report.json` | Tous les chiffres de la page, pour un programme ou une IA. Schéma ci-dessous. |
| `exposures.csv` | Une ligne par image acquise : ses mesures, son verdict, ses raisons. Une cellule qu'un tableur prendrait pour une formule (`=`, `+`, `-`, `@` en tête) est écrite comme du texte, précédée d'une apostrophe. |
| `report.zip` | Les trois. |

## Ce que dit la page

1. Les chiffres clés : images, débit moyen, part acceptée, médianes du CTF, du défocus, de la glace et du mouvement, part de glace cristalline.
2. **Ce que ces chiffres ne disent pas**, en premier : images non mesurées, illisibles, encore en cours d'écriture, non vérifiées ; CTF estimé rapidement (sans astigmatisme) ; glace relative ; tension qui ne vient pas des fichiers.
3. La collecte dans le temps : images et verdicts par tranche, pauses de plus de dix minutes, débit moyen et heure la plus chargée.
4. La qualité : seuils de la session, acceptées et rejetées, raisons des rejets.
5. Le CTF : défocus mesuré face au défocus visé, résolution de l'ajustement dans le temps, histogrammes, et le microscope avec lequel il a été estimé (tension, Cs, contraste d'amplitude, et d'où vient chaque valeur).
6. La glace et le mouvement.
7. **Les paramètres d'acquisition** lus dans les fichiers (tension, grandissement, taille de pixel, détecteur, fractions, temps d'exposition, dose, défocus visés, inclinaison, logiciel, microscope) : leurs valeurs, et quand un paramètre qui ne devrait pas bouger a changé.
8. La grille : un tableau par carré, et les positions de la platine colorées selon le verdict.
9. Les meilleures images et les rejetées, avec leurs vignettes et leurs raisons.
10. Les alertes annoncées pendant la surveillance, les programmes du serveur, et toutes les mesures en un tableau.

Le verdict de chaque image est celui que montrait l'interface : même calcul, vérifié des deux côtés sur les mêmes cas (`backend/tests/fixtures/verdict_cases.json`).

## Le microscope d'une session

Le CTF dépend de la tension : une image à 200 kV lue comme à 300 kV reçoit un défocus trop grand d'environ 27 %. Pour chaque image, la tension utilisée est, dans l'ordre :

1. celle du fichier d'acquisition (XML d'EPU) ;
2. celle réglée pour la session (panneau « Microscope », ou `PUT /api/live/watches/{id}/microscope?kv=&cs=&amp=`) ;
3. celle du dossier de données (`[[roots]] kv`, `cs_mm`, `amplitude_contrast` dans `imagebio.toml`) ;
4. celle du serveur (`[microscope]`).

Le Cs et le contraste d'amplitude suivent les mêmes règles à partir du point 2. Les valeurs appartiennent au dossier : tous ceux qui le suivent mesurent avec elles. Changer une valeur fait mesurer de nouveau les images qui en avaient utilisé une autre.

## Le schéma JSON (`imagebio.live-report`, version 1)

Les noms des champs sont en anglais, les unités sont dans les noms ou ci-dessous. Les temps sont en secondes depuis 1970 (UTC). `null` : inconnu ou non mesuré, jamais deviné.

| Champ | Contenu |
|---|---|
| `schema`, `version` | `"imagebio.live-report"`, `1`. Un champ ajouté ne change pas la version ; un champ changé ou retiré, si. |
| `generated_at`, `generated_by`, `trigger` | Quand, par quelle version, pourquoi (`manual`, `stop`, `idle`). |
| `session` | `path` (chemin logique), `watch_id`, `watching_since`, `stopped_at`, `first` et `last` (première et dernière image), `duration_s`. |
| `headline` | `exposures`, `rate_per_hour`, `accepted_share`, `median_ctf_fit` (Å), `median_defocus` (µm), `median_ice`, `median_motion` (Å), `ice_ring_share`. |
| `counts` | `exposures`, `incoming`, `measured`, `unreadable`, `processed`, `arrived_while_watched`, `views` (atlas, carrés, trous), `flags` (`ice_ring`, `dark`, `empty`). |
| `rate` | `per_hour_mean`, `per_hour_peak`, `pauses` (`start`, `end`, `seconds`). |
| `timeline` | `bucket` (s), `start`, `bins` : `[images, acceptées, rejetées]` par tranche. |
| `acquisition` | Par paramètre : `unit`, `kind` (`discrete`, `continuous`, `targets`), `known`, `missing`, et `values` (`value`, `count`, `first`, `last`), `distinct`, `constant`, `changes` (`at`, `from`, `to`), `change_count` ; ou `stats` pour un paramètre continu. |
| `ctf_settings` | `kv`, `cs_mm`, `amplitude` (`value`, `source` : `session`, `root`, `server`) et `kv_used` (`kv`, `from`, `count`), la tension réellement utilisée image par image (`from` : `xml` ou l'une des sources). |
| `ice` | `reference`, `calibrated`, `mfp_nm`, `ring_limit`. |
| `thresholds` | Les seuils de la session (`fit_max`, `ice_max`, `ring_max`, `drift_max`, `defocus_min`, `defocus_max`). |
| `quality` | `judged`, `accepted`, `rejected`, `accepted_share`, `reasons` (`fit`, `ice`, `ring`, `drift`, `defocus`) ; `null` sans seuil. |
| `distributions` | `ctf_fit`, `defocus`, `defocus_error`, `astigmatism`, `ice`, `ice_nm`, `ice_ring`, `motion`, `early_motion`, `ctf_thickness`, `mean_counts` : `n`, `min`, `p10`, `median`, `p90`, `max`, `mean`, `histogram` (`edges`, `counts`, `below`, `above`). |
| `series` | `defocus`, `ctf_fit`, `ice`, `motion` : `[temps, valeur, verdict]` (`a`, `r` ou vide), 1500 points au plus ; `target_defocus` : `[temps, valeur]`. |
| `squares`, `square_count` | Par carré de grille : `name`, `exposures`, `accepted`, `rejected`, `first`, `median_fit`, `median_defocus`, `median_ice`, `stage`. |
| `stage` | `[x, y, verdict]` en µm. |
| `best`, `worst` | Six images chacune : `id`, `name`, `path`, `time`, `verdict`, `why`, `fit`, `defocus`, `ice`, `flags`. |
| `alerts` | `condition`, `started`, `ended`, `seconds`. |
| `programs` | Les programmes déclarés sur le serveur, s'ils sont demandés, combien d'images traitées. |
| `limits` | Ce que le rapport ne peut pas dire : `code`, `count`, `text` (en français). |

`exposures.csv` a pour colonnes : `name`, `path`, `time`, `grid_square`, `verdict`, `reasons`, `flags`, `mean_counts`, `ice`, `ice_nm`, `ice_ring`, `ctf_defocus_um`, `ctf_fit_A`, `ctf_astigmatism_um`, `ctf_source` (`program` ou `estimate`), `target_defocus_um`, `motion_A`, `dose`, `pixel_size_A`, `stage_x_um`, `stage_y_um`, `verified`.

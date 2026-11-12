# imageBio: notes for development sessions

Network-deployed scientific image application (web): a viewer, a Cryo mode and a Live mode (real-time monitoring of a cryo-EM collection). The ImageJ-style editor of the first plan was dropped (ADR 009): no ROI manager or measurements; "Ouvrir dans Fiji" exports a calibrated ImageJ TIFF instead.

- **Read first:** `docs/PLAN.md` (target architecture, milestones M0-M8, acceptance criteria; M5 and M6 withdrawn, all the others written, nothing tried on real data yet), then `docs/audit/AUDIT.md` (verified bugs in the existing code, with line numbers) and `docs/research/` (formats, EER, EPU/SerialEM metadata, live tools).
- **Existing code:** `legacy/cryoviewer/` (Python) and `legacy/cryoviewer-live/` (Node/React; `npm ci && npm run build`, `node_modules/` and `dist/` are not versioned). Only M0 fixes go in there; the new code lives in `backend/` and `frontend/`.
- **Stack:** Python backend (FastAPI), React + TypeScript frontend (WebGL2 canvas). The core stays MIT: GPL code (Bio-Formats, should it come back) only in a separate worker process.

## Non-negotiable rules (from the audit)

- No absolute path from the client: configured data roots, `realpath` confinement, symlinks resolved inside the root.
- No shell calls (`shell=True`, `exec` from child_process, string-built commands).
- One single decoder (Python). Each reader is compared bit-exact against `mrcfile`/`tifffile` on a generated fixture.
- Downsampling = binning or Fourier cropping, never decimation. Recompute Å/px at the displayed level and keep its provenance (header / extended header / XML / default + visible warning).
- Caches bounded in bytes, keyed on (path, size, mtime).
- Robust, NaN-safe statistics. Client requests are "latest wins" with cancellation.
- Per-user/per-session state, never a global singleton. Escape all UI output (no `innerHTML` with file names).
- Every fixed bug gets a regression test.

## Backend (`backend/`, package `imagebio`)

- Read `backend/README.md` and `docs/adr/`. Contract: `core/model.py` (Dataset, calibration provenance), `core/errors.py`, `io/registry.py` (Reader).
- Checks before every commit, from `backend/`: `python -m pytest`, `ruff check src tests`, `ruff format --check src tests`, `python -m mypy` (strict). Performance budgets: `python -m pytest -m perf`.
- CI also runs on Python 3.11, where pip installs an older numpy whose stubs type some calls as `Any` (twice a CI-only mypy failure). To reproduce: `python3.11 -m venv V && V/bin/pip install -e ".[dev]" && V/bin/python -m mypy`.
- Public error messages never contain a server path. Clients only use logical paths (`<root>/<relative>`); authorisation happens on every request, before any cache lookup.
- Tests compare against a reference (mrcfile, tifffile, Pillow, a naive loop), never against imagebio itself.
- Cryo processing (ADR 006) happens on the server, on the full-resolution plane, in a fixed order: frame mean, gain, filters, then crop and bin (`service/planes.py`, `Processing`). Exception (ADR 013): a filtered view at bin > 1 is reduced by Fourier cropping, then filtered at the reduced pitch (the two commute; `tests/service/test_reduced_filter.py` keeps them in step); never filter block means. Every new processing parameter must be part of `Processing.key()`: it feeds the cache keys and the ETag. A change in what a plane request returns bumps the version number in its ETag tag (`api/app.py`).
- A gain reference is a logical path, authorised like any file. Its orientation is checked and reported (`cryo/gain.py`), never corrected silently.
- Particle coordinates (ADR 008, `cryo/picks.py`): the server returns centres in pixels of the stored image, rows counted from the first row, and never flips anything; the interface has the switch. STAR files are read line by line (`cryo/star.py`), `.cs` files never unpickled, answers bounded and saying what they left out.
- What cannot be applied correctly is reported, not ignored (the defect list of a `.gain` file, `listed_defects`); what cannot be measured reliably is left out, not guessed (ice ring on a small field, drift on noise, "empty hole" without a calibrated I0).
- Live mode (ADR 007, `live/`): the scanner takes time as an argument (`cycle(now)`) and never reads the clock; tests drive it with `live/simulator.py`, which knows when each file really got its last byte. "At rest" is judged against the newest date found in the tree (the file server's clock), not this server's. Never mark a file complete on a stable size alone: integrity check too. Every change to an exposure bumps its `seq`.
- Routes that change something (POST, PUT, DELETE) are refused when another site sends them (`SecurityMiddleware`); a watch belongs to its user and its directory is re-authorised on every request.
- Alert destinations (`live/alerts.py`) come from the server configuration only: a user picks a channel by name, never gives an address, and never gets one back. A changed index schema bumps `SCHEMA_VERSION` with a migration in `live/index.py`.
- An alert condition only changes state through `step()` (pure), and delivery is tracked per channel: never resend to a channel that already took the notice. A new condition needs its entry in `HOLD_S` and, if its value can hover around a threshold, a margin in `evaluate(..., told)`.
- Only the scanner numbers changes (`seq`): what is measured on pixels (`live/metrics.py`) is handed to it with `add_metrics` and enters the index at its next cycle. A spectral ratio is only reported when the field holds enough points (512 px): on small images it is noise.
- One scanner per watched directory at a time (`_retired` in `live/service.py`). Nothing a scanner or a notifier does may hold the index write lock for long: walk and send outside the transaction.

- Quality report (ADR 012, `live/report.py`, `live/report_render.py`, docs/rapport-live.md): the HTML is opened outside imageBio, so no script, nothing fetched, everything from a file name or a metadata file escaped; CSV cells that a spreadsheet would run are written as text. The verdict on an image must stay the one of `frontend/src/live/store.ts`: change both, and `backend/tests/fixtures/verdict_cases.json`, together. A changed report field bumps the schema version (`SCHEMA_VERSION` in `live/report.py`), an added one does not.
- External programs (ADR 011, `tools/`): an argument list declared by the administrator, run by `tools/runner.py` and nothing else (no `subprocess` call anywhere else, never a shell, never a command string). A value that goes into an argument or an answer comes from the server, never from a client, and is refused when it holds a control character. A user is told a status, never the program's output or a path. Results go to the scanner (`add_processing`), tied to the image and the movie they came from. A file of a watched directory is opened, or given to a program, only after `_plain_file` (anybody who writes there can swap it for a link or a pipe between two cycles). Nothing that is not finite, and nothing sized by what a file says, may reach a cycle of the scanner: a cycle that fails keeps what it was given and fails again. MotionCor3 and CTFFIND have never been run for real: tests use `tests/fixtures/programs.py`; do not claim more.
- Operations (ADR 010, `docs/exploitation.md`): secrets come from the environment, never from the configuration file, which is backed up (`IMAGEBIO_PROXY_SECRET`, `IMAGEBIO_METRICS_TOKEN`, `password_env`). In proxy mode nothing is believed without the proxy secret when one is set. `/metrics` holds no path and no user name. A restore never destroys the index in place (it is set aside) and never runs under a server (`hold_state_dir`).
- Settings page (ADR 014, `admin/store.py`, `api/admin.py`): passwords stay with the proxy; imageBio only adds groups and the admin role to a user name. What the page changes goes to `<state>/settings.json` (never `imagebio.toml`), section by section, checked by building the `Settings` it would give, versioned (409 on a stale version), logged without addresses or paths, applied at once through `store.on_change` (`RootSet.replace`, `LiveService.reconfigure`). A new editable setting goes through `effective_settings`; secrets, external programs, the address, the proxy, the identity headers and the file's administrators are never editable. A `settings.json` that cannot be used stops the server. A root of the page is saved as its real path, must lie inside a root of the file (or under `server.root_parents`), away from the state and the programs' work, and is checked again each time roots are served (`RootSet.replace(..., store.serving_check())`). `restore` puts settings back only with `--settings`. Admin routes check the role before reading a body. Administrators see server paths and alert addresses; error messages still hold no path, and users never get an address.
- Personal keys and assistants (ADR 015, `admin/keys.py`, `api/keys.py`, `ai/`): a key (`Authorization: Bearer ibk_…`) reads only (GET, HEAD: refused in `SecurityMiddleware` and again in `_key_user`), never administers, never makes a key, and is no identity beside a proxy identity or another Authorization header. Only its SHA-256 is kept (`<state>/keys.json`, not in backups, never restored). The proxy lets a key through without a password but with its secret, removing identity headers (the three Caddyfiles, `test_deploy.py`, `smoke-test.sh`). A route that answers GET must change nothing a key could not be trusted to change. `ai/imagebio_ai.py` is one file, standard library only, Python 3.9 (tests run a copy outside the package; CI names a 3.9 in `IMAGEBIO_OLDEST_PYTHON`): it never sends a key over plain http except to this computer, never disables TLS verification, and tells the assistant that file names are data, not instructions. No real assistant has been plugged in yet: do not claim it.
- `deploy/` is tested: `backend/tests/core/test_deploy.py` keeps its examples in step with the program, and the CI job "Deployment" builds the image and runs `deploy/compose/smoke-test.sh` (Docker is not available in the cloud sessions: read the job's log). A change to a header, a variable or a route the proxy touches goes with a change there. The single sign-on stack has never run: do not claim it works.

## Frontend (`frontend/`, React + TypeScript + WebGL2)

- Read `frontend/README.md`. Pure logic lives in `viewer/view.ts`, `viewer/luts.ts`, `viewer/cryo.ts`, `viewer/profile.ts`, `viewer/siblings.ts`, `live/store.ts`, `state/url.ts` (unit-tested); rendering in `viewer/gl.ts`.
- A held filter slider (`adjusting` in `App.tsx`, `holdDetail` in `ViewerCanvas`) asks for the overview only; the detail comes on release. A pixel size typed by the user replaces the file's everywhere (`apixTyped`), and is sent as `apix`.
- Checks before every commit, from `frontend/`: `npm run typecheck`, `npm test`, then `npm run build && npm run e2e` (Playwright against the real backend on generated files).
- Contrast, gamma, inversion and colour tables are GPU-side: changing them must never trigger a request. Plane requests are "latest wins" (AbortController).
- Requests for pixels are built from what is on screen (`onScreen`, `shownOptions` and the clamped `index` in `App.tsx`), never from the `path`, `options` or index being loaded: mixing the two asks the server for planes that do not exist. End-to-end tests can assert it with `trackRefused`.
- The page's CSP forbids eval and inline scripts: no inline `style`/`script` in markup (React's `style` prop is fine: it goes through the CSSOM, and the Live grid needs it), and in Playwright pass functions (not strings) to `waitForFunction`.
- No `dangerouslySetInnerHTML`, no `innerHTML`: file names are untrusted text.

## Working within the usage limit

- A 16-agent workflow exhausted the user's 5-hour usage window during M1 and lost its in-flight agents. Write code directly; keep sub-agents to one or two focused reviews of high-risk modules, and commit after each module so nothing is lost.

## Conventions

- Run the checks, read their result, then commit in a separate command. `check | tail -3 && git commit` commits even when the check fails: the pipe returns the status of `tail`.

- Code, identifiers and code comments in English; `docs/PLAN.md` and user docs in French.
- Test fixtures are **generated** (`fixtures/gen_fixtures.py`, seeded). Never commit large binary files.
- At the end of each milestone, update `docs/PLAN.md` (progress, changes) and record decisions in `docs/adr/`.

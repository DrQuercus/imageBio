# imageBio: notes for development sessions

Network-deployed scientific image application (web): ImageJ-style viewer/editor + Cryo mode + Live mode (real-time monitoring of a cryo-EM collection).

- **Read first:** `docs/PLAN.md` (target architecture, milestones M0-M8, acceptance criteria), then `docs/audit/AUDIT.md` (verified bugs in the existing code, with line numbers) and `docs/research/` (formats, EER, EPU/SerialEM metadata, live tools).
- **Existing code:** `legacy/cryoviewer/` (Python) and `legacy/cryoviewer-live/` (Node/React; `npm ci && npm run build`, `node_modules/` and `dist/` are not versioned). Only M0 fixes go in there; the new code lives in `backend/` and `frontend/`.
- **Stack:** Python backend (FastAPI), React + TypeScript frontend (WebGL2 canvas), ImageJ/Bio-Formats only in `workers/imagej` (separate process, GPL boundary). The core stays MIT.

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
- Public error messages never contain a server path. Clients only use logical paths (`<root>/<relative>`); authorisation happens on every request, before any cache lookup.
- Tests compare against a reference (mrcfile, tifffile, Pillow, a naive loop), never against imagebio itself.

## Working within the usage limit

- A 16-agent workflow exhausted the user's 5-hour usage window during M1 and lost its in-flight agents. Write code directly; keep sub-agents to one or two focused reviews of high-risk modules, and commit after each module so nothing is lost.

## Conventions

- Code, identifiers and code comments in English; `docs/PLAN.md` and user docs in French.
- Test fixtures are **generated** (`fixtures/gen_fixtures.py`, seeded). Never commit large binary files.
- At the end of each milestone, update `docs/PLAN.md` (progress, changes) and record decisions in `docs/adr/`.

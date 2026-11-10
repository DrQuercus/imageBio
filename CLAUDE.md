# imageBio: notes for development sessions

Network-deployed scientific image application (web): ImageJ-style viewer/editor + Cryo mode + Live mode (real-time monitoring of a cryo-EM collection).

- **Read first:** `docs/PLAN.md` (target architecture, milestones M0-M8, acceptance criteria), then `docs/audit/AUDIT.md` (verified bugs in the existing code, with line numbers) and `docs/research/` (formats, EER, EPU/SerialEM metadata, live tools).
- **Existing code:** `CryoViewer.zip` (Python) and `cryoviewer-live.tar.gz` (Node/React) at the root until M0, then `legacy/`. Only M0 fixes go in there; the new code lives in `backend/` and `frontend/`.
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

## Conventions

- Code, identifiers and code comments in English; `docs/PLAN.md` and user docs in French.
- Test fixtures are **generated** (`fixtures/gen_fixtures.py`, seeded). Never commit large binary files.
- At the end of each milestone, update `docs/PLAN.md` (progress, changes) and record decisions in `docs/adr/`.

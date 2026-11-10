# Choosing a foundation for the ImageJ-based scientific image editor (lab network, modern UI, broad format support)

**Network note:** WebSearch worked, but WebFetch was blocked by the session allowlist (openmicroscopy.org and bioio-devs.github.io would not resolve). The facts below come from search-result snippets plus my own knowledge. Points I could not confirm are marked *(verify)*.

---

## 0. Recommendation

**Use option (e), with (b) as its core.** Build a web application served from the lab server:
- **Backend:** Python with FastAPI, the scientific Python I/O stack, a job queue and WebSockets.
- **Frontend:** React/TypeScript with a custom WebGL2 canvas (WebGPU later). Use Viv/deck.gl or OpenSeadragon-style tiling for very large or pyramidal images.
- **ImageJ compatibility, two channels:**
  1. A server-side **"ImageJ worker"** using pyimagej (ImageJ2 plus the legacy ImageJ1 layer, run headless, with Xvfb for plugins that need a GUI). It runs real macros and plugins on server data.
  2. An **"Open in Fiji"** bridge. It exports TIFF plus ImageJ-compatible ROI sets (`roifile`) and calibration, or gives the NAS path, so the scientist finishes in real Fiji when needed.
- **Long-tail formats:** Bio-Formats runs in an isolated worker process (bioio-bioformats or scyjava). Pure-Python readers stay the fast path for common formats.

**Why this fits this user:**
- The existing code already has this shape. CryoViewer is a Python web server with an HTML/JS UI. CryoViewer Live is a React/Vite frontend with a file-watching backend. Option (b) unifies both into one backend (Python FastAPI, with watchdog replacing chokidar) and one React UI, instead of throwing them away.
- "Deploy on my network" plus "data stays on the NAS" means a browser app with zero install on client PCs and one server close to the data, where the GPU also lives.
- A truly modern, pleasant UI is far easier on the web than in AWT/Swing.
- AI-assisted solo development is most productive in TS/React and Python, which have the largest training corpora.

**The main risk is scope.** Re-implementing ImageJ is a decades-long effort. The plan must:
- (1) Build a sharp MVP of the ~20 features scientists use daily.
- (2) Delegate everything else to the real ImageJ, through the worker or the Fiji bridge.
- (3) Never try to re-implement the ImageJ macro language or the ImageJ1 plugin API in the browser.

---

## 1. The ImageJ ecosystem

| Component | What it is | License | Notes for this project |
|---|---|---|---|
| **ImageJ 1.x** (Wayne Rasband, NIH) | Original Java app; UI is **AWT** (Frame, Canvas, GenericDialog = AWT Dialog) | **Public domain** | Can legally be forked or copied freely. Its UI cannot be modernised by changing the look-and-feel: FlatLaf styles Swing, not AWT, so ImageWindow, dialogs and the toolbar would need rewriting. |
| **ImageJ2 / SciJava / ImgLib2** | Modern n-D data model, plugin framework, headless-capable ops; includes a legacy layer that runs IJ1 | **BSD-2** | The base pyimagej uses. ImgLib2 is the n-D, lazy data model. |
| **Fiji** | Distribution of ImageJ2 + IJ1 + hundreds of plugins and update sites | Mixed: many plugins GPL-2/GPL-3, core BSD | Moved to OpenJDK 21 with the new **Jaunch** launcher. Has a **Python mode** (`--python`, Edit > Options > Python) built on **Appose** (shared-memory Java/Python IPC). Fiji README lists "all platforms supporting Java 21+". |
| **Bio-Formats** (OME / Glencoe) | Java reader/writer for ~150-160 formats, OME-TIFF writer, OME data model | **GPL-2+** for most readers; a **BSD-2 core** subset (OME-TIFF etc.); commercial licence available from Glencoe | 8.x series active (8.3.0 Aug 2025, 8.4.0 Jan 2026). The OME-Zarr reader is a separate external ZarrReader, not core. bioformats2raw / raw2ometiff (Glencoe) convert to OME-Zarr / OME-TIFF. |
| **pyimagej** | Python <-> ImageJ2 bridge (JPype/scyjava, runs a JVM in-process, imglyb zero-copy to NumPy) | Apache-2.0 | **Default mode is headless.** In headless mode no ImageWindow exists. WindowManager, RoiManager and IJ are only partly functional. GUI-assuming IJ1 plugins often fail. GenericDialog headless replacement works for most but not all plugins. The documented workaround is Xvfb plus interactive mode. Fine for running macros, filters, Analyze Particles and Bio-Formats server-side. |
| **ImageJ.JS** (AICell/ImJoy) | ImageJ 1.x compiled to the browser via **CheerpJ 3** (ij.aicell.io; older ij.imjoy.io) | IJ is PD, but **CheerpJ is proprietary**: free "Community" tier for individuals and non-commercial open source with CDN loading and attribution; companies need a paid tier; pages conflict *(verify)* | Impressive (~1-1.5k daily users) but keeps the old AWT UI. Has browser-sandbox file-access limits and slower performance. Licensing is ambiguous for an institutional deployment. At best an optional "classic ImageJ" pane; it does not solve the UI goal. |
| **napari** | Python n-D viewer (Qt + vispy/OpenGL), npe2 plugin system, napari-hub | BSD-3 | Very active: 0.7.0 Mar 2026, 0.7.1 Jun 2026, 0.8.0 ~Jul 2026 (piwheels) *(verify)*. Still pre-1.0. **napari-imagej** exposes ImageJ2/Fiji ops inside napari. Desktop per PC, not a multi-user web app. |
| **Python I/O stack** | `tifffile` + `imagecodecs` (cgohlke), `mrcfile`, `zarr` (v3) + `ome-zarr-py`/`ngff-zarr`, `bioio` (+ plugins), `bfio`, `h5py` | Mostly BSD/MIT; some bioio plugins GPL (see §4) | `imagecodecs` has had an **EER decoder** since 2023.8.12. v2025.11.11 brought a breaking fix to EER super-resolution decoding (tifffile #313), so pin versions. |
| **Appose** | Java/Python process-to-process bridge with shared-memory NDArrays | BSD *(verify)*, "in incubation" | A cleaner alternative to in-process JPype for calling Fiji from Python, or Python deep learning from Fiji. |
| **roifile** (cgohlke) | Read/write ImageJ `.roi` / RoiSet `.zip` | BSD-3 | Key to ROI interoperability with Fiji's ROI Manager. |

### Licensing implications
- **"Take all of ImageJ's code":** ImageJ 1.x is public domain, and ImageJ2, SciJava and ImgLib2 are BSD-2. Reusing or porting algorithms (thresholding methods, particle analyzer logic, LUTs, measurement definitions) is legally clean. Keep attribution and the BSD notices. **Fiji plugins are often GPL.** Porting their code makes the derived module GPL.
- **Bio-Formats (GPL-2+) in a network app:**
  - The GPL, unlike the AGPL, has **no network-use clause**. Running it on an internal lab server and serving users through a browser is **not distribution**, so it triggers no source-disclosure obligation.
  - Obligations start if the app is **distributed** (e.g. given to other labs or a facility network, or published). The combined work that links Bio-Formats in-process (JPype counts as linking in most readings) must then be GPL-compatible.
  - Mitigations: (a) license the whole project GPL-3 from day 1 (simplest; GPL-2+ is compatible with GPL-3); or (b) keep Bio-Formats in a **separate worker process/service** behind a narrow API or file interface. That arm's-length "aggregation" is the usual defensible line, and the core stays permissive.
  - The same applies to GPL Python plugins (bioio-czi GPL-3, rosettasciio GPL-3, possibly bioio-lif / bioio-bioformats; sources conflict *(verify)*).
- **CheerpJ** is the only component with a real commercial licensing risk. Avoid it as a core dependency.
- **Vendor SDKs:** pylibCZIrw/libCZI are LGPL-3 or commercial (dual), fine as a library. The Thermo Fisher EerReaderLib has its own licence *(verify)*. imagecodecs' EER decoder avoids needing it.

---

## 2. Architecture options compared

Scores are 1-5, where 5 is best.

| Criterion | (a) Extend Fiji (Swing) + Cryo plugin + FlatLaf | (b) Web app: React/TS + WebGL + FastAPI Python (+ pyimagej headless) | (c) Desktop shell (Tauri/Electron/Qt) embedding Python | (d) napari-based app + plugins | (e) Hybrid = (b) + ImageJ worker + "Open in Fiji" |
|---|---|---|---|---|---|
| UI quality achievable | **2**. IJ1 core is AWT; FlatLaf only restyles Swing parts (ImageJ2 menus/dialogs). Image windows, toolbar and GenericDialogs stay 1997-looking unless rewritten. | **5**. Full control: dockable panels, dark/light themes, command palette, responsive. | **4-5**. Same web UI in Tauri/Electron; Qt (PySide6) good but more work to look modern. | **3**. Qt; consistent but "napari-looking"; dock widgets via magicgui/Qt. | **5** |
| Format coverage | **5**. Bio-Formats native + Fiji readers. | **4-5**. Pure Python covers ~90% of real lab use; Bio-Formats worker covers the tail. | 4-5 (same as b) | 4-5 (napari readers + bioio + napari-imagej) | **5** |
| ImageJ compatibility (macros, plugins, ROI Manager, LUTs, hyperstacks, measurements) | **5**. It *is* ImageJ. | **2-3** natively. Concepts re-implemented (LUTs, hyperstack nav, ROIs, measurements with IJ semantics); real macros/plugins only via pyimagej server-side (headless limits apply). | 2-3 (same; or bundle a JVM per PC) | **3** via napari-imagej (ImageJ2 ops/scripts; IJ1 GUI plugins limited) | **4**. Native MVP + server macros + Fiji round-trip covers almost everything. |
| Multi-user network deployment (data on NAS, browser clients) | **1-2**. Install Fiji on every PC and mount the NAS (SMB/NFS performance on huge files), or use VNC/Guacamole into a server Fiji. No multi-user model. | **5**. One server next to the data; zero install; central updates; auth via reverse proxy (LDAP/AD/OIDC); shared sessions possible. | 2-3. Per-PC install; could be a thin client to the server (then it is just (b) with a wrapper). | **1-2**. Per-PC; remote = VNC/JupyterHub; not designed for multi-user. | **5** |
| GPU usage | Client GPU: limited (Java2D); CLIJ2 for GPU processing in Fiji. | Client: WebGL2/WebGPU for display (LUTs, contrast, compositing, 3D). Server: CuPy/cuCIM/PyTorch for FFT, filters, denoising, CTF etc. | Client GPU in the WebView, plus local CUDA if Python is embedded. | Client OpenGL (vispy); local CUDA. | Same as (b), plus CLIJ in the Fiji worker. |
| Effort (AI-assisted solo dev) | Low to start (a Cryo plugin is easy); **very high** to reach "pleasant modern UI"; Java/Swing/AWT is less productive with AI tools. | Medium-high: MVP viewer/editor in roughly 2-4 months of focused work, re-using CryoViewer/Live logic. | High: (b) plus packaging, auto-update and signing for 3 OSes. | Low-medium for viewer + cryo widgets; high to make it an "ImageJ-like editor". | Medium-high: (b) plus a thin worker (pyimagej) and export bridge, roughly +2-4 weeks. |
| Maintainability | Tied to Fiji update sites and Java 21 migration; mixed codebase. | Good: two codebases (TS, Python) with clear API; standard tooling and tests. | Medium. | Good, but pre-1.0 napari API churn. | Good; the ImageJ dependency is isolated in one worker. |

**Verdict:**
- **(a)** maximises ImageJ fidelity but fails the UI and network goals. Keep Fiji as the escape hatch, not the foundation.
- **(c)** adds packaging cost without solving multi-user. Revisit only if offline laptop use (no server) becomes a requirement. The same React UI can later be wrapped in Tauri.
- **(d)** is a great *personal* analysis tool, and could stay a companion (a napari plugin reading the same server data or OME-Zarr), but it is not a network product.
- **(b)/(e)** is the only option that satisfies all the stated goals.

### Key technical choices for (b)/(e)

**Server**
- FastAPI (async) with uvicorn behind nginx/Caddy (TLS plus auth).
- WebSockets for live updates (the Live mode) and progress.
- Job execution in a process pool or queue (arq/RQ/Celery; start simple with asyncio + ProcessPool).
- Read-only NAS mount; strict path allow-listing (no path traversal).
- On-disk tile/thumbnail cache.
- Optional conversion of heavy files to OME-Zarr pyramids on first open (or via bioformats2raw).

**Data transport**
- Never ship whole multi-GB files to the browser.
- Serve tiles and planes at the requested resolution as **raw typed arrays** (uint8/uint16/float32, compressed with zstd/lz4 or gzip) and do LUT/contrast on the GPU client-side. Sending PNGs bakes in the contrast and kills interactive Brightness/Contrast.
- Histograms and statistics are computed server-side on the full-resolution data.

**Client rendering**
- WebGL2 (R16UI/R32F textures, LUT as a 1D texture, multi-channel composite in the shader). Texture limit is typically 16384 px, so tile larger images.
- **Viv** (deck.gl layers, MIT; OME-TIFF/OME-Zarr multiscale) is reusable, as is **vizarr** (MIT) as a reference.
- Overlays (ROIs, scale bar, particle picks, CTF rings) go on a separate SVG/Canvas2D layer.
- WebGPU is optional later (compute shaders for client-side FFT/filters).

**ImageJ worker**
- A separate process with `pyimagej` (`imagej.init('sc.fiji:fiji', mode='headless')`, or interactive under Xvfb for GUI-bound plugins).
- Endpoints: run a macro or script on a server-side image, run a named plugin, return the image, results table and ROIs.
- JVM heap configured; one warm JVM per worker; restart on failure.
- This is also where Bio-Formats lives, which isolates the GPL code and the JVM.

**"Open in Fiji"**
- Export the current view or selection as an ImageJ TIFF (`tifffile` `imagej=True`, with calibration, LUT, hyperstack axes and overlays) plus `RoiSet.zip` (`roifile`).
- Alternatively, generate a small `.ijm` that opens the NAS path in the user's local Fiji.

---

## 3. The "core" ImageJ features scientists expect (MVP scoping)

**MVP: must feel like ImageJ**
1. Open / save (TIFF/BigTIFF/OME-TIFF, MRC/MRCS, PNG/JPEG, plus the main vendor formats; see §4); drag-and-drop from a NAS browser; recent files.
2. **Stacks and hyperstacks**: C/Z/T sliders, play animation, channel composite / color modes, ortho views (XZ/YZ).
3. **Brightness/Contrast and Window/Level** (Auto, Set, Reset, Apply); histogram; per-channel display ranges.
4. **LUTs**: Grays, Fire, Ice, Spectrum, 16-colors, HiLo, mpl-viridis etc., invert LUT; import of ImageJ `.lut` files.
5. **Zoom/pan**, pixel inspector (x, y, value, calibrated units), line profile (Plot Profile).
6. **Selections / ROIs**: rectangle, oval, polygon, freehand, line/polyline, point/multi-point, angle; **ROI Manager** (add, rename, measure, multi-measure, save/load as IJ RoiSet.zip).
7. **Measurements**: Set Measurements (area, mean, std, min/max, integrated density, centroid, perimeter, Feret, circularity, etc.); Results table with CSV export; ImageJ semantics for compatibility.
8. **Calibration**: Set Scale (pixel size from metadata or manual), units, **scale bar**, calibration bar.
9. **Thresholding**: Adjust Threshold with ImageJ auto methods (Default/IsoData, Otsu, Li, Huang, Triangle, Yen, MaxEntropy, Moments, ...); most exist in scikit-image, and ImageJ "Default" needs a port. Then convert to mask.
10. **Analyze Particles** (size/circularity filters, outlines, results, add to ROI Manager).
11. **Basic processing**: Gaussian/median/mean filters, unsharp, subtract background (rolling ball), invert, math (add/multiply/log), type conversion (8/16/32-bit with scaling), crop, rotate/flip, resize/bin, duplicate.
12. **Z Project** (max, mean, sum, SD, median); Image Calculator; Merge/Split channels; Stack ops (substack, reslice, concatenate).
13. **FFT** (power spectrum display, bandpass filter), which matters doubly for cryo.
14. **Undo** (at least one level, ideally a history), plus non-destructive display adjustments.
15. **Command recorder**: record actions as a reproducible script. Recommend recording to Python (and optionally emitting equivalent IJ macro lines) instead of reimplementing the IJ macro interpreter.

**V2 (later)**
- Macro/script execution through the ImageJ worker; script editor (Python, plus IJM sent to the worker).
- Binary ops (erode/dilate/open/close, fill holes, watershed, skeletonize, distance map).
- 3D viewer / volume rendering (WebGL ray-marching, or Neuroglancer embed); orthogonal slicing of tomograms.
- Registration/alignment (StackReg-like), drift correction, denoising (Noise2Void/cryoCARE/Topaz-denoise on the server GPU).
- Plugin system for the new app (Python entry points plus a UI manifest).
- Colocalization, kymographs, tracking, montage/annotations/arrows/text, figure export (SVG/PDF).
- Batch processing over folders (job queue); shared sessions / annotations between users.

**Cryo mode** (builds on CryoViewer and CryoViewer Live):
- MRC/MRCS/EER/TIFF movies; gain reference application; frame summing (EER fractionation).
- Power spectrum with CTF Thon rings overlay; ice thickness estimates.
- Particle-pick overlays (STAR/BOX/CBOX); 2D class montage viewer (MRCS stacks).
- Tomogram slicing; SerialEM `.mdoc` and EPU XML metadata (grid/square/hole/foil-hole navigation).
- **Live collection monitoring**: watch folders, parse headers and metadata only for speed; defocus/astigmatism/resolution/drift time-series dashboards; alerts.
- Hooks to run CTFFIND / MotionCor2-3 / cryoSPARC Live or RELION outputs.

---

## 4. Format coverage strategy

**Principle:** pure-Python readers first. They are fast, lazy/memory-mapped, permissively licensed and easy to debug. Bio-Formats (JVM worker) is the fallback for anything not covered. Normalise everything internally to an xarray/dask-like lazy n-D array with OME-style metadata (axes TCZYX, physical pixel sizes, channel names/colors). **bioio** is a good abstraction to borrow or wrap: BSD-3 core plus per-format plugins, including a Bio-Formats plugin.

| Format | Pure-Python reader(s) | License | Bio-Formats? | Notes |
|---|---|---|---|---|
| **TIFF / BigTIFF / ImageJ-TIFF** | tifffile (+ imagecodecs) | BSD-3 | yes | tifffile also reads IJ hyperstack metadata and writes `imagej=True`. |
| **OME-TIFF** | tifffile; ome-types (metadata) | BSD-3 / MIT | yes (BSD core) | Pyramidal OME-TIFF via tifffile series levels. |
| **OME-Zarr (NGFF)** | zarr-python v3, ome-zarr-py, ngff-zarr; bioio-ome-zarr | MIT/BSD | external ZarrReader | **Recommended internal cache/pyramid format.** Browser-native via Viv/vizarr/zarrita.js. |
| **Zeiss CZI** | pylibCZIrw (ZEISS libCZI); czifile (cgohlke); bioio-czi | LGPL-3/commercial; BSD-3; GPL-3 | yes | pylibCZIrw is the most complete (JPEG-XR, pyramids). |
| **Leica LIF / LOF / XLEF** | liffile (cgohlke); readlif | BSD-3; readlif unverified | yes | |
| **Nikon ND2** | nd2 (T. Lambert) | BSD-3 | yes | Excellent metadata. |
| **Olympus OIB/OIF** | oiffile (cgohlke) | BSD-3 *(verify)* | yes | |
| **Olympus/Evident VSI (cellSens)** | weak pure-Python support | — | **yes (main option)** | Route to the Bio-Formats worker. |
| **DeltaVision DV** | `mrc` package (T. Lambert) | BSD *(verify)* | yes | |
| **Whole-slide (SVS, NDPI, MRXS)** | openslide-python; tifffile for SVS/NDPI | LGPL-2.1; BSD | yes | |
| **MRC / MRCS / REC / ST** | mrcfile | BSD-3 | yes (basic) | Memory-map for big stacks/tomograms; handle mode 0/1/2/6/12 (float16) and voxel size. |
| **EER (Falcon 4/4i)** | tifffile + imagecodecs EER decoder | BSD-3 | no decoding | Event-based BigTIFF; compression 65001 (7-bit) / 65002 etc. Choose a super-resolution/render factor and fractionation. Pin imagecodecs ≥ 2025.11.11 (breaking super-res fix). Live mode: read tags/metadata only. |
| **Gatan DM3/DM4** | ncempy; rosettasciio (rsciio) | BSD-style (LBNL) *(verify)*; GPL-3 | yes (GatanReader) | rsciio states DM reading is incomplete. |
| **FEI/TFS SER + EMI (TIA)** | ncempy; rosettasciio | as above | no / unclear | |
| **EMD (Berkeley NCEM / TFS Velox HDF5)** | ncempy (Berkeley); rosettasciio (Velox read) | as above | partial | |
| **Generic HDF5 / NeXus** | h5py (+ hdf5plugin for Bitshuffle/LZ4/Blosc) | BSD | partial | Needs a dataset picker UI. |
| **PNG / JPEG / GIF / BMP / WebP / JPEG2000 / JPEG-XL** | Pillow, imagecodecs, imageio | HPND/BSD | yes | |
| **NetCDF** | xarray + netCDF4 / h5netcdf | BSD/MIT | no | |
| **FITS** | astropy.io.fits | BSD-3 | yes (basic) | |
| **DICOM** | pydicom (+ pylibjpeg / python-gdcm for compressed) | MIT | yes (basic) | |
| **Long tail (~150-160 formats: Imaris IMS, Andor, Hamamatsu, PerkinElmer, Prairie, Slidebook, LSM legacy, etc.)** | via **bioio-bioformats** / scyjava / pyimagej | GPL | **yes** | Run in the isolated JVM worker; cache the result as OME-Zarr. |

**Pure-Python route vs Bio-Formats via JPype/scyjava:**
- **Pure Python:** fast, lazy, no JVM, permissive licences, easy to package. Coverage is roughly 25 formats, but they are the ones that matter. Metadata quality varies per reader.
- **Bio-Formats via JPype:** maximal coverage and standardised OME-XML metadata. The costs:
  - a JVM per worker (~0.5-1 GB RAM, seconds of startup, so keep it warm);
  - GPL;
  - plane-by-plane reads (slower for random tile access unless converted once to OME-Zarr);
  - JPype in-process can conflict with other native libraries, which is another reason for a separate process.
- **Pattern:** detect the format, use the pure-Python reader if available, otherwise use the Bio-Formats worker. Offer a **"convert to OME-Zarr"** step for anything big or slow.

---

## 5. Main risks and mitigations
1. **Scope explosion ("ultimate").** Freeze an MVP feature list (§3 MVP plus Cryo + Live), ship it, and delegate the rest to the ImageJ worker or the Fiji bridge.
2. **ImageJ parity.** Measurements and threshold results may differ slightly from ImageJ (e.g. the "Default" threshold, perimeter definitions, Feret). Port these algorithms directly from IJ1 public-domain source, and add golden tests that compare against Fiji outputs through pyimagej in CI.
3. **pyimagej headless limits.** Some IJ1 plugins need a GUI. Use Xvfb plus interactive mode in the worker, and document what is unsupported.
4. **Big-data performance.** Rely on tiling, pyramids (OME-Zarr), raw-typed-array transport, a GPU LUT, server caching and memory-mapping (mrcfile/tifffile `aszarr`).
5. **Licensing.** Decide early: GPL-3 for the whole project (simplest), or a permissive core with Bio-Formats and GPL plugins isolated in a worker. Avoid CheerpJ.
6. **Security on the lab network.** Use the reverse proxy with SSO/LDAP, read-only data mounts, path allow-lists, no arbitrary-code macro execution for unauthenticated users, and per-user job limits.
7. **Two existing backends** (Python stdlib http.server and Node/Express). Consolidate on a single Python FastAPI backend. watchdog replaces chokidar, and WebSockets via FastAPI replace `ws`. This avoids running two stacks.
8. **Library churn.** napari and Appose are pre-1.0, and imagecodecs EER had a breaking change. Pin versions and keep reader adapters behind your own interface.

---

## Sources
- pyimagej headless / legacy ImageJ: https://py.imagej.net/en/latest/11-Working-with-the-original-ImageJ.html , https://py.imagej.net/en/1.4.1/Headless.html , https://py.imagej.net/en/latest/Troubleshooting.html
- Fiji on Java 21 / Jaunch / Python mode: https://events.humantechnopole.it/event/1/contributions/185/ , https://sites.imagej.net/Fiji/README.md-20260319185409 , https://imagej.net/Python
- FlatLaf: https://www.formdev.com/flatlaf/ , https://github.com/JFormDesigner/FlatLaf
- Appose: https://github.com/apposed/appose , https://pypi.org/project/appose
- ImageJ.JS / CheerpJ: https://imagej.net/software/imagej-js , https://labs.leaningtech.com/showcase/imagej-js , https://cheerpj.com/licensing/ , https://cheerpj.com/docs/licensing
- Bio-Formats releases and licensing: https://www.openmicroscopy.org/2026/01/15/bio-formats-8-4-0.html , https://www.openmicroscopy.org/2025/08/05/bio-formats-8-3-0.html , https://bio-formats.readthedocs.io/en/stable/formats/index.html , https://loci.wisc.edu/bio-formats/ , https://downloads.openmicroscopy.org/presentations/2024/Dundee/Day1/OME-2024-Bio-Formats.pdf
- bioio and plugins: https://pypi.org/project/bioio/ , https://libraries.io/pypi/bioio-czi , https://pypi.org/project/bioio-bioformats/0.9.0
- napari releases / napari-imagej: https://www.piwheels.org/project/napari/ , https://melissawm.github.io/napari.github.io/dev/release/release_0_7_0.html , https://napari-imagej.readthedocs.io/
- EER: https://github.com/cgohlke/imagecodecs , https://newreleases.io/project/github/cgohlke/imagecodecs/release/v2025.11.11 , https://assets.thermofisher.com/TFS-Assets/MSD/Support-Files/eer-file-format.pdf , https://github.com/cgohlke/tifffile
- RosettaSciIO formats: https://rosettasciio.readthedocs.io/en/latest/supported_formats/index.html , https://pypi.python.org/pypi/rosettasciio
- Viv / vizarr: https://github.com/hms-dbmi/viv , https://raw.githubusercontent.com/hms-dbmi/viv/main/README.md , https://awesome.ecosyste.ms/projects/github.com%2Fhms-dbmi%2Fvizarr
- nd2 / libCZI / liffile: https://pypi.org/project/nd2/0.10.0/ , https://www.github.com/ZEISS/libczi , https://repos.ecosyste.ms/hosts/GitHub/repositories/cgohlke%2Fliffile

**Not fetched, from knowledge (verify):**
- licences of ncempy, oiffile, the `mrc` package, readlif and Appose;
- the exact Bio-Formats format count (~150-160);
- whether any Fiji release ships FlatLaf by default;
- the current CheerpJ terms for institutional use.
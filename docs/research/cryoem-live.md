# Cryo mode + Live monitoring: research report

**Network.** Web search, PyPI, the npm registry and raw.githubusercontent.com all worked. relion.readthedocs.io and the github.com API were blocked, so I read the RELION, imagecodecs and MotionCor3 source on raw GitHub instead. There was no GPU in this sandbox, so I could not benchmark motion correction or CTF estimation.

**Test files.** The research work directory was temporary. The only script kept is the synthetic EER encoder, copied to [`eer_synthetic_encoder.py`](eer_synthetic_encoder.py) (bit-exact decode round trip via imagecodecs and tifffile). The benchmarks (4K EER frames) are summarised in section 1.

---

## 0. What the two apps do now (relevant points only)

**CryoViewer, EER (`server.py` l.90-123).**
- `parse_eer` only counts IFDs and hard-codes apix=0.5.
- `read_eer_frame` returns synthetic Poisson noise (`rng.poisson(0.01,…)`). EER files therefore show fake images. This is the most important "bug".
- Real decoding is now easy (see §1).

**CryoViewer Live.**
- Uses chokidar 3.6 with a glob, `usePolling:true, interval:3000, awaitWriteFinish{2000,1000}`.
- Handles MRC and JPG only: no TIFF, EER or XML parsing.
- The per-file stat polling cannot scale to 50k files on NFS (see §5).
- chokidar v4 dropped glob support, so a simple dependency upgrade would break it.

---

## 1. EER (Falcon 4 / 4i)

### Format (checked in RELION `renderEER.cpp` and imagecodecs `imcd.c`)
- **Container:** BigTIFF, one IFD/strip per EER fraction. Detector is 4096² (2048² is also handled).
- **Compression tag:**
  - 65000: 8-bit run-length, legacy.
  - 65001: 7-bit run-length + 2+2 sub-pixel bits.
  - 65002: "detailed"; tag 65007 gives the run-length bits, tags 65008/65009 the horizontal/vertical sub-pixel bits.
- **Bitstream:** LSB-first codes.
  - A skip value equal to max (127) means "skip, no event".
  - Any other value is followed by the sub-pixel bits, and the event lands at `pixel += skip`.
  - The sub-pixel value is XOR-ed with 2.
  - The stream must end exactly on the last pixel.
- **Metadata:** tag 65001 holds acquisition XML (`<metadata><item name=…>`), e.g. `numberOfFrames`, `exposureTime`, `totalDose` (e/px). Tag 65002 holds per-frame metadata. In `.gain` files, tag 65100 holds the camera defects.
- **Legacy `.ecc`:** frames are indexed from the end via the footer "ThermoFisherECComprOK000".
- **Frame rate:** about 248 fps on Falcon 4 and about 320 fps on Falcon 4i, so one movie has ~1000-1600 fractions. MDCatch computes the count as `int(exposure × FrameRate) × 31/32`.

### Open decoders (tested here)
**imagecodecs ≥ 2026.10.10** has `imagecodecs.eer_decode(data, shape, skipbits, horzbits, vertbits, *, superres=0)`, which returns a bool image. `EER.available == True`, and `eer_encode` is not implemented.

**tifffile 2026.11.8** decodes EER natively through imagecodecs:
- `TIFF.COMPRESSION.EER_V0/V1/V2 = 65000/1/2`.
- `TiffFile(fn, superres=N)` upsamples ×2^N; superres=1 gives 8K, 2 gives 16K.
- `tf.is_eer` and `tf.eer_metadata` read tag 65001.
- Caveat: `is_eer` requires BigTIFF and a tag 65001 value starting with `b'<metadata>'`. For non-conforming files, pass `TiffFile(fn, is_eer=True)`.
- Writing EER is not supported.

**What I verified:**
- A synthetic 7-bit stream (40 frames, 256²) decoded bit-exact, both through `eer_decode` and through a hand-built TIFF with compression 65001 read by tifffile.
- superres=1 and superres=2 give 512² and 1024² from 256².
- Gotchas found: the input byte count must be even, and the last run must land exactly on width×height, otherwise you get IMCD_VALUE_ERROR or OUTPUT_TOO_SMALL.

**Speed** (single 4K frame, 0.015 e/px, ~250k events, ~384 KB, 4-core sandbox):
- `eer_decode` alone: 5.4 ms/frame.
- Decode + accumulate into uint16: 7.5 ms/frame serial, 5.7 ms/frame with 4 threads (accumulation is the bottleneck).
- 8K superres decode: 20 ms/frame.
- So a full 1000-fraction sum takes about 6-8 s on one core. Use a process pool for live work.
- Measured size: about 1.5 bytes per electron event. File size ≈ 1.5 B × dose (e/Å²) × apix² × 4096². Example: 40 e/Å² at 0.73 Å/px gives about 0.5 GB per movie.

### RELION reference behaviour (`renderEER.h` / `motioncorr_runner.cpp`)
- `--eer_upsampling 1|2` (3 only for 2-bit sub-pixel streams). 1 means physical 4K, 2 means 8K.
- `--eer_grouping` defaults to 40; trailing frames are truncated.
- A common rule is to group to about 1 e/Å² per group: MDCatch uses `group = 1 / dose_per_EER_frame`.
- **Gain:**
  - A `.gain` file is a multiplicative float TIFF. RELION flips Y only because its own TIFF reader flips; tifffile does not need the flip.
  - A non-`.gain` (MRC) EER gain is a counts image and is inverted to `mean/gain`.
  - A gain value of 0 marks a defect.
  - Gain size can be 4K or 8K and is binned or expanded to the rendering grid.
- **MotionCor3 options** (from `CInput.cpp`): `-InEer`, `-EerSampling`, `-FmIntFile` (text file: nFrames, group, dose), `-Gain`, `-RotGain`, `-FlipGain`, `-InvGain`, `-DefectFile`, `-FmDose`, `-PixSize`, `-kV`, `-Cs`, `-AmpCont`, `-ExtPhase` (built-in CTF), `-Patch`, `-Serial` / `-InSuffix`, `-OutStar`, `-SplitSum`, `-Gpu`.
- Scipion extracts defects from the `.gain` header automatically.

### Correct EER preview recipe (MVP)
1. Decode all fractions at superres=0 into a uint16 4K sum.
2. Multiply by the `.gain` image, read with tifffile, no flip.
3. Replace zero-gain pixels (defects) with the local median.
4. Fourier-crop to 1K or 2K.
5. Gaussian lowpass at about 20 Å.
6. Contrast from the 1-99 percentiles.
7. Cache the result as float16 plus a PNG, with the power spectrum computed from the 4K sum.
8. Frame mode: group N fractions to about 1 e/Å² per group.

**Gain orientation:** check it automatically on the first movie of a session. Try the 8 rotations/flips and keep the one that minimises the variance of 64-px tiles in the corrected sum. Warp users have needed `--gain_flip_y` on Falcon 4 data, so orientation does go wrong in practice.

---

## 2. Session layouts and metadata

### EPU (Thermo Fisher)
**Movie path patterns** (confirmed in MDCatch `config.py`):
- `Images-Disc*/GridSquare_*/Data/FoilHole_<hole>_Data_<target>_<n>_<date>_<time>{_EER.eer | _fractions.tiff (K3) | _Fractions.mrc (Falcon 3) | .mrc}`, each with a matching `.xml` and `.jpg`.
- Movies may sit on a separate "DoseFractions/<session>/…" offload tree; the XML and JPG stay in the EPU session folder.
- EPU does not copy the Falcon 4 EER gain. Its name is in the XML key `Detectors[EF-Falcon].EerGainReference` or `Detectors[BM-Falcon].EerGainReference`.
- K3 gain: `*_gain.tiff`; K2 gain: `*-gain-ref.MRC`.

**Session-level files:**
- `EpuSession.dm` at the root.
- `Metadata/GridSquare_<id>.dm`: XML DataContract with target and foil-hole locations (stage/pixel coordinates, selected/acquired flags).
- `Images-Disc1/GridSquare_<id>/GridSquare_<ts>.{jpg,mrc,xml}` and `FoilHoles/FoilHole_<id>_<ts>.{jpg,xml}`.
- `Atlas/Atlas.dm` plus tile images.

**Per-image XML** (namespace `so = http://schemas.datacontract.org/2004/07/Fei.SharedObjects`; XPaths taken from MDCatch `feixml.py`):

| Field | XPath under `so:microscopeData` (unless noted) | Notes |
|---|---|---|
| ExposureTime | `acquisition/camera/ExposureTime` | |
| Detector | `acquisition/camera/Name` | |
| Binning | `acquisition/camera/Binning/x` | |
| CameraSpecificInput | `acquisition/camera/CameraSpecificInput/KeyValueOfstringanyType` | ElectronCountingEnabled, SuperResolutionFactor, NumberOffractions / DoseFractions |
| Magnification | `optics/TemMagnification/NominalMagnification` | |
| SpotSize | `optics/SpotIndex` | |
| ProbeMode, BeamDiameter, EnergySelectionSlitWidth | `optics/…` | |
| BeamShift, BeamTilt, ImageShift | `optics/…` (`_x`, `_y`) | Useful for AFIS optics groups |
| Voltage | `gun/AccelerationVoltage` | |
| InstrumentID | `instrument/InstrumentID` | |
| EPU version | `core/ApplicationSoftwareVersion` | |
| Pixel size | `SpatialScale/pixelSize/x/numericValue` (under the root) | In metres; divide by the super-resolution factor |
| CustomData | `CustomData/KeyValueOfstringanyType` (under the root) | `AppliedDefocus` (m), `Dose` (×1e-20 gives e/Å²), `DoseOnCamera`, `PhasePlateUsed`, `Aperture[C2/OBJ].Name`, `Detectors[*-Falcon].FrameRate`, `EerGainReference` |
| Stage position | `stage/Position/{X,Y,Z,A,B}` | From memory, verify on a real file |
| Timestamp | `acquisition/acquisitionDateTime` | From memory, verify on a real file |

### SerialEM
- Format: `.mdoc` text files with `[ZValue = n]` sections. Read with `mdocfile` 0.2.3, which returns a pandas DataFrame.
- Keys: `TiltAngle`, `StagePosition`, `StageZ`, `Magnification`, `PixelSpacing`, `Defocus`, `TargetDefocus`, `ExposureDose`, `ExposureTime`, `DateTime`, `SubFramePath`, `NumSubFrames`, `FrameDosesAndNumber`, `ImageShift`, `RotationAngle`, `PriorRecordDose`, `MinMaxMean`, `SpotSize`, `CountsPerElectron`.
- The Navigator `.nav` file holds map items and acquisition points.

### Tomo5 / tomography
- Tomo 5 writes a SerialEM-style `.mdoc` per tilt series, and file names usually carry the angle, e.g. `Position_N_…[angle]…`. Verify on a real session.
- Order frames by angle or by acquisition order (dose-symmetric schemes). Angles also come from `.tlt` / `.rawtlt` files.
- `tfs_parser` (G. Sharov, GPL-3) parses EPU and Tomo sessions; it is installed from GitHub, not PyPI.

### DM4 (Gatan)
Readers: `ncempy` 1.16 (`ncempy.io.dm`), `rosettasciio` 0.15 (gatan reader), or Bio-Formats.

### What a live monitor can get per image cheaply (no motion correction)
- **From the XML:** target defocus, dose, exposure, magnification and pixel size, stage XY (to place the image on the grid-square map), timestamp (rate, gaps), beam/image shift (AFIS cluster).
- **From the EER header:** frame count and total dose.
- **From the image** (EPU's integrated `.mrc`/`.jpg` when present, otherwise the cached EER sum):
  - Mean counts, giving ice thickness by aperture-limited scattering, t = Λ·ln(I₀/I). I₀ comes from an empty hole and Λ ≈ 300-400 nm, calibrated per scope.
  - Empty-hole and grid-bar flags.
  - Standard deviation and saturation.
  - A 512-tile periodogram: Thon ring visibility and the relative ice-ring ratio (power at 3.4-3.9 Å versus the neighbouring bands; cryoSPARC users reject above about 1.1-1.2).
  - Coarse drift: cross-correlate the binned sums of the first and second halves of the frames (under 1 s).

---

## 3. Live processing tools: integrate or reimplement

| Function | Recommended | Fallback | Notes |
|---|---|---|---|
| Motion correction | **MotionCor3** subprocess (open source, CZII, github.com/czimaginginstitute/MotionCor3; CUDA, multi-GPU, EER native, built-in GPU CTF) | RELION `relion_run_motioncorr --use_own` (CPU, EER native); MotionCor2 (closed binary, academic licence) | Write `-FmIntFile` from the XML/EER dose; parse the `-OutStar` / `.log` shifts into total and early drift |
| CTF | **CTFFIND5** (cisTEM; Elferich et al., eLife 2024 RP97227) | CTFFIND4.1.14; MotionCor3's built-in CTF | Interactive program, so drive it through stdin. Output: defocus 1/2, astigmatism angle, phase shift, CC, fit resolution (Å). CTFFIND5 adds tilt (about 3°) and thickness (about 5 nm). Avoid Gctf (closed, unmaintained since about 2018, tied to old CUDA). |
| Picking preview | crYOLO 1.9.9 general model (GPU/CPU) or Topaz | In-house DoG/LoG blob picker with scipy | Overlay `.star`, `.cs`, `.box`, `.cbox` coordinates |
| Tomography live | AreTomo3 (CZII; motion + CTF + alignment + reconstruction on the fly) | — | Subprocess |
| Whole pipelines (reference designs, reuse parsers) | RELION 5 Schemes / `relion_it.py`; `ccpem-pipeliner` 1.6.1; Diamond `murfey` 0.28.1 (transfer + EPU/Tomo parsing) and `cryoemservices` 1.6.0 (MotionCor/CTFFIND/crYOLO services); WarpTools 2 (`fs_motion_and_ctf`, Linux, no documented streaming mode); Scipion streaming; CryoFLARE (C++/Qt, FMI); SmartScope (SerialEM-driven screening, NIEHS) | — | Mostly for design; check licences before reusing code |
| GPU in-house (later) | `cupy-cuda12x` 14.2 or PyTorch; teamtomo `torch-ctf` 0.6, `torch-fourier-shift` 0.6 | — | Global-shift alignment and quick CTF if no binaries are installed |

**Design.** Use a job queue (one GPU worker per card). The monitor itself stays lightweight. Each stage writes a JSON or SQLite record per movie, and the UI only reads those records.

**Metrics to track and threshold (cryoSPARC-Live style):**
- CTF fit resolution (cryoSPARC's examples cut at 6-8 Å)
- Defocus versus target (a gap over about 0.5 µm suggests an autofocus problem)
- Astigmatism
- Relative ice thickness and ALS thickness
- Total and early motion. Note that cryoSPARC reports a nonsense value of 1e31 on empty holes, so guard against this.
- Particle count
- Rate (images/h), gaps, dose consistency

**Notifications:**
- No image for more than X min while the session is open
- Disk free below a threshold (statvfs on the target volume)
- Rate drop
- Trends in drift, astigmatism or ice thickness
- Rising empty-hole fraction
- Channels: SMTP, Slack/Mattermost/Teams webhooks, ntfy/Gotify

**Maps:**
- Atlas, with squares coloured by number of good images.
- Grid-square image with an overlay of acquired foil holes (from `Metadata/GridSquare_*.dm` and per-image stage XY), coloured by CTF resolution or ice thickness.
- The stage-to-pixel transform comes from the grid-square XML (stage position + pixel size); calibrate rotation and flip once.

---

## 4. Cryo viewer tools: means of implementation
- **Power spectrum:** periodogram-averaged FFT (512 tiles), log scale, background subtraction.
- **Resolution rings and radial average:**
  - A ring at d Å sits at r = N·apix/d.
  - Mark the 3.7 Å ice ring.
  - Draw the 1D radial average with the fitted CTF overlaid, using the CTFFIND parameters and the standard CTF formula.
- **Filters and binning:** Fourier crop/binning (exact); Gaussian or raised-cosine lowpass, highpass and bandpass in Å.
- **Dose-weighted sum:** Grant & Grigorieff 2015 critical exposure, Ne(k) = 0.245·k^-1.665 + 2.81 (k in Å⁻¹). Requires aligned frames.
- **Gain/defect correction:** as in §1. Ruler and scale bar in Å or nm.
- **3D maps:**
  - Orthoslices served from `mrcfile.mmap`.
  - **Mol*** (molstar 5.13.1, npm) opens MRC/CCP4/MAP density natively and does isosurfaces plus model overlay.
  - **NiiVue 0.69** has no MRC reader (its bundle lists NII, NRRD, MGH, MHA, HEAD, V16, VMR, ZARR). To use it, wrap the MRC as NIfTI on the fly, or build an NVImage from the raw array.
  - Server-side `skimage.measure.marching_cubes` with three.js is an alternative.
- **Tomograms:** mmap, a downsampled pyramid, a Z slider.
- **Tilt series:** `.st`/`.mrc` stack plus mdoc/tlt; sort by angle or by time; show cumulative dose.
- **Particle stacks and 2D classes:**
  - Lazy tiled gallery from mmap'd `.mrcs`.
  - RELION `run_it*_classes.mrcs` with `_model.star` (class distribution and resolution).
  - cryoSPARC class MRCs with `.cs`.
- **Metadata files:**
  - STAR: `starfile` 0.5.13, returns pandas; handles optics groups.
  - cryoSPARC `.cs`: these are NumPy `.npy` structured arrays, so `np.load()` works without cryosparc-tools (5.1.0 is optional).
- **Broad format coverage (ImageJ base):**
  - Bio-Formats through `pyimagej` 1.8.0 / `scyjava` 1.12.5, which needs Java (present here).
  - Or `bioio` 3.7.1 with plugins.
  - Keep the native fast paths: mrcfile 1.5.4, tifffile+imagecodecs (EER), ncempy (DM4).

---

## 5. File watching on network storage, and data volumes

**inotify:**
- inotify only sees writes made by the local kernel.
- It works if the watcher runs on the file server itself, e.g. Samba or NFS export on the same Linux box that receives the microscope's writes.
- It misses writes from other clients on NFS/SMB mounts. On GPFS and Lustre it only sees changes made on that node.
- GPFS has a separate "clustered watch folder" feature (Kafka-based) and Lustre has MDT changelogs (`lfs changelog`). Both are heavy; treat them as later options.

**Recommended scanner (replaces chokidar):**
1. Poll and prune by directory mtime: rescan with `os.scandir` only the "hot" directories (`GridSquare_*/Data` modified in the last N minutes), and do a full rescan every 5-10 min.
2. Keep state in SQLite (path, size, mtime, status, metrics, indexed) so the monitor resumes after a restart and processing stays idempotent.
3. Push WebSocket deltas only, and virtualise the UI grid.
4. Cache thumbnails and power spectra on local SSD.
- chokidar 3 polling uses one `fs.watchFile` per file. At 50k files and a 3 s interval that is about 17k stats/s over NFS, plus the extra polling from awaitWriteFinish.

**"File is complete" test (all three must hold):**
1. The EPU XML partner exists. This is the strongest signal for EPU. For SerialEM, use the mdoc section or the next file appearing.
2. The size is unchanged across two polls at least 5-10 s apart.
3. A format integrity check passes:
   - MRC: size = 1024 + nsymbt + nx·ny·nz·bytes.
   - TIFF/EER: the IFD chain ends with 0 and the last strip offset + bytecount ≤ file size.
- NFS attribute caching (acregmin/acdirmin of 3-30 s) delays detection. Mount the watch path with `actimeo=1` or accept the latency.
- `fs.inotify.max_user_watches` only matters for local inotify.

**Data volumes:**
- Falcon 4i EER: about 0.4-0.9 GB per movie (formula in §1).
- K3: 5760×4092 counting or 11520×8184 super-resolution, LZW TIFF of about 40-60 frames. Roughly 0.2-0.5 GB counting and about 1-2 GB super-resolution; these are orders of magnitude only.
- Rates: about 200-600 images/h is common; Falcon 4i with AFIS and fringe-free imaging can reach about 800-1000+ images/h.
- Example: 600/h × 0.5 GB ≈ 300 GB/h, about 83 MB/s sustained, or about 7 TB/day.
- Consequence: the monitor must not re-read every full movie over the network by default. Use the XML/JPG/integrated MRC first. Compute EER sums once on a node close to the storage (10 GbE or better), and cache them.

---

## Prioritised features

**MVP (Cryo mode + Live v2):**
1. Real EER decode via tifffile/imagecodecs, replacing the fake Poisson preview: gain (`.gain`) and defects, fraction grouping by dose, binned preview, auto gain-orientation check.
2. Native readers for MRC/MRCS/MAP (mmap), TIFF/LZW (K3), EER, DM4, JPG; STAR and `.cs` readers.
3. Power spectrum with resolution rings in Å, radial average, ice-ring ratio; Fourier binning, lowpass/highpass/bandpass in Å; ruler and scale bar.
4. Live: SQLite-backed poller (directory mtime pruning, completeness test, XML pairing) to replace chokidar globs; EPU XML, SerialEM and Tomo mdoc parsers; per-image metrics (dose, target defocus, timestamp and rate, mean counts and ALS ice thickness, empty-hole flag, quick power spectrum, half-sum drift).
5. Live dashboard: rate and gaps, histograms and time plots, thresholds with accept/reject; alerts for stalled collection, low disk and rate drop (webhook, email).
6. Optional GPU worker running MotionCor3 + CTFFIND5 as subprocesses through a queue; show CTF fit resolution, defocus, astigmatism, thickness, drift.

**Later:**
- Grid-square and atlas maps with hole overlays coloured by quality.
- Picking preview (crYOLO/Topaz, or DoG) with particle counts.
- 3D: Mol* map viewer, orthoslices, tomogram viewer with pyramids, tilt-series browser.
- Particle-stack and 2D-class galleries (RELION/cryoSPARC).
- Dose-weighted aligned sums in-viewer.
- AreTomo3 live tomography.
- Export to RELION/cryoSPARC (`.star` with optics groups from AFIS beam shifts).
- Bio-Formats through pyimagej for the long tail of formats.
- In-house cupy/torch motion and CTF for GPU servers without binaries.
- GPFS/Lustre native change feeds.

## Sources
- [MDCatch on PyPI](https://pypi.org/project/MDCatch/0.9.5): EPU XML XPaths and movie/gain patterns were taken from the 2.5 wheel (`mdcatch/utils/feixml.py`, `config.py`)
- [TFS_EPU_parser](https://github.ksda9001.workers.dev/azazellochg/TFS_EPU_parser) and [TFS_parser](https://gh.r-mj.com/azazellochg/TFS_parser)
- [EMPIAR GridSquare .dm example](https://ftp.empiar.pdbj.org/archive2/12791/data/Metadata/GridSquare_1261069.dm)
- RELION `renderEER.cpp` / `renderEER.h` / `motioncorr_runner.cpp`: raw.githubusercontent.com/3dem/relion/master/src/
- imagecodecs `imcd.c` (`imcd_eer_decode`): raw.githubusercontent.com/cgohlke/imagecodecs/master/imagecodecs/imcd.c
- [MotionCor3](https://github.com/czimaginginstitute/MotionCor3); option list read from `CInput.cpp` on raw GitHub
- [scipion-em-motioncorr](https://pypi.org/project/scipion-em-motioncorr) (EER `.gain` defects)
- [CTFFIND5, eLife 97227](https://elifesciences.org/articles/97227)
- [About CryoSPARC Live](https://guide.cryosparc.com/live/about-cryosparc-live), [cryoSPARC exposure curation tutorial](https://cryosparc.com/docs/tutorials/exposure-curation), [relative ice thickness forum thread](https://discuss.cryosparc.com/t/exposure-curation-suggestions-and-feature-requests/3197), [cryoSPARC motion 1e31 bug](https://discuss.cryosparc.com/t/bug-with-arbitrary-high-motion-in-live-mode/7776)
- [CryoFLARE preprint](https://www.biorxiv.org/content/10.1101/861740v1), [SmartScope, eLife](https://elifesciences.org/articles/80047)
- [Warp on SBGrid](https://sbgrid.org/software/titles/warp), [Warp gain_flip_y thread](https://groups.google.com/g/warp-em/c/c1UX_vKzbJU)
- PyPI JSON for versions: starfile, mdocfile, cryosparc-tools, rosettasciio, ncempy, mrcfile, pyimagej, scyjava, bioio, watchdog, napari, murfey, cryoemservices, ccpem-pipeliner, cryolo, cupy-cuda12x, torch-ctf, torch-fourier-shift
- npm: molstar 5.13.1, @niivue/niivue 0.69.0 (bundle checked: no MRC reader)
- From memory, not fetched: EPU stage/timestamp XPaths, Tomo5 file naming, K3 file sizes, Falcon frame rates, ALS Λ value, Grant & Grigorieff constants, chokidar v4 dropping glob support, inotify/NFS semantics.

# handheld-lidar-benchmark

A small, **reproducible** protocol to benchmark handheld LiDAR / SLAM scanners
for building documentation. It uses [CloudCompare](https://www.cloudcompare.org/)
as the geometry engine (driven head-less from the command line) and a thin
Python layer for statistics and fitness-for-purpose classification.

The goal is a **fair, instrument-agnostic comparison**: every scanner is measured
with the *same* parameters, so differences reflect the device rather than the
processing.

It works for **any number of scanners** (1, 2, 3, … N). With a single device it
*characterises* it against the fitness-for-purpose scale; with several it ranks
them in one table. Nothing in the tool assumes a fixed instrument count — just
list however many you have in the manifest.

> Developed for a multi-instrument point-cloud comparison study
> (9th ISPRS Low-Cost 3D Workshop, LC3D 2026).

---

## What it measures

| Metric | What it captures | How |
|---|---|---|
| **Point density** | spatial resolution | CloudCompare surface density → median pts/m² and equivalent point spacing (mm) |
| **Surface noise (σ)** | intrinsic sensor noise | RMS of point-to-plane residuals on a flat patch; the dominant plane is isolated with **RANSAC**, so the result is independent of how the patch was segmented |

All metrics, and the p95 geometric deviation against the reference, are mapped
to a 3-level **fitness-for-purpose** scale whose bounds come from published
building-documentation specifications (class bounds):

| Class | Approx. drawing scale | p95 deviation | σ (planar noise) | Spacing |
|---|---|---|---|---|
| **High** | 1:50 | ≤ 5 mm (USIBD LOA40 upper limit) | ≤ 5 mm (HE 2024, s. 3.2.7) | ≤ 5 mm (HE 2024, s. 2.1.2) |
| **Medium** | 1:100 | ≤ 15 mm (USIBD LOA30) | ≤ 15 mm (HE 2024, s. 3.2.7) | ≤ 15 mm (HE 2024, s. 2.1.2) |
| **Low** | 1:200 | ≤ 50 mm (USIBD LOA20) | ≤ 30 mm (HE 2018, handheld/backpack upper end) | ≤ 30 mm (HE 2024, s. 2.1.2) |

Beyond the Low bound a value is classed **Fail** (USIBD LOA10 for the deviation).
USIBD states its ranges at the 95% confidence level, matched here by the p95
statistic; planar noise is an RMS, comparable with the one-sigma precision of
Historic England. The overall class of a device is the worst of its classes.
These are operational classes, not USIBD certifications.

Sources: USIBD, *Level of Accuracy (LOA) Specification* v3.1 (2025);
Historic England, *Geospatial Survey Specifications for Cultural Heritage*,
HEAG317 (2024); Historic England, *3D Laser Scanning for Heritage*, HEAG155
(2018); GSA, *BIM Guide Series 03: 3D Imaging* (2009), tolerances ±6/±13/±51 mm
for Levels 3/2/1.

---

## Why one common scene matters

If each scanner captures a *different* building, the instrument effect is
confounded with site, geometry, materials and operator: you cannot attribute
a measured difference to the device. **Scan at least one shared environment
with every instrument.** That also enables a direct cloud-to-cloud comparison
(CloudCompare's C2C / M3C2) against the most accurate device as a pseudo-reference.

---

## Install

```bash
git clone https://github.com/<you>/handheld-lidar-benchmark.git
cd handheld-lidar-benchmark
pip install -r requirements.txt
```

You also need a CloudCompare installation (≥ 2.11). The tool auto-detects it on
Windows / macOS / Linux; otherwise pass `--cc-exe /path/to/CloudCompare` or set
the `CLOUDCOMPARE` environment variable.

---

## Easiest: the desktop app (no terminal)

Double-click **`LiDAR Benchmark.bat`** (or run `python gui.py`). A window opens:

1. **Add scan…** → pick a point-cloud file. The app computes density **and**
   surface noise automatically — it finds a **vertical wall** on its own (RANSAC +
   a local patch), so noise is always measured on a comparable surface and you do
   **not** need to segment anything in CloudCompare.
2. Repeat for each scanner (the filename becomes the label).
3. Select the best instrument (e.g. the BLK2GO) and click **★ Set reference**, then
   **Deviation vs ref**: the app co-registers every other scan onto the reference and
   measures the **geometric deviation** (how far the low-cost scanner is from the best
   one). *(requires `open3d`; ~2 min per scanner.)*
4. **Save table** → writes `comparison.csv` and `comparison.md`. **Open table** shows it.

*Acquisition time* is read automatically from the LAS GPS-time; for devices without it
(e.g. iPhone/Polycam), double-click the **Acq. (min)** cell to type it in.

The command line below does exactly the same and is what makes the benchmark
scriptable / reproducible.

## Usage (command line)

**1. Density + automatic noise** on a full scan (any format CloudCompare reads:
`.las`, `.e57`, `.ply`, `.pcd`, …) — one command gives spacing **and** σ:

```bash
python benchmark.py scan --cloud BLK2GO.las --label BLK2GO
```

**2. Surface noise (σ)** on a flat patch. In CloudCompare, segment a clean wall
(Segment tool → keep a roughly planar patch) and save it; then:

```bash
python benchmark.py plane --wall BLK2GO_wall.bin --label BLK2GO
```

RANSAC isolates the dominant plane, so a slightly dirty segmentation is fine.

**3. Direct comparison vs a reference (M3C2)** — *only when two scanners captured
the **same** scene* (e.g. the shared environment). Uses CloudCompare's M3C2 plugin
and reports the mean ± σ of the point-to-surface distance, in mm, against a
reference scan (typically the most accurate device):

```bash
python benchmark.py m3c2 --reference BLK2GO_shared.las --compared FJD_shared.las --label FJD_Trion
```

Tune the scales to your scene with `--normal-scale`, `--proj-scale`, `--max-depth`
(defaults: 0.30 / 0.20 / 5.0 m). The M3C2 distance column is auto-detected; override
with `--dist-col N` if needed.

**3b. Geometric deviation vs the reference (recommended)** — the question at the
heart of the benchmark: *how far does a low-cost scanner deviate from the best
instrument?* This **co-registers** the target onto the reference (FPFH+RANSAC global
alignment + point-to-plane ICP, Open3D) and then runs M3C2, reporting mean ± σ, the
95th-percentile |deviation|, the fitness class and the % of points within 5 / 15 / 50 mm:

```bash
python benchmark.py deviation --reference BLK2GO.las --target Raven.las --label Raven
```

Unlike `m3c2`, this does **not** assume the clouds are already aligned. Requires `open3d`
and `plyfile`. The result records `registration_fitness` and `m3c2_valid_pct`; if these are
low the deviation is flagged `REGISTRATION FAILED` (automatic alignment did not find enough
overlap) rather than reported as a real number.

**Assisted registration (for real partial-overlap / different-frame scans).** Automatic
FPFH+RANSAC alignment is unreliable on heterogeneous outdoor scans captured in different SLAM
frames. In that case give ICP a rough manual seed:

1. In CloudCompare open the reference and the target, select both, and run
   **Tools → Registration → Align (point pairs picking)**.
2. Pick 3–4 corresponding points on each cloud, **Align**, and copy the 4×4 matrix from the
   Console into a text file (4 lines of 4 numbers), e.g. `Raven_init.txt`.
3. Re-run with the seed — multi-scale ICP refines it and M3C2 measures the deviation:

```bash
python benchmark.py deviation --reference BLK2GO.las --target Raven.las --label Raven --init-transform Raven_init.txt
```

In the desktop app this is the **“Deviation (assisted)…”** button (select the scan, pick its matrix).

**4. Build the comparison table** (CSV + Markdown):

```bash
python benchmark.py report
```

M3C2 columns appear in the table automatically when M3C2 results are present.

**One-shot batch** for every scanner, from a manifest:

```bash
python benchmark.py batch --manifest scanners.example.json
```

---

## v2: TLS reference and measured AI layers

Version 2 (see `../CHANGELOG.md`) adds a reference-based workflow and two AI
support layers that are themselves *measured components*, not trusted tools.

**Reference in the manifest.** Add a `"reference"` entry (see
`scanners.fontaniva.json`): the batch measures the reference like any scan,
then runs `deviation` (co-registration + M3C2) for every enabled device
against it. The report gains p95 |M3C2|, % within 5/15/50 mm, a deviation
class and an overall class (worst of the available classes). Devices with
`"enabled": false` are skipped. E57 input is supported everywhere; for E57
the acquisition time is entered manually (`acq_min` in the manifest).

**Standardized renders (`render_std.py`).** Deterministic orthographic
renders (numpy + Pillow, no GPU): five locked views plus a horizontal
section at +1.20 m, identical resolution, point size and colour ramp for
every cloud, full parameter audit in `render_params.json`. These are the
blind inputs of the VLM screening and the paper figures:

```bash
python render_std.py --cloud results/_prep/BLK2GO.las --label BLK2GO --out results/renders
```

**VLM visual pre-screening (`vlm_screen.py`).** No paid API: `prepare`
builds anonymised, blind evaluation packets (versioned fixed prompt, closed
artefact rubric: drift, ghosting, wall duplication, holes, excess noise;
JSON response schema). Each packet is then assessed by ONE fresh LLM session
with empty context (protocol: fixed prompts, independent fresh-context
repetitions, inter-run consistency reported). `collect` aggregates verdicts,
`correlate` computes Spearman correlation against the quantitative metrics,
`kappa` computes weighted Cohen / Fleiss agreement against human ratings.

**Fault injection (`fault_inject.py`).** Measures the LLM orchestrator as a
fault detector: `prepare` builds clean and contaminated runs (degraded
registration, corrupted logs, synthetic outliers) with sealed ground truth
kept outside the evaluation packets; fresh LLM sessions review each run;
`collect` scores recall, precision and inter-run consistency per fault type.

**Visual scorecards (`visual_scorecard.py`).** Translates `comparison.csv`
into non-expert material: one card per device with traffic-light classes and
tangible everyday equivalents (credit card, 1-euro coin, pencil, golf ball),
a common 0-50 mm ruler, and a comparison poster (also 16:9 for slides).

---

## Output

* `results/<label>.json` and `results/<label>_plane.json` — raw per-scanner results.
* `comparison.csv` — machine-readable table (open in Excel / pandas).
* `comparison.md` — human-readable table.

Example:

| Scanner | Points | Density (pts/m²) | Spacing (mm) | Density class | Noise σ (mm) | Noise class |
|---|---|---|---|---|---|---|
| BLK2GO | 11932734 | 4201.7 | 15.43 | Low | 3.9 | High |

---

## Handedness check (mirrored exports)

Every `deviation` run also asks a question a rigid registration cannot answer on
its own: **does the target fit the reference better as a mirror image?**

A rigid transform has determinant +1, so it can rotate and translate a cloud but
never un-mirror it. A rectangular room is nearly symmetric under reflection, so
ICP converges happily on a mirrored cloud, the walls line up, and the residual
mismatch is then charged to the scanner instead of to the file that delivered it.
The check reflects the registered target, re-runs ICP from each of the four
cardinal yaws, and compares the two hypotheses; a reflection cannot be undone by
any rotation, so only one of them can fit well.

The result goes into `results/<label>_deviation.json`:

```json
"handedness": {
  "direct_fitness": 0.714, "direct_rmse_mm": 39.5,
  "mirrored_fitness": 0.796, "mirrored_rmse_mm": 39.1,
  "source_is_mirrored": true
}
```

When the mirrored hypothesis wins, the deviation class is reported as
`MIRRORED SOURCE`: the numbers describe the export defect, not the instrument.
Fix the source data (reflect one axis) and run again.

This is not hypothetical. In the case study of this repository the smartphone
export was written in a left-handed frame, which cost **365.4 mm** of apparent
p95 deviation against **48.8 mm** once corrected: the difference between a device
that fails outright and one that lands in the Low class. Neither the dimensional
check distances (a reflection preserves lengths) nor the visual pre-screening
could reveal it.

---

## Locked parameters (equity)

Identical for every scanner (edit `DEFAULTS` in `benchmark.py` or the manifest):

* density radius: **0.05 m**
* roughness kernel: **0.03 m**
* RANSAC inlier threshold: **0.015 m** (σ is stable in the 0.010–0.020 m band)
* no subsampling before the density/noise computation (export step is for I/O only)

---

## Limitations

This protocol quantifies **intrinsic, per-scan** quality (noise, density). It does
**not**, on its own, establish absolute accuracy — that requires a ground-truth
reference (TLS, total station, or certified scale bars). Density depends on scan
duration and stand-off distance; report the acquisition protocol alongside the
numbers.

## License

MIT — see [LICENSE](LICENSE).

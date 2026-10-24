# Changelog

## v2.2 (2026-10-24)

Code and results aligned with the LC3D 2026 manuscript (revision of
2026-10-10): a handedness check on the co-registration and class thresholds
anchored on the published standards.

### Added

- **Handedness check** (`handedness_check()` in `benchmark.py`): every
  `deviation` run tests whether the registered cloud fits better mirrored. A
  rigid transform has determinant +1 and cannot undo a reflection, yet on a
  rectangular room ICP still converges and the residual is charged to the
  instrument instead of the file. The check reflects the registered cloud,
  reruns ICP from the four cardinal axes and compares the two hypotheses. The
  outcome is stored as `handedness` in the JSON; when the mirrored hypothesis
  wins, the class becomes `MIRRORED SOURCE`.
- **Case-study finding**: the SiteScape v1.8.3 export (e57 and PLY alike, which
  differ by a pure rotation and therefore share handedness) delivers the room
  mirrored. With the corrected cloud the M3C2 p95 drops from 365.36 to
  48.75 mm and the class from Fail to Low. Density, spacing, planar noise and
  check distance are unchanged, as expected for an isometry.
- **New results**: `IPHONE17_FIX_deviation.json` (corrected cloud, seeded
  registration), `IPHONE17_ORIG_SEEDED_deviation.json` (control: the original
  cloud with its best registration reproduces 365.36 mm, so the difference
  comes from the reflection and not from the alignment),
  `IPHONE17_MIRR_deviation.json` (corrected cloud, automatic registration:
  349.77 mm, the coarse FPFH+RANSAC step misses the right basin) and
  `IPHONE17_MIRR_SCAN.json` (scan metrics of the corrected cloud).

### Changed

- **Class thresholds from the standards**: spacing and planar noise
  5/15/30 mm from Historic England 2024 (HEAG317, s. 2.1.2 and 3.2.7: point
  cloud at 1:50/1:100/1:200, TLS precision at 1:50/1:100), with the Low noise
  bound taken from the upper limit of handheld mobile systems in Historic
  England 2018. Deviation p95 unchanged at 5/15/50 mm, the upper limits of
  USIBD LOA40/30/20. Previously spacing 5/10/25 mm and noise 5/10/30 mm.
- **Class labels**: "High (LOA30)" and the others were shifted one level
  against USIBD (5 mm is the LOA40 limit). Classes are now High, Medium, Low
  and Fail (formerly "Insufficient"), as in the paper. Updated `benchmark.py`,
  `visual_scorecard.py` and the README (class-bounds table with sources).
- **Results reclassified** without recomputing the metrics (`results/*.json`,
  `comparison.*`). Only two rows change: `TRION_P2_geo` (spacing 11.95 mm) and
  `TRION_P2_room` (spacing 14.01 mm) move from Low to Medium, and the geo run
  becomes Medium overall. Table 2 of the paper is unchanged.

### Fixed

- **Fault-injection generator**: `_jitter` could push the ICP fitness above 1.
  Clean run run_05 of the July campaign had fitness 1.006 and was flagged in
  all 10 repetitions. Fitness is now capped at 1 after the random draw, with
  the random sequence unchanged. The archived campaign was not regenerated:
  the paper reports precision 0.62 over all runs and 0.71 without run_05. The
  class tables in `fault_inject.py` stay as in July, so the archived runs
  regenerate identically.

### Known limitation

- On the corrected cloud the good result is reached only with a seeded
  registration (`--init-transform`); automatic global registration fails on
  both the original and the corrected cloud.

## v2.1 (2026-07-28)

Data release accompanying the revised LC3D 2026 manuscript. The tool itself is
unchanged: this release publishes the completed AI-reliability campaign and
corrects one bookkeeping field.

### Added

- **Fault injection at full scale**: 100 verdicts (10 runs x 10 independent
  fresh-context repetitions), replacing the 2-repetition pilot. Per-repetition
  recall 0.940 at precision 0.618, recall 1.000 by per-run majority, fault-type
  accuracy 0.830, mean inter-repetition consistency 0.810. False positives on
  clean runs 0.580, most of them runs built from genuinely drifted smartphone
  data: telling a faulty pipeline from a faithful record of a poor scan is the
  hard case.
- **VLM pre-screening at full scale**: 30 blind verdicts (3 clouds x 10
  repetitions). Across all 30 the screener never labels drift as drift; the
  smartphone distortion reads as holes.

### Fixed

- `ground_truth.json` still recorded the 2 planned repetitions of the set built
  on 2026-07-09, although the campaign was extended to 10 on 2026-07-27. The
  field now reflects the campaign actually executed; `metrics.csv` and
  `summary.md` were regenerated, and every derived figure is unchanged.

### Known limitations

- The VLM screening covers the RTC360 reference, the BLK2GO and the iPhone. The
  FJD Trion P2 is not part of the screened set.
- The Spearman correlation of VLM overall usability against p95 deviation rests
  on two clouds only, and is reported as descriptive rather than evidential.
- Weighted kappa against independent human assessors is not yet available.


## v2.0 (2026-07-22)

Release accompanying the LC3D 2026 submission. Adds a TLS-referenced workflow
and two AI support layers that are themselves measured rather than trusted.

### Added

- **E57 input** accepted by every command. For E57 the acquisition duration is
  supplied manually (`--duration-min`, or `acq_min` in the manifest); GPS-time
  remains automatic for LAS/LAZ.
- **Reference device in the manifest.** A `reference` entry makes the batch
  measure the reference like any other scan and then run co-registration plus
  M3C2 deviation for every enabled device against it. Devices can be held back
  with `"enabled": false`; manifest paths resolve relative to the manifest.
- **Extended report**: p95 |M3C2| deviation, share of surface within 5/15/50 mm,
  deviation class and an overall class (the worst of the available classes).
  Outliers beyond 3 sigma are excluded from the M3C2 statistics and the excluded
  fraction is recorded.
- **`render_std.py`**: deterministic standardized renders (numpy + Pillow, no
  GPU). Five locked orthographic views plus a horizontal section at +1.20 m,
  1600x1200, fixed colour ramp, stable z-buffer, full parameter audit in
  `render_params.json`.
- **`vlm_screen.py`**: harness for VLM-based visual pre-screening without any
  paid API. `prepare` builds anonymised blind evaluation packets (versioned
  fixed prompt, closed artefact rubric, JSON response schema); each packet is
  assessed by one fresh LLM session with empty context; `collect`, `correlate`
  and `kappa` aggregate the verdicts, correlate them with the quantitative
  indicators (Spearman) and score agreement against human ratings (weighted
  Cohen, Fleiss).
- **`fault_inject.py`**: fault-injection protocol measuring the LLM
  orchestrator as a fault detector. Clean and contaminated runs (degraded
  registration, corrupted logs, synthetic outliers) with sealed ground truth
  kept outside the evaluation packets; `collect` scores recall, precision and
  inter-repetition consistency per fault type.
- **`visual_scorecard.py`**: non-expert reporting. One card per device with
  traffic-light classes and everyday tangible equivalents (credit card, 1-euro
  coin, pencil, golf ball), a shared 0-50 mm ruler and a comparison poster,
  including a 16:9 variant for slides.

### Changed

- M3C2 defaults aligned with the paper: core points 0.05 m, projection 0.20 m,
  normal scale 0.30 m.
- Registration failures are reported explicitly instead of surfacing as
  plausible-looking numbers.
- Requirements now include pillow and matplotlib. No dependency on any paid API.

### Notes on the released data

`results/` holds the derived products of the case study: per-scan metrics,
deviation results, standardized renders, scorecards, the blind VLM evaluation
packets with their prompts, and the fault-injection set with its sealed ground
truth. The raw point clouds are not included here because of their size; see the
README for how to obtain them.

## v1.0 (2026-06)

Initial release: reproducible density and planar-noise benchmark driving
CloudCompare from the command line, fitness-for-purpose classification against
USIBD LOA and drawing-scale tolerances, desktop GUI, and geometric deviation via
FPFH+RANSAC and point-to-plane ICP co-registration followed by M3C2.

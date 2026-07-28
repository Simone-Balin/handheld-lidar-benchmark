# Changelog

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

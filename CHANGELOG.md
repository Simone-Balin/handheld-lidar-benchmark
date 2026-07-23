# Changelog

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

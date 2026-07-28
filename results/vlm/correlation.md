# VLM screening vs quantitative metrics: Spearman rank correlation

Devices matched: 3 (BLK2GO, IPHONE17, RTC360)
VLM side: median severity per artifact and median overall usability across the fresh-context repetitions (prompt `vlm-rubric-1.0`).

| VLM variable | sigma_mm | spacing_mm | p95_mm |
|---|---|---|---|
| drift | n/a | n/a | n/a |
| ghosting | n/a | n/a | n/a |
| wall_duplication | n/a | n/a | n/a |
| holes | +0.50 (n=3) | -0.50 (n=3) | +1.00 (n=2) |
| excess_noise | n/a | n/a | n/a |
| overall_usability | -0.87 (n=3) | +0.00 (n=3) | -1.00 (n=2) |

## Method
Spearman's rho is computed in numpy as the Pearson correlation of the
average ranks (ties get the mean of their rank positions); this tie-safe
general form is used instead of the 6*sum(d^2) shortcut. No scipy.

## Limitations (declared)
- Only n = 3 devices: rho is reported as a descriptive, exploratory
  statistic. No p-values or significance claims are made; with n = 3 the
  possible rho values are only 0, ±0.5 and ±1, so any single coefficient
  is uninformative in isolation.
- Median severities are ordinal (0-3) with heavy ties; ranks are averaged,
  and a constant column yields 'n/a' (no rank order to correlate).
- Correlation here indicates monotonic association only, not calibration
  of the VLM scores against the metric scale.

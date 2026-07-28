# VLM screening: BLK2GO

Blind alias: `cloud_B` · prompt `vlm-rubric-1.0` · 10/10 verdicts collected · models: claude-sonnet-5 · dates: 2026-07-09, 2026-07-27

Protocol: fixed prompts, independent fresh-context repetitions, inter-run consistency reported.

| Artifact | Median severity (0-3) | IQR | Consistency (±1 of median) | Median confidence |
|---|---|---|---|---|
| drift | 0 | [0, 1] | 100% | 0.62 |
| ghosting | 0 | [0, 0] | 100% | 0.6 |
| wall_duplication | 0 | [0, 0] | 100% | 0.7 |
| holes | 0 | [0, 0] | 100% | 0.57 |
| excess_noise | 1 | [1, 1] | 100% | 0.53 |

**Overall usability (0-3)**: median 3, IQR [3, 3], consistency 100% (n = 10)

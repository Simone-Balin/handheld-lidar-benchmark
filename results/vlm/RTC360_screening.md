# VLM screening: RTC360

Blind alias: `cloud_A` · prompt `vlm-rubric-1.0` · 2/2 verdicts collected · models: claude-sonnet-5 · dates: 2026-07-09

Protocol: fixed prompts, independent fresh-context repetitions, inter-run consistency reported.

| Artifact | Median severity (0-3) | IQR | Consistency (±1 of median) | Median confidence |
|---|---|---|---|---|
| drift | 0.5 | [0.25, 0.75] | 100% | 0.55 |
| ghosting | 0 | [0, 0] | 100% | 0.68 |
| wall_duplication | 0 | [0, 0] | 100% | 0.77 |
| holes | 0.5 | [0.25, 0.75] | 100% | 0.53 |
| excess_noise | 1.5 | [1.25, 1.75] | 100% | 0.57 |

**Overall usability (0-3)**: median 2.5, IQR [2.25, 2.75], consistency 100% (n = 2)

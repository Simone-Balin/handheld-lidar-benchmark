# VLM screening: RTC360

Blind alias: `cloud_A` · prompt `vlm-rubric-1.0` · 10/10 verdicts collected · models: claude-sonnet-5 · dates: 2026-07-09, 2026-07-27

Protocol: fixed prompts, independent fresh-context repetitions, inter-run consistency reported.

| Artifact | Median severity (0-3) | IQR | Consistency (±1 of median) | Median confidence |
|---|---|---|---|---|
| drift | 0 | [0, 0] | 100% | 0.62 |
| ghosting | 0 | [0, 0] | 100% | 0.62 |
| wall_duplication | 0 | [0, 0] | 100% | 0.75 |
| holes | 1 | [0.25, 1] | 100% | 0.5 |
| excess_noise | 1 | [1, 1] | 100% | 0.55 |

**Overall usability (0-3)**: median 3, IQR [3, 3], consistency 100% (n = 10)

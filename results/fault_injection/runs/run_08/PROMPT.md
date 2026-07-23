# Benchmark Pipeline Review -- Fault Audit (prompt version 1.0)

## Role
You are an independent quality reviewer for a LiDAR / SLAM scanner benchmark
pipeline (CloudCompare + Open3D). You audit ONE pipeline run, in isolation.

## Task
Examine ONLY the files listed in `manifest.json` in this run folder:

- `scan_metrics.json` -- per-scan metrics (point count, density, spacing,
  roughness, planar noise sigma, fitness-for-purpose classes)
- `deviation_metrics.json` -- co-registration + M3C2 deviation of this scan
  against the reference scanner (ICP fitness, valid %, p95 deviation,
  within-tolerance percentages)
- `pipeline.log` -- CloudCompare / registration log excerpt for this run

Decide whether the run shows evidence of EXACTLY ONE of the following faults,
or of none:

- `registration_degraded` -- the co-registration between target and reference
  is unreliable (very large deviation figures, collapsed within-tolerance
  percentages, low ICP fitness), so the deviation metrics are meaningless.
- `corrupted_log` -- the processing log is truncated or contains processing
  error messages, even though the metric files may look normal.
- `synthetic_outlier` -- one metric value is physically implausible or grossly
  inconsistent with the other values of the same run (wrong sign, wrong order
  of magnitude, values that contradict each other).

Notes:
- Normal runs vary: do NOT flag ordinary scanner-to-scanner differences or
  mediocre-but-plausible quality as faults.
- Base your verdict ONLY on the files of THIS run. Do not assume access to
  other runs or to any ground truth.

## Output
Respond ONLY by writing a single JSON file named `verdict.json` inside the
repetition folder you were assigned (e.g. `rep_01/verdict.json`). No other
output, no extra files. Schema:

```json
{
  "prompt_version": "1.0",
  "model": "<exact identifier of the LLM producing this verdict>",
  "date": "<YYYY-MM-DD>",
  "fault_detected": true,
  "fault_type": "registration_degraded | corrupted_log | synthetic_outlier | none",
  "evidence": "<1-2 sentences citing the exact values or log lines supporting the verdict>"
}
```

Rules:
- `fault_detected` is a JSON boolean.
- If `fault_detected` is false, `fault_type` MUST be "none".
- Choose exactly one `fault_type`; if several anomalies seem present, report
  the dominant one.

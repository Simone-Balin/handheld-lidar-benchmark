#!/usr/bin/env python3
"""
fault_inject.py -- fault-injection harness for an LLM "pipeline reviewer"
==========================================================================
Companion module of `benchmark.py` (handheld-lidar-benchmark). It measures,
with a controlled fault-injection protocol, how reliably a large language
model used as an automatic quality-control layer detects faults in benchmark
runs. The module itself makes NO API call: it only prepares the evaluation
set and aggregates the verdicts written by external LLM sessions.

Protocol: fixed prompts, independent fresh-context repetitions
--------------------------------------------------------------
1. `prepare` builds a sealed evaluation set: N clean runs mixed with N
   contaminated runs (one known fault each), shuffled with a fixed seed.
   Every run folder contains realistic pipeline artefacts (per-scan metrics
   JSON, deviation metrics JSON, CloudCompare-style log excerpt), a FIXED
   versioned prompt (`PROMPT.md`, identical for every run and repetition)
   and a `manifest.json` listing the files to examine. The truth lives ONLY
   in `ground_truth.json`, outside the run folders; the run folders carry no
   clue (neutral file names, uniform timestamps, shuffled ordering).
2. Evaluation is performed by EXTERNAL LLM sessions (e.g. Claude Code): one
   FRESH session per repetition, with an empty context, so repetitions are
   statistically independent. Each session reads one run folder, follows
   `PROMPT.md` and writes `runs/run_XX/rep_YY/verdict.json`. The prompt never
   changes between runs or repetitions (it is versioned in the verdict).
3. `collect` compares the verdicts against the sealed ground truth and
   computes detection recall/precision (per-repetition pooled and per-run
   majority vote), fault-type accuracy, and inter-repetition consistency.

Fault taxonomy (exactly one per contaminated run)
-------------------------------------------------
(a) registration_degraded : the deviation JSON is rewritten with values
    consistent with a failed co-registration (p95 deviation of hundreds of
    mm, collapsed within-tolerance percentages) and the log reports a low
    ICP fitness. The fitness is kept above the pipeline auto-flag threshold
    so the run is NOT self-labelled as failed: the reviewer must notice.
(b) corrupted_log : the log is truncated mid-line or an error message in
    CloudCompare style is injected, while the metric JSONs look normal.
(c) synthetic_outlier : one metric is physically implausible or grossly
    inconsistent with the rest of the run (density x100 with unchanged
    spacing, negative sigma, sigma x50).

Folder layout produced by `prepare`
-----------------------------------
  <out>/
    ground_truth.json          sealed truth: run_id -> {faulted, fault_type, details}
    runs/
      run_01/
        PROMPT.md              fixed, versioned reviewer prompt (English)
        manifest.json          files to examine + repetition folders
        scan_metrics.json      per-scan metrics (benchmark.py `scan` format)
        deviation_metrics.json deviation vs reference (benchmark.py `deviation` format)
        pipeline.log           CloudCompare-style log excerpt
        rep_01/ .. rep_RR/     empty; each receives one verdict.json
      run_02/ ...

Usage
-----
  python fault_inject.py prepare --source results --n-clean 5 --n-faulted 5 \\
      --seed 42 --reps 10 --out results/fault_injection
  # ... one fresh LLM session per (run, repetition) writes verdict.json ...
  python fault_inject.py collect --dir results/fault_injection

If --source does not exist (real benchmark run not done yet), `prepare`
falls back to plausible embedded templates (declared in the console log),
so the protocol can be tested immediately.

Requires: Python 3.8+, numpy. No other third-party dependency, no API.
"""

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
PROMPT_VERSION = "1.0"
FAULT_KINDS = ("registration_degraded", "corrupted_log", "synthetic_outlier")
VERDICT_TYPES = FAULT_KINDS + ("none",)
DATA_FILES = ("scan_metrics.json", "deviation_metrics.json", "pipeline.log")
GROUND_TRUTH = "ground_truth.json"
VERDICT_FILE = "verdict.json"

# Every artefact in the evaluation set gets the same mtime -> no temporal clue.
_UNIFORM_MTIME = datetime(2026, 7, 8, 12, 0, 0).timestamp()

# Fitness-for-purpose tables mirrored from benchmark.py (kept local so this
# harness is importable/runnable in isolation).
SPACING_CLASSES = [(5, "High (LOA30)"), (10, "Medium (LOA20)"), (25, "Low (LOA10)")]
SIGMA_CLASSES = [(5, "High (LOA30)"), (10, "Medium (LOA20)"), (30, "Low (LOA10)")]
DEVIATION_CLASSES = [(5, "High (LOA30)"), (15, "Medium (LOA20)"), (50, "Low (LOA10)")]

# Registration acceptance rule mirrored from benchmark.analyze_deviation.
_MIN_FITNESS = 0.10
_MIN_VALID_PCT = 30.0


def classify(value_mm, table):
    for thr, name in table:
        if value_mm <= thr:
            return name
    return "Insufficient"


# --------------------------------------------------------------------------- #
# Embedded fallback templates (plausible values, benchmark.py output format)   #
# --------------------------------------------------------------------------- #
_TEMPLATE_SCAN = {
    "label": "unit",
    "cloud": "data/input_cloud.e57",
    "n_points": 41866393,
    "bbox_m": [28.4, 16.9, 6.2],
    "density_radius_m": 0.05,
    "density_median_pts_m2": 31480.0,
    "density_mean_pts_m2": 38240.0,
    "spacing_mm": 5.64,
    "density_class": "Medium (LOA20)",
    "roughness_kernel_m": 0.03,
    "roughness_median_mm": 1.8,
    "roughness_p90_mm": 4.6,
    "sigma_planar_mm": 2.7,
    "noise_class": "High (LOA30)",
    "noise_method": "auto (dominant wall, local patch)",
    "measured_on_wall": True,
    "acq_duration_min": 14.5,
    "acq_s_per_m2": None,
}

_TEMPLATE_DEVIATION = {
    "label": "unit",
    "reference": "data/reference_cloud.e57",
    "target": "data/target_cloud.e57",
    "registration_fitness": 0.874,
    "registration_rmse_mm": 6.51,
    "m3c2_valid_pct": 91.7,
    "registration_ok": True,
    "deviation_mean_mm": 0.62,
    "deviation_std_mm": 4.83,
    "deviation_p95abs_mm": 8.4,
    "deviation_class": "Medium (LOA20)",
    "within_5mm_pct": 74.2,
    "within_15mm_pct": 95.8,
    "within_50mm_pct": 99.6,
}

# --------------------------------------------------------------------------- #
# Fixed reviewer prompt (versioned; identical for every run and repetition)    #
# --------------------------------------------------------------------------- #
PROMPT_MD = """# Benchmark Pipeline Review -- Fault Audit (prompt version {version})

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
{{
  "prompt_version": "{version}",
  "model": "<exact identifier of the LLM producing this verdict>",
  "date": "<YYYY-MM-DD>",
  "fault_detected": true,
  "fault_type": "registration_degraded | corrupted_log | synthetic_outlier | none",
  "evidence": "<1-2 sentences citing the exact values or log lines supporting the verdict>"
}}
```

Rules:
- `fault_detected` is a JSON boolean.
- If `fault_detected` is false, `fault_type` MUST be "none".
- Choose exactly one `fault_type`; if several anomalies seem present, report
  the dominant one.
"""


# --------------------------------------------------------------------------- #
# Generic helpers                                                              #
# --------------------------------------------------------------------------- #
def _deepcopy(obj):
    return json.loads(json.dumps(obj))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(repr(o))


def _write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False,
                                     default=_json_default) + "\n",
                          encoding="utf-8")


def _numeric_leaves(obj, prefix=()):
    """All (key_path, value) numeric leaves of a nested dict/list structure."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _numeric_leaves(v, prefix + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += _numeric_leaves(v, prefix + (str(i),))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, obj))
    return out


def _find_path(obj, substrs):
    """First numeric leaf whose joined key path contains all substrings."""
    for path, val in _numeric_leaves(obj):
        joined = "/".join(path).lower()
        if all(s in joined for s in substrs):
            return list(path), val
    return None, None


def _set_path(obj, path, value):
    cur = obj
    for k in path[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur[k]
    last = path[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


# --------------------------------------------------------------------------- #
# Artefact preparation: neutralisation, jitter, consistency                    #
# --------------------------------------------------------------------------- #
_NEUTRAL_STRINGS = {
    "label": "unit",
    "cloud": "data/input_cloud.e57",
    "input": "data/input_cloud.e57",
    "wall": "data/wall_patch.e57",
    "compared": "data/target_cloud.e57",
    "reference": "data/reference_cloud.e57",
    "target": "data/target_cloud.e57",
}

# Locked pipeline parameters must NOT vary across runs (same for every scanner).
_JITTER_EXCLUDE = {
    "density_radius_m", "roughness_kernel_m", "ransac_inlier_m", "ransac_iters",
    "normal_scale_m", "proj_scale_m", "max_depth_m", "m3c2_subsample",
}


def _neutralize(d):
    """Strip identifying labels and file paths from a result dict (in place)."""
    for k, repl in _NEUTRAL_STRINGS.items():
        if k in d and isinstance(d[k], str):
            d[k] = repl
    for k, v in list(d.items()):
        if isinstance(v, str) and re.search(r"[A-Za-z]:\\|/home/|/Users/|\\\\", v):
            d[k] = "data/input_cloud.e57"
    return d


def _jitter(obj, rng, key=""):
    """Small multiplicative jitter on numeric leaves so runs are not
    byte-identical copies. Locked parameters are excluded; percentages
    are clipped to [0, 100]."""
    if isinstance(obj, dict):
        return {k: _jitter(v, rng, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jitter(v, rng, key) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float)):
        if key in _JITTER_EXCLUDE:
            return obj
        f = float(np.clip(rng.normal(1.0, 0.06), 0.85, 1.15))
        v = obj * f
        kl = key.lower()
        if "pct" in kl or kl.startswith("within"):
            v = float(np.clip(v, 0.0, 100.0))
        if isinstance(obj, int):
            return int(round(v))
        return round(float(v), 3)
    return obj


def _enforce_scan_consistency(scan):
    """Re-derive dependent quantities after jitter so CLEAN runs stay
    internally consistent (spacing follows density, classes follow values).
    Internal inconsistency must remain an exclusive signature of faults."""
    d_path, d_val = _find_path(scan, ("density", "median"))
    s_path, _ = _find_path(scan, ("spacing",))
    if d_path and s_path and d_val and d_val > 0:
        spacing = 1000.0 / math.sqrt(float(d_val))
        _set_path(scan, s_path, round(spacing, 2))
        if "density_class" in scan:
            scan["density_class"] = classify(spacing, SPACING_CLASSES)
    sigma = scan.get("sigma_planar_mm")
    if isinstance(sigma, (int, float)) and "noise_class" in scan:
        scan["noise_class"] = classify(sigma, SIGMA_CLASSES)
    return scan


def _enforce_deviation_consistency(dev):
    """Re-apply the pipeline's own rules (benchmark.analyze_deviation) so the
    deviation dict is exactly what the real pipeline would have written."""
    keys = ("within_5mm_pct", "within_15mm_pct", "within_50mm_pct")
    if all(k in dev for k in keys):
        vals = sorted(float(dev[k]) for k in keys)
        for k, v in zip(keys, vals):
            dev[k] = round(v, 1)
    fitness = dev.get("registration_fitness")
    valid = dev.get("m3c2_valid_pct")
    if isinstance(fitness, (int, float)) and isinstance(valid, (int, float)):
        ok = fitness >= _MIN_FITNESS and valid >= _MIN_VALID_PCT
        dev["registration_ok"] = bool(ok)
    else:
        ok = bool(dev.get("registration_ok", True))
    p95 = dev.get("deviation_p95abs_mm")
    if isinstance(p95, (int, float)):
        dev["deviation_class"] = classify(p95, DEVIATION_CLASSES) if ok else "REGISTRATION FAILED"
    return dev


# --------------------------------------------------------------------------- #
# CloudCompare-style log synthesis                                             #
# --------------------------------------------------------------------------- #
def _fmt_ts(seconds):
    s = int(seconds) % 86400
    return "[{:02d}:{:02d}:{:02d}]".format(s // 3600, (s % 3600) // 60, s % 60)


class _Clock:
    """Monotonic fake wall clock for log timestamps (uniform style across
    runs: clean and faulted logs are generated by the same process)."""

    def __init__(self, rng, start=None):
        self.rng = rng
        self.t = int(start) if start is not None else int(rng.integers(13 * 3600, 16 * 3600))

    def tick(self, lo=0, hi=6):
        self.t += int(self.rng.integers(lo, hi + 1))
        return _fmt_ts(self.t)


def _build_scan_log(scan, clock, rng):
    """Scan-analysis section, modelled on real CloudCompare -SILENT logs."""
    _, n_points = _find_path(scan, ("n_points",))
    n_points = int(n_points) if n_points else 30000000
    n_sub = max(1000, int(n_points / float(rng.uniform(80, 120))))
    dur = round(float(rng.uniform(20, 140)), 2)
    radius = scan.get("density_radius_m", 0.05)
    kernel = scan.get("roughness_kernel_m", 0.03)
    lines = [
        clock.tick() + " Output export format (clouds) set to: ASC",
        clock.tick() + " Auto-save is disabled",
        clock.tick() + " [LOADING]",
        clock.tick() + " Opening file: 'data/input_cloud.e57'",
        clock.tick(1, 9) + " [I/O] File 'data/input_cloud.e57' loaded successfully",
        clock.tick() + " Found one cloud with %d points" % n_points,
        clock.tick() + " [DENSITY]",
        clock.tick() + "\tSphere radius: %s" % radius,
        clock.tick(2, 14) + " [ROUGHNESS]",
        clock.tick() + "\tKernel size: %s" % kernel,
        clock.tick(4, 18) + " [SUBSAMPLING]",
        clock.tick() + "\tMethod: SPATIAL",
        clock.tick() + "\tSpatial step: 0.1",
        clock.tick(1, 4) + "\tResult: %d points" % n_sub,
        clock.tick() + " [SAVING]",
        clock.tick() + " [I/O] File 'results/scan_metrics.asc' saved successfully",
        clock.tick() + " Processed finished in %.2f s." % dur,
    ]
    return "\n".join(lines) + "\n"


def _build_registration_log(dev, clock, rng):
    """Registration + M3C2 section. Present in EVERY run (clean and faulted)
    with the same structure; only the numbers change. The values mirror the
    deviation JSON so both artefacts tell the same story."""
    fitness = float(dev.get("registration_fitness", 0.87))
    rmse_m = float(dev.get("registration_rmse_mm", 6.5)) / 1000.0
    valid = float(dev.get("m3c2_valid_pct", 92.0))
    n_core = int(rng.integers(180000, 620000))
    dur = round(float(rng.uniform(40, 220)), 2)
    lines = [
        clock.tick(2, 10) + " [REGISTRATION] Coarse alignment: FPFH + RANSAC (voxel 0.04 m)",
        clock.tick(4, 25) + " [ICP] Point-to-plane refinement (multi-scale)",
        clock.tick(4, 30) + " [ICP] Converged: fitness=%.3f, inlier RMSE=%.6f m" % (fitness, rmse_m),
        clock.tick() + " [M3C2] Parameters file: m3c2_params.txt",
        clock.tick() + " [M3C2] Core points sub-sampling: 0.05 m",
        clock.tick(5, 40) + " [M3C2] Core points: %d" % n_core,
        clock.tick(1, 8) + " [M3C2] Distances computed (valid: %.1f%%)" % valid,
        clock.tick() + " [SAVING]",
        clock.tick() + " [I/O] File 'results/deviation_metrics.json' saved successfully",
        clock.tick() + " Processed finished in %.2f s." % dur,
    ]
    return "\n".join(lines) + "\n"


def _adapt_source_log(text, scan, rng):
    """Neutralise a real CloudCompare log excerpt: strip file paths, patch the
    point count so it matches the (jittered) scan JSON, and return the start
    time for the appended registration section."""
    text = re.sub(r"'[^'\n]*[/\\][^'\n]*'", "'data/input_cloud.e57'", text)
    _, n_points = _find_path(scan, ("n_points",))
    if n_points:
        text = re.sub(r"cloud with \d+ points", "cloud with %d points" % int(n_points), text)
    stamps = re.findall(r"\[(\d{2}):(\d{2}):(\d{2})\]", text)
    start = None
    if stamps:
        h, m, s = stamps[-1]
        start = int(h) * 3600 + int(m) * 60 + int(s) + int(rng.integers(3, 20))
    return text.rstrip("\n") + "\n", start


# --------------------------------------------------------------------------- #
# Fault injection                                                              #
# --------------------------------------------------------------------------- #
def _fault_registration_degraded(dev, rng):
    """Rewrite the deviation dict as after a skipped/failed co-registration.
    Fitness stays just above the pipeline auto-flag threshold (0.10) so the
    run is NOT self-labelled: the reviewer has to read the numbers."""
    before_p95 = dev.get("deviation_p95abs_mm")
    before_fit = dev.get("registration_fitness")
    fit = round(float(rng.uniform(0.10, 0.30)), 3)
    w5 = round(float(rng.uniform(0.2, 2.5)), 1)
    w15 = round(min(100.0, w5 + float(rng.uniform(0.5, 3.0))), 1)
    w50 = round(min(100.0, w15 + float(rng.uniform(2.0, 9.0))), 1)
    dev.update({
        "registration_fitness": fit,
        "registration_rmse_mm": round(float(rng.uniform(80, 220)), 2),
        "m3c2_valid_pct": round(float(rng.uniform(31, 58)), 1),
        "deviation_mean_mm": round(float(rng.uniform(-60, 60)), 3),
        "deviation_std_mm": round(float(rng.uniform(140, 420)), 3),
        "deviation_p95abs_mm": round(float(rng.uniform(350, 900)), 2),
        "within_5mm_pct": w5,
        "within_15mm_pct": w15,
        "within_50mm_pct": w50,
    })
    _enforce_deviation_consistency(dev)
    return {
        "file": "deviation_metrics.json",
        "p95_before_mm": before_p95,
        "p95_after_mm": dev["deviation_p95abs_mm"],
        "fitness_before": before_fit,
        "fitness_after": dev["registration_fitness"],
        "note": "low ICP fitness also mirrored in pipeline.log",
    }


def _fault_synthetic_outlier(scan, rng):
    """Corrupt ONE metric of the scan JSON far beyond 3 sigma of plausibility
    (x100 density with stale spacing, negative sigma, x50 sigma)."""
    variants = ["density_x100", "sigma_negative", "sigma_x50"]
    order = [variants[i] for i in rng.permutation(len(variants))]
    for variant in order:
        if variant == "density_x100":
            path, val = _find_path(scan, ("density", "median"))
            if path is None:
                path, val = _find_path(scan, ("density",))
            if path is None:
                continue
            injected = round(float(val) * 100.0, 1)
            _set_path(scan, path, injected)
            # companion mean corrupted the same way (a unit bug hits both);
            # spacing and density_class are deliberately left stale -> the
            # internal inconsistency is the detectable signature
            m_path, m_val = _find_path(scan, ("density", "mean"))
            if m_path:
                _set_path(scan, m_path, round(float(m_val) * 100.0, 1))
            return {"file": "scan_metrics.json", "variant": variant,
                    "metric": "/".join(path), "original": val, "injected": injected}
        else:
            path, val = _find_path(scan, ("sigma",))
            if path is None:
                continue
            if variant == "sigma_negative":
                injected = round(-abs(float(val)), 3)
            else:
                injected = round(float(val) * 50.0, 2)
            _set_path(scan, path, injected)
            # naive pipeline propagation: the class is re-derived from the
            # corrupted value exactly as benchmark.py would do
            s = scan.get("sigma_planar_mm")
            if isinstance(s, (int, float)) and "noise_class" in scan:
                scan["noise_class"] = classify(s, SIGMA_CLASSES)
            return {"file": "scan_metrics.json", "variant": variant,
                    "metric": "/".join(path), "original": val, "injected": injected}
    # last-resort fallback: negative point count
    path, val = _find_path(scan, ("n_points",))
    if path is not None:
        _set_path(scan, path, -abs(int(val)))
        return {"file": "scan_metrics.json", "variant": "n_points_negative",
                "metric": "/".join(path), "original": val, "injected": -abs(int(val))}
    raise RuntimeError("no metric available for synthetic_outlier injection")


def _fault_corrupted_log(text, rng):
    """Truncate the log mid-line, or inject CloudCompare-style error lines.
    The metric JSONs stay untouched."""
    if float(rng.random()) < 0.5:
        cut = int(len(text) * float(rng.uniform(0.45, 0.70)))
        cut = max(cut, 10)
        while cut > 1 and text[cut - 1] == "\n":
            cut -= 1
        return text[:cut], {"file": "pipeline.log", "variant": "truncated_mid_line",
                            "cut_at_char": int(cut), "original_chars": len(text)}
    lines = text.rstrip("\n").split("\n")
    lo = len(lines) // 2
    idx = int(rng.integers(lo, max(lo + 1, len(lines) - 1)))
    m = re.match(r"(\[\d{2}:\d{2}:\d{2}\])", lines[idx])
    ts = m.group(1) if m else "[00:00:00]"
    err = [ts + " [ERROR] Failed to load file 'data/input_cloud.e57'",
           ts + " An error occurred during this process! Result may be incomplete!"]
    lines[idx + 1:idx + 1] = err
    return "\n".join(lines) + "\n", {"file": "pipeline.log", "variant": "error_injected",
                                     "after_line": idx + 1, "lines": err}


# --------------------------------------------------------------------------- #
# Source discovery (real benchmark results)                                    #
# --------------------------------------------------------------------------- #
def _discover_source(source_dir):
    """Harvest real artefacts from a benchmark.py results directory:
    per-scan JSONs, *_deviation.json, *_scan.log (any *.log as fallback)."""
    scans, deviations = [], []
    for f in sorted(source_dir.glob("*.json")):
        if f.name in (GROUND_TRUTH, VERDICT_FILE, "manifest.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="ignore"))
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        stem = f.stem
        if stem.endswith("_deviation"):
            deviations.append(data)
        elif stem.endswith(("_plane", "_m3c2")):
            continue
        elif "label" in data and _find_path(data, ("density",))[0] is not None:
            scans.append(data)
    logs = []
    log_files = sorted(source_dir.glob("*_scan.log")) or sorted(source_dir.glob("*.log"))
    for f in log_files:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if txt.strip():
            logs.append(txt)
    return scans, deviations, logs


# --------------------------------------------------------------------------- #
# prepare                                                                      #
# --------------------------------------------------------------------------- #
def cmd_prepare(args):
    out = Path(args.out)
    runs_root = out / "runs"
    out.mkdir(parents=True, exist_ok=True)
    if runs_root.exists():
        print("[prepare] removing existing evaluation set: %s" % runs_root)
        shutil.rmtree(runs_root)
    runs_root.mkdir(parents=True)

    # -- source artefacts (real results) or embedded-template fallback ------- #
    src = Path(args.source) if args.source else None
    scans, deviations, logs = [], [], []
    if src is not None and src.is_dir():
        scans, deviations, logs = _discover_source(src)
    if not scans:
        print("[prepare] WARNING: no usable scan results in source '%s' -> "
              "falling back to embedded templates (synthetic but realistic "
              "artefacts modelled on benchmark.py output)." % args.source)
    if scans and not deviations:
        print("[prepare] NOTE: no *_deviation.json in source -> deviation "
              "artefacts generated from the embedded template.")
    src_desc = {
        "source_dir": str(src) if src else None,
        "scan_artefacts": "real (%d found)" % len(scans) if scans else "embedded_template",
        "deviation_artefacts": "real (%d found)" % len(deviations) if deviations else "embedded_template",
        "log_artefacts": "real (%d found)" % len(logs) if logs else "embedded_template",
    }

    # -- balanced fault mix, shuffled with the master seed ------------------- #
    kinds = ["clean"] * args.n_clean + [FAULT_KINDS[i % len(FAULT_KINDS)]
                                        for i in range(args.n_faulted)]
    master = np.random.default_rng(args.seed)
    order = [kinds[i] for i in master.permutation(len(kinds))]

    prompt_text = PROMPT_MD.format(version=PROMPT_VERSION)
    rep_names = ["rep_%02d" % r for r in range(1, args.reps + 1)]
    truth_runs = {}

    for i, kind in enumerate(order, start=1):
        run_id = "run_%02d" % i
        rng = np.random.default_rng([args.seed, i])  # per-run deterministic stream
        run_dir = runs_root / run_id
        run_dir.mkdir()

        # base artefacts: sample from real pool or copy the template
        scan = _deepcopy(scans[int(rng.integers(len(scans)))]) if scans else _deepcopy(_TEMPLATE_SCAN)
        dev = _deepcopy(deviations[int(rng.integers(len(deviations)))]) if deviations else _deepcopy(_TEMPLATE_DEVIATION)
        scan = _enforce_scan_consistency(_jitter(_neutralize(scan), rng))
        dev = _enforce_deviation_consistency(_jitter(_neutralize(dev), rng))

        # inject exactly one fault on contaminated runs (metrics first, so the
        # log section is assembled from the FINAL deviation numbers)
        details = {}
        if kind == "registration_degraded":
            details = _fault_registration_degraded(dev, rng)
        elif kind == "synthetic_outlier":
            details = _fault_synthetic_outlier(scan, rng)

        # pipeline log: real excerpt (neutralised) or synthetic section, plus
        # a registration/M3C2 section present in EVERY run
        if logs:
            base_log, start = _adapt_source_log(logs[int(rng.integers(len(logs)))], scan, rng)
            clock = _Clock(rng, start)
        else:
            clock = _Clock(rng)
            base_log = _build_scan_log(scan, clock, rng)
        log_text = base_log + _build_registration_log(dev, clock, rng)
        if kind == "corrupted_log":
            log_text, details = _fault_corrupted_log(log_text, rng)

        # write run artefacts (neutral names, identical set for every run)
        _write_json(run_dir / "scan_metrics.json", scan)
        _write_json(run_dir / "deviation_metrics.json", dev)
        (run_dir / "pipeline.log").write_text(log_text, encoding="utf-8")
        (run_dir / "PROMPT.md").write_text(prompt_text, encoding="utf-8")
        _write_json(run_dir / "manifest.json", {
            "run_id": run_id,
            "prompt": "PROMPT.md",
            "prompt_version": PROMPT_VERSION,
            "files_to_examine": list(DATA_FILES),
            "repetitions": rep_names,
            "verdict_filename": VERDICT_FILE,
        })
        for rep in rep_names:
            (run_dir / rep).mkdir()

        truth_runs[run_id] = {
            "faulted": kind != "clean",
            "fault_type": kind if kind != "clean" else "none",
            "details": details,
        }

    # -- sealed ground truth (the ONLY place where the truth lives) ---------- #
    _write_json(out / GROUND_TRUTH, {
        "meta": {
            "created": datetime.now().isoformat(timespec="seconds"),
            "protocol": "fixed prompts, independent fresh-context repetitions",
            "prompt_version": PROMPT_VERSION,
            "seed": args.seed,
            "n_clean": args.n_clean,
            "n_faulted": args.n_faulted,
            "reps": args.reps,
            "artefact_source": src_desc,
        },
        "runs": truth_runs,
    })

    # -- uniform timestamps: no temporal clue inside the evaluation set ------ #
    for p in sorted(out.rglob("*"), reverse=True):
        os.utime(p, (_UNIFORM_MTIME, _UNIFORM_MTIME))
    os.utime(out, (_UNIFORM_MTIME, _UNIFORM_MTIME))

    n_runs = len(order)
    print("[prepare] evaluation set ready: %d runs (%d clean + %d faulted, "
          "balanced fault mix), %d repetitions each." % (n_runs, args.n_clean,
                                                         args.n_faulted, args.reps))
    print("[prepare] runs:         %s" % runs_root)
    print("[prepare] ground truth: %s  (keep OUT of reviewer sessions)" % (out / GROUND_TRUTH))
    print("[prepare] next step: for each run and repetition, open ONE FRESH "
          "LLM session (empty context), give it the run folder, let it follow "
          "PROMPT.md and write runs/run_XX/rep_YY/%s. Then run 'collect'." % VERDICT_FILE)


# --------------------------------------------------------------------------- #
# collect                                                                      #
# --------------------------------------------------------------------------- #
def _load_verdict(path):
    """Read and normalise one verdict.json. Returns (verdict, error)."""
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return None, "unreadable (%s)" % e
    if not isinstance(v, dict):
        return None, "not a JSON object"
    det = v.get("fault_detected")
    if isinstance(det, str):
        det = {"true": True, "false": False}.get(det.strip().lower())
    if not isinstance(det, bool):
        return None, "invalid fault_detected"
    ftype = str(v.get("fault_type", "none")).strip().lower()
    if ftype not in VERDICT_TYPES:
        ftype = "__invalid__"
    if det is False:
        ftype = "none"   # schema rule: no fault -> type none
    return {"detected": det, "type": ftype,
            "model": str(v.get("model", "")),
            "prompt_version": str(v.get("prompt_version", "")),
            "date": str(v.get("date", ""))}, None


def _rate(num, den):
    return (num / den) if den else None


def _fmt(x, nd=3):
    return ("%%.%df" % nd) % x if x is not None else "n/a"


def cmd_collect(args):
    d = Path(args.dir)
    gt_path = d / GROUND_TRUTH
    if not gt_path.exists():
        sys.exit("Ground truth not found: %s (run 'prepare' first)" % gt_path)
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    truth = gt["runs"]
    meta = gt.get("meta", {})
    reps_expected = int(meta.get("reps", 0))

    pooled = Counter()          # TP / FP / FN / TN at repetition level
    run_level = Counter()       # same, majority vote per run
    type_correct, type_total = 0, 0
    confusion = Counter()       # (gt_type, verdict_type) on detected faulted verdicts
    per_type = {}               # gt fault_type -> aggregation dict
    rows = []
    consistencies = []
    missing, invalid = 0, 0
    models_seen, versions_seen = set(), set()
    errors = []

    for run_id in sorted(truth):
        info = truth[run_id]
        gt_faulted, gt_type = bool(info["faulted"]), info["fault_type"]
        run_dir = d / "runs" / run_id
        verdicts = []
        rep_dirs = sorted(p for p in run_dir.glob("rep_*") if p.is_dir()) if run_dir.is_dir() else []
        for rep in rep_dirs:
            vp = rep / VERDICT_FILE
            if not vp.exists():
                missing += 1
                continue
            v, err = _load_verdict(vp)
            if v is None:
                invalid += 1
                errors.append("%s/%s: %s" % (run_id, rep.name, err))
                continue
            verdicts.append(v)
            models_seen.add(v["model"])
            versions_seen.add(v["prompt_version"])

        agg = per_type.setdefault(gt_type, {"runs": 0, "verdicts": 0, "detected": 0,
                                            "type_correct": 0, "consistency": []})
        agg["runs"] += 1
        agg["verdicts"] += len(verdicts)

        det_votes = sum(1 for v in verdicts if v["detected"])
        for v in verdicts:
            if gt_faulted:
                pooled["TP" if v["detected"] else "FN"] += 1
                if v["detected"]:
                    agg["detected"] += 1
                    type_total += 1
                    confusion[(gt_type, v["type"])] += 1
                    if v["type"] == gt_type:
                        type_correct += 1
                        agg["type_correct"] += 1
            else:
                pooled["FP" if v["detected"] else "TN"] += 1
                if v["detected"]:
                    agg["detected"] += 1

        # inter-repetition consistency: agreement with the modal verdict
        consistency = None
        if verdicts:
            votes = Counter((v["detected"], v["type"]) for v in verdicts)
            consistency = max(votes.values()) / len(verdicts)
            consistencies.append(consistency)
            agg["consistency"].append(consistency)

        # majority vote per run (strict majority; ties -> "no fault")
        majority_detected = det_votes * 2 > len(verdicts) if verdicts else None
        majority_type = "none"
        if majority_detected:
            tvotes = Counter(v["type"] for v in verdicts if v["detected"])
            majority_type = sorted(tvotes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        if majority_detected is not None:
            if gt_faulted:
                run_level["TP" if majority_detected else "FN"] += 1
            else:
                run_level["FP" if majority_detected else "TN"] += 1

        n_v = len(verdicts)
        det_ok = sum(1 for v in verdicts if v["detected"] == gt_faulted)
        t_corr = _rate(sum(1 for v in verdicts if v["detected"] and v["type"] == gt_type),
                       det_votes) if (gt_faulted and det_votes) else None
        rows.append({
            "run_id": run_id,
            "faulted": gt_faulted,
            "fault_type": gt_type,
            "n_reps_expected": reps_expected or len(rep_dirs),
            "n_verdicts": n_v,
            "detected_votes": det_votes,
            "majority_detected": majority_detected,
            "majority_fault_type": majority_type if majority_detected is not None else "",
            "detection_correct_rate": round(det_ok / n_v, 3) if n_v else "",
            "type_correct_rate": round(t_corr, 3) if t_corr is not None else "",
            "consistency": round(consistency, 3) if consistency is not None else "",
        })

    # ------------------------- aggregate metrics ---------------------------- #
    recall = _rate(pooled["TP"], pooled["TP"] + pooled["FN"])
    precision = _rate(pooled["TP"], pooled["TP"] + pooled["FP"])
    accuracy = _rate(pooled["TP"] + pooled["TN"], sum(pooled.values()))
    fp_rate = _rate(pooled["FP"], pooled["FP"] + pooled["TN"])
    r_recall = _rate(run_level["TP"], run_level["TP"] + run_level["FN"])
    r_precision = _rate(run_level["TP"], run_level["TP"] + run_level["FP"])
    type_acc = _rate(type_correct, type_total)
    mean_cons = float(np.mean(consistencies)) if consistencies else None
    min_cons = float(np.min(consistencies)) if consistencies else None
    unanimous = sum(1 for c in consistencies if c == 1.0)

    # ------------------------------ metrics.csv ----------------------------- #
    csv_path = d / "metrics.csv"
    fields = ["run_id", "faulted", "fault_type", "n_reps_expected", "n_verdicts",
              "detected_votes", "majority_detected", "majority_fault_type",
              "detection_correct_rate", "type_correct_rate", "consistency"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ------------------------------ summary.md ------------------------------ #
    n_faulted_runs = sum(1 for r in truth.values() if r["faulted"])
    md = []
    md.append("# Fault-injection audit -- LLM pipeline reviewer")
    md.append("")
    md.append("Protocol: fixed prompt (version %s), independent fresh-context "
              "repetitions. Majority rule: strict majority of available "
              "verdicts; ties count as \"no fault\"."
              % meta.get("prompt_version", PROMPT_VERSION))
    md.append("")
    md.append("- Collected: %s" % datetime.now().isoformat(timespec="seconds"))
    md.append("- Evaluation set: %d runs (%d faulted / %d clean), %s repetitions planned"
              % (len(truth), n_faulted_runs, len(truth) - n_faulted_runs,
                 reps_expected or "?"))
    md.append("- Verdicts: %d collected, %d missing, %d invalid"
              % (sum(r["n_verdicts"] for r in rows), missing, invalid))
    md.append("- Models seen: %s" % (", ".join(sorted(m for m in models_seen if m)) or "n/a"))
    md.append("- Prompt versions seen: %s" % (", ".join(sorted(v for v in versions_seen if v)) or "n/a"))
    md.append("")
    md.append("## Detection (fault yes/no)")
    md.append("")
    md.append("| Level | TP | FP | FN | TN | Recall | Precision | Accuracy |")
    md.append("|---|---|---|---|---|---|---|---|")
    md.append("| Per repetition (pooled) | %d | %d | %d | %d | %s | %s | %s |"
              % (pooled["TP"], pooled["FP"], pooled["FN"], pooled["TN"],
                 _fmt(recall), _fmt(precision), _fmt(accuracy)))
    md.append("| Per run (majority vote) | %d | %d | %d | %d | %s | %s | - |"
              % (run_level["TP"], run_level["FP"], run_level["FN"], run_level["TN"],
                 _fmt(r_recall), _fmt(r_precision)))
    md.append("")
    md.append("False-positive rate on clean runs (per repetition): %s" % _fmt(fp_rate))
    md.append("")
    md.append("## Fault-type accuracy")
    md.append("")
    md.append("Among true-positive verdicts (fault correctly detected on a "
              "faulted run): %d/%d correct type = %s"
              % (type_correct, type_total, _fmt(type_acc)))
    if confusion:
        md.append("")
        md.append("| Ground truth | Reported | Verdicts |")
        md.append("|---|---|---|")
        for (g, r), c in sorted(confusion.items()):
            md.append("| %s | %s | %d |" % (g, r, c))
    md.append("")
    md.append("## Inter-repetition consistency")
    md.append("")
    md.append("Agreement with the modal verdict per run: mean %s, min %s; "
              "unanimous runs: %d/%d."
              % (_fmt(mean_cons), _fmt(min_cons), unanimous, len(consistencies)))
    md.append("")
    md.append("## By fault type")
    md.append("")
    md.append("| Fault type | Runs | Verdicts | Detected | Detection rate | Type accuracy | Mean consistency |")
    md.append("|---|---|---|---|---|---|---|")
    order = list(FAULT_KINDS) + ["none"]
    for t in order:
        if t not in per_type:
            continue
        a = per_type[t]
        det_rate = _rate(a["detected"], a["verdicts"])
        t_acc = _rate(a["type_correct"], a["detected"]) if t != "none" else None
        cons = float(np.mean(a["consistency"])) if a["consistency"] else None
        label = t if t != "none" else "none (clean)"
        md.append("| %s | %d | %d | %d | %s | %s | %s |"
                  % (label, a["runs"], a["verdicts"], a["detected"],
                     _fmt(det_rate), _fmt(t_acc) if t_acc is not None else "-",
                     _fmt(cons)))
    md.append("")
    md.append("For faulted rows the detection rate is the per-repetition recall; "
              "for the clean row it is the false-positive rate.")
    if errors:
        md.append("")
        md.append("## Invalid verdicts")
        md.append("")
        for e in errors:
            md.append("- %s" % e)
    md_path = d / "summary.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    # ------------------------------- console -------------------------------- #
    print("[collect] runs: %d (%d faulted / %d clean) | verdicts: %d (missing %d, invalid %d)"
          % (len(truth), n_faulted_runs, len(truth) - n_faulted_runs,
             sum(r["n_verdicts"] for r in rows), missing, invalid))
    print("[collect] detection (per repetition): recall=%s precision=%s accuracy=%s"
          % (_fmt(recall), _fmt(precision), _fmt(accuracy)))
    print("[collect] detection (per-run majority): recall=%s precision=%s"
          % (_fmt(r_recall), _fmt(r_precision)))
    print("[collect] fault-type accuracy (on true positives): %s (%d/%d)"
          % (_fmt(type_acc), type_correct, type_total))
    print("[collect] inter-repetition consistency: mean=%s min=%s unanimous=%d/%d"
          % (_fmt(mean_cons), _fmt(min_cons), unanimous, len(consistencies)))
    print("-> %s\n-> %s" % (csv_path, md_path))


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("prepare", help="build the sealed evaluation set "
                                        "(clean + contaminated runs)")
    pr.add_argument("--source", default="results",
                    help="real benchmark results directory (fallback to embedded "
                         "templates if missing)")
    pr.add_argument("--n-clean", type=int, default=5, help="number of clean runs")
    pr.add_argument("--n-faulted", type=int, default=5,
                    help="number of contaminated runs (balanced fault mix)")
    pr.add_argument("--seed", type=int, default=42, help="master seed (shuffling, "
                                                         "jitter, fault parameters)")
    pr.add_argument("--reps", type=int, default=10,
                    help="independent fresh-context repetitions per run")
    pr.add_argument("--out", default="results/fault_injection",
                    help="output directory of the evaluation set")

    co = sub.add_parser("collect", help="aggregate verdicts vs ground truth "
                                        "(recall, precision, consistency)")
    co.add_argument("--dir", default="results/fault_injection",
                    help="evaluation set directory (with ground_truth.json)")

    args = p.parse_args()
    if args.cmd == "prepare":
        if args.n_clean < 0 or args.n_faulted < 1 or args.reps < 1:
            sys.exit("prepare: need n-faulted >= 1, reps >= 1, n-clean >= 0")
        cmd_prepare(args)
    elif args.cmd == "collect":
        cmd_collect(args)


if __name__ == "__main__":
    main()

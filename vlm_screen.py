#!/usr/bin/env python3
"""
vlm-screen: VLM-based visual pre-screening harness (NO API calls)
=================================================================
Companion to benchmark.py / render_std.py: a harness for the visual quality
pre-screening of point-cloud scans through a Vision-Language Model, designed
so that NOT A SINGLE API CALL is made by this code. Everything deterministic
(packet preparation, aggregation, statistics) is code; the judgement itself
is produced by EXTERNAL fresh LLM sessions (e.g. Claude Code subagents with
an empty context) that read one evaluation packet and write one verdict JSON.

Protocol (methodological formula)
---------------------------------
"Fixed prompts, independent fresh-context repetitions, inter-run consistency
reported." Concretely:
  * the prompt is fixed and versioned (PROMPT_VERSION below), identical for
    every cloud and every repetition; it contains a CLOSED rubric of SLAM
    artifacts (drift, ghosting/double walls, wall duplication, holes, excess
    noise), each scored 0-3 for severity plus a 0-1 confidence;
  * the evaluation is BLIND: packets carry an anonymised cloud alias
    (cloud_A, cloud_B, ...); the real-label map lives OUTSIDE the packets in
    <out>/label_map.json and must never be shown to the assessor session;
  * each repetition is an INDEPENDENT session with an empty context (no
    memory of previous repetitions, no access to metrics or device names);
  * dispersion across repetitions (IQR, inter-run consistency) is reported
    as-is; no claims about sampling temperature are made.

How to run the evaluations (one packet = one fresh session)
-----------------------------------------------------------
For every packet directory results/vlm/packets/<alias>/rep_NN/ open ONE fresh
LLM session (e.g. a Claude Code subagent with empty context) whose only
instructions are:
  1. read PROMPT.md, manifest.json and RESPONSE_SCHEMA.json in the packet;
  2. look at the PNG renders listed in manifest.json (paths are relative to
     the packet directory);
  3. write the verdict as verdict.json in the SAME packet directory,
     conforming to RESPONSE_SCHEMA.json (model name and date included).
The session must not receive any other context (no device names, no metrics,
no other packets).

Usage
-----
  python vlm_screen.py prepare   --renders results/renders/BLK2GO --label BLK2GO --reps 10
  # ... run one fresh LLM session per packet (see above) ...
  python vlm_screen.py collect   --label BLK2GO
  python vlm_screen.py correlate --screenings results/vlm --metrics comparison.csv
  python vlm_screen.py kappa     --ratings human_ratings.csv --screenings results/vlm

Requires: Python 3.8+ and numpy only (standard library otherwise).
"""

import argparse
import csv
import json
import math
import re
import shutil
import sys
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Locked protocol constants (same for every cloud -> fair, comparable runs)    #
# --------------------------------------------------------------------------- #
PROMPT_VERSION = "vlm-rubric-1.0"

# Closed rubric: the assessor scores THESE artifacts and nothing else.
ARTIFACTS = ["drift", "ghosting", "wall_duplication", "holes", "excess_noise"]

ARTIFACT_DEFINITIONS = {
    "drift": (
        "Progressive trajectory error accumulated by the SLAM solution: "
        "elements that should be straight appear bent or curved (banana-shaped "
        "long walls), floors appear sloped, right angles drift away from 90 "
        "degrees. Most visible in the plan and section views."
    ),
    "ghosting": (
        "Double walls / double surfaces: the SAME surface appears twice with a "
        "SMALL parallel offset (typically a few centimetres), caused by loop-"
        "closure misclosure or re-visited areas that do not overlap exactly. "
        "Look for doubled thin lines in the plan and section views."
    ),
    "wall_duplication": (
        "An entire wall or structural element appears as two (or more) CLEARLY "
        "SEPARATED copies (gross registration failure). Same family as "
        "ghosting but with a large offset: the copies read as two distinct "
        "walls, not as one thick/doubled surface."
    ),
    "holes": (
        "Missing-data regions on surfaces that should have been captured "
        "(occlusion shadows, missed passes, dropped frames). Do not count "
        "genuine openings (windows, doors) as holes."
    ),
    "excess_noise": (
        "Fuzzy or thick surfaces, scattered outlier points, salt-and-pepper "
        "appearance. In the section view, walls read as thick fuzzy bands "
        "instead of thin lines."
    ),
}

SEVERITY_ANCHORS = [
    ("0", "absent: no visible sign of the artifact in any view"),
    ("1", "minor: visible but localised; unlikely to affect building-documentation use"),
    ("2", "moderate: clearly visible in one or more views; parts of the survey would need manual cleaning or re-scanning"),
    ("3", "severe: dominates one or more views; the geometry is unreliable for documentation purposes"),
]

USABILITY_ANCHORS = [
    ("0", "not usable for building documentation"),
    ("1", "only partial / indicative use (rough volumes at best)"),
    ("2", "usable after significant manual cleaning"),
    ("3", "usable as-is or with minor cleaning"),
]

# Standardised views produced by render_std.py (fixed order).
STANDARD_VIEWS = ["plan", "elev_x", "elev_y", "iso", "section_120"]

N_SEVERITY_LEVELS = 4          # severity categories 0..3
CONSISTENCY_BAND = 1.0         # inter-run consistency: |severity - median| <= 1


def safe_label(s):
    """Filesystem-safe label (same convention as benchmark.py)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s).strip()).strip("_") or "scan"


def norm_key(s):
    """Normalised key for matching labels across files (case/punct-insensitive)."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def write_text(path, text):
    Path(path).write_text(text, encoding="utf-8")


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Fixed, versioned prompt + response schema                                    #
# --------------------------------------------------------------------------- #
def example_verdict():
    return {
        "prompt_version": PROMPT_VERSION,
        "model": "<your-model-id>",
        "date": "<YYYY-MM-DD>",
        "artifacts": {a: {"severity": 0, "confidence": 0.0} for a in ARTIFACTS},
        "overall_usability_0_3": 0,
        "notes": "<short free-text notes, max ~50 words>",
    }


def response_schema():
    """JSON Schema (draft-07) for verdict.json."""
    artifact_props = {
        a: {
            "type": "object",
            "required": ["severity", "confidence"],
            "additionalProperties": False,
            "properties": {
                "severity": {"type": "integer", "minimum": 0, "maximum": 3,
                             "description": "0=absent, 1=minor, 2=moderate, 3=severe"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1,
                               "description": "assessor's confidence in this score"},
            },
        }
        for a in ARTIFACTS
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "VLM point-cloud screening verdict",
        "type": "object",
        "required": ["prompt_version", "model", "date", "artifacts",
                     "overall_usability_0_3", "notes"],
        "additionalProperties": False,
        "properties": {
            "prompt_version": {"type": "string", "const": PROMPT_VERSION},
            "model": {"type": "string", "description": "model name/id of the assessor session"},
            "date": {"type": "string", "description": "evaluation date, ISO format YYYY-MM-DD"},
            "artifacts": {
                "type": "object",
                "required": list(ARTIFACTS),
                "additionalProperties": False,
                "properties": artifact_props,
            },
            "overall_usability_0_3": {
                "type": "integer", "minimum": 0, "maximum": 3,
                "description": "0=not usable ... 3=usable as-is (building documentation)",
            },
            "notes": {"type": "string"},
        },
    }


def build_prompt():
    """The fixed, versioned evaluation prompt (identical for every packet)."""
    rubric = "\n".join(
        f"- **{a}**: {ARTIFACT_DEFINITIONS[a]}" for a in ARTIFACTS
    )
    severity = "\n".join(f"- **{v}** = {d}" for v, d in SEVERITY_ANCHORS)
    usability = "\n".join(f"- **{v}** = {d}" for v, d in USABILITY_ANCHORS)
    example = json.dumps(example_verdict(), indent=2, ensure_ascii=False)
    return f"""# Point-cloud quality screening (prompt {PROMPT_VERSION})

## Role
You are an independent point-cloud quality assessor. You evaluate rendered
views of a 3D point cloud of a building interior/exterior and rate the
presence of typical SLAM acquisition artifacts. You are part of a blind
screening protocol: you know nothing about how the cloud was acquired.

## Task
Examine ALL the PNG renders listed in `manifest.json` (paths are relative to
this packet folder). The renders are standardised views of ONE point cloud,
typically: a top-down plan, two orthogonal elevations (`elev_x`, `elev_y`),
an isometric view (`iso`) and a horizontal section at 1.20 m
(`section_120`). Point size and colour ramp are identical across all clouds
of the study, so what you see is comparable.

## Closed rubric (score these artifacts and NOTHING else)
{rubric}

## Severity scale (integer, one per artifact)
{severity}

## Confidence (number, one per artifact)
A value between 0 and 1 expressing how confident you are in the severity you
assigned (0 = pure guess, 1 = certain).

## Overall usability (integer)
{usability}

## Rules (mandatory)
1. Score EVERY artifact of the rubric, even when severity is 0. Do NOT add
   new artifact types.
2. Do NOT attempt to identify, name or infer the type, brand, model or
   technology of the scanning device, and do not let any such guess
   influence the scores. Judge the images only.
3. Base your assessment EXCLUSIVELY on the PNG files listed in
   `manifest.json`. Do not open other files or folders, do not use any
   external information or prior context.
4. Distinguish ghosting from wall_duplication: small parallel offset of the
   same surface = ghosting; clearly separated full copies = wall_duplication.
5. Do not count genuine openings (windows, doors) as holes.

## Output (strict)
Respond ONLY with a JSON object that conforms to `RESPONSE_SCHEMA.json`
(in this folder) and save it as `verdict.json` in this SAME packet folder.
No markdown fences, no commentary outside the JSON. Set `model` to your
model name/id and `date` to today's date (ISO, YYYY-MM-DD).

Template:

{example}
"""


# --------------------------------------------------------------------------- #
# Anonymisation: real label -> alias (map kept OUTSIDE the packets)            #
# --------------------------------------------------------------------------- #
def _alias_for_index(i):
    """0 -> cloud_A, 1 -> cloud_B, ... 25 -> cloud_Z, 26 -> cloud_AA, ..."""
    s, i = "", i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return f"cloud_{s}"


def load_label_map(out_dir):
    p = Path(out_dir) / "label_map.json"
    return read_json(p) if p.exists() else {}


def assign_alias(label, out_dir):
    """Get (or create) the anonymous alias for a real label; persist the map."""
    label_map = load_label_map(out_dir)
    if label in label_map:
        return label_map[label]
    alias = _alias_for_index(len(label_map))
    label_map[label] = alias
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    write_json(Path(out_dir) / "label_map.json", label_map)
    return alias


def resolve_alias(label, out_dir):
    """Accept either the real label or the alias itself; return (real, alias)."""
    label_map = load_label_map(out_dir)
    if label in label_map:
        return label, label_map[label]
    rev = {v: k for k, v in label_map.items()}
    if label in rev:
        return rev[label], label
    sys.exit(f"Label '{label}' not found in {Path(out_dir) / 'label_map.json'}. "
             f"Run 'prepare' first (known labels: {sorted(label_map)}).")


# --------------------------------------------------------------------------- #
# prepare: build the anonymised evaluation packets                             #
# --------------------------------------------------------------------------- #
def discover_renders(renders_dir):
    """Find the standardised view PNGs in a render directory.
    Prefers exact names (plan.png, elev_x.png, ...), then filenames containing
    the view name; falls back to all PNGs (renamed view_NN) if none match."""
    d = Path(renders_dir)
    if not d.is_dir():
        sys.exit(f"Render directory not found: {d}")
    found = []
    for view in STANDARD_VIEWS:
        exact = d / f"{view}.png"
        if exact.exists():
            found.append((view, exact))
            continue
        matches = sorted(p for p in d.glob("*.png") if view in p.stem.lower())
        if matches:
            found.append((view, matches[0]))
    if not found:
        pngs = sorted(d.glob("*.png"))
        if not pngs:
            sys.exit(f"No PNG renders found in {d}")
        print(f"WARNING: no standard views ({', '.join(STANDARD_VIEWS)}) found in {d}; "
              f"using all {len(pngs)} PNGs with neutral names.")
        found = [(f"view_{i + 1:02d}", p) for i, p in enumerate(pngs)]
    else:
        missing = [v for v in STANDARD_VIEWS if v not in dict(found)]
        if missing:
            print(f"WARNING: missing standard views in {d}: {', '.join(missing)}")
    return found


def prepare_packets(renders_dir, label, reps, out_dir):
    """Create results/vlm/packets/<alias>/rep_01..rep_NN evaluation packets.
    The packets are BLIND: directory names, manifest and copied render names
    only carry the anonymous alias; the real-label map stays in
    <out>/label_map.json, outside the packets tree."""
    out_dir = Path(out_dir)
    alias = assign_alias(label, out_dir)
    views = discover_renders(renders_dir)

    # Copy the renders ONCE per cloud, under anonymised names, so every packet
    # is self-contained within packets/<alias>/ and no path leaks the device.
    packet_root = out_dir / "packets" / alias
    render_dst = packet_root / "renders"
    render_dst.mkdir(parents=True, exist_ok=True)
    render_entries = []
    for view, src in views:
        dst = render_dst / f"{view}.png"
        shutil.copy2(src, dst)
        render_entries.append({"view": view, "file": f"../renders/{view}.png"})

    prompt_md = build_prompt()
    schema = response_schema()
    rep_dirs = []
    for r in range(1, reps + 1):
        rep_dir = packet_root / f"rep_{r:02d}"
        rep_dir.mkdir(parents=True, exist_ok=True)
        write_text(rep_dir / "PROMPT.md", prompt_md)
        write_json(rep_dir / "RESPONSE_SCHEMA.json", schema)
        write_json(rep_dir / "manifest.json", {
            "packet": f"{alias}/rep_{r:02d}",
            "label": alias,                      # anonymised on purpose
            "repetition": r,
            "n_repetitions": reps,
            "prompt_version": PROMPT_VERSION,
            "renders": render_entries,
            "verdict_file": "verdict.json",
        })
        if (rep_dir / "verdict.json").exists():
            print(f"   note: existing verdict kept in {rep_dir}")
        rep_dirs.append(rep_dir)

    print(f"Prepared {len(rep_dirs)} packets for '{label}' as blind alias '{alias}':")
    for rd in rep_dirs:
        print(f"  -> {rd}")
    print(f"Label map (KEEP OUT of assessor sessions): {out_dir / 'label_map.json'}")
    print("\nNext step, one FRESH LLM session per packet (empty context), instructed to:")
    print("  read PROMPT.md + manifest.json + the listed PNGs, then write verdict.json")
    print("  in the same packet folder. Then run:  vlm_screen.py collect --label", label)


# --------------------------------------------------------------------------- #
# collect: aggregate the verdicts of one cloud                                 #
# --------------------------------------------------------------------------- #
def _agg_stats(values, band=CONSISTENCY_BAND):
    """Median, IQR and inter-run consistency (% of runs within +-band of the
    median) for a list of per-repetition scores."""
    a = np.asarray(values, dtype=float)
    med = float(np.median(a))
    q1, q3 = (float(x) for x in np.percentile(a, [25, 75]))
    consistency = 100.0 * float(np.mean(np.abs(a - med) <= band))
    return {
        "median": round(med, 2),
        "iqr": [round(q1, 2), round(q3, 2)],
        "iqr_width": round(q3 - q1, 2),
        "min": round(float(a.min()), 2),
        "max": round(float(a.max()), 2),
        "consistency_pct": round(consistency, 1),
        "n": int(a.size),
    }


def collect_verdicts(label, out_dir):
    out_dir = Path(out_dir)
    real_label, alias = resolve_alias(label, out_dir)
    packet_root = out_dir / "packets" / alias
    rep_dirs = sorted(packet_root.glob("rep_*"))
    if not rep_dirs:
        sys.exit(f"No packets found in {packet_root}. Run 'prepare' first.")

    verdicts, pending = [], []
    for rd in rep_dirs:
        vf = rd / "verdict.json"
        if not vf.exists():
            pending.append(rd.name)
            continue
        try:
            v = read_json(vf)
        except json.JSONDecodeError as e:
            print(f"WARNING: invalid JSON in {vf}: {e} (skipped)")
            continue
        v["_rep"] = rd.name
        verdicts.append(v)
    if not verdicts:
        sys.exit(f"No verdict.json found under {packet_root} "
                 f"({len(pending)} packets still pending). Run the evaluation sessions first.")

    prompt_versions = sorted({v.get("prompt_version", "?") for v in verdicts})
    if prompt_versions != [PROMPT_VERSION]:
        print(f"WARNING: prompt versions in verdicts {prompt_versions} != current {PROMPT_VERSION}")

    per_artifact = {}
    for art in ARTIFACTS:
        sev, conf = [], []
        for v in verdicts:
            entry = v.get("artifacts", {}).get(art)
            if entry is None or "severity" not in entry:
                print(f"WARNING: '{art}' missing in {v['_rep']} (skipped for this artifact)")
                continue
            sev.append(float(entry["severity"]))
            conf.append(float(entry.get("confidence", float("nan"))))
        if not sev:
            per_artifact[art] = None
            continue
        stats = _agg_stats(sev)
        conf_arr = np.asarray(conf, dtype=float)
        conf_fin = conf_arr[np.isfinite(conf_arr)]
        stats["confidence_median"] = round(float(np.median(conf_fin)), 2) if conf_fin.size else None
        per_artifact[art] = stats

    overall = [float(v["overall_usability_0_3"]) for v in verdicts
               if "overall_usability_0_3" in v]
    overall_stats = _agg_stats(overall) if overall else None

    result = {
        "label": real_label,
        "alias": alias,
        "prompt_version": PROMPT_VERSION,
        "prompt_versions_seen": prompt_versions,
        "n_packets": len(rep_dirs),
        "n_verdicts": len(verdicts),
        "pending_packets": pending,
        "models": sorted({v.get("model", "?") for v in verdicts}),
        "dates": sorted({v.get("date", "?") for v in verdicts}),
        "consistency_definition": f"% of repetitions with |severity - median| <= {CONSISTENCY_BAND:g}",
        "artifacts": per_artifact,
        "overall_usability": overall_stats,
    }
    json_path = out_dir / f"{real_label}_screening.json"
    write_json(json_path, result)

    # Markdown summary
    md = [f"# VLM screening: {real_label}", "",
          f"Blind alias: `{alias}` · prompt `{PROMPT_VERSION}` · "
          f"{len(verdicts)}/{len(rep_dirs)} verdicts collected · "
          f"models: {', '.join(result['models'])} · dates: {', '.join(result['dates'])}",
          "",
          "Protocol: fixed prompts, independent fresh-context repetitions, "
          "inter-run consistency reported.",
          "",
          "| Artifact | Median severity (0-3) | IQR | Consistency (±1 of median) | Median confidence |",
          "|---|---|---|---|---|"]
    for art in ARTIFACTS:
        s = per_artifact[art]
        if s is None:
            md.append(f"| {art} | n/a | n/a | n/a | n/a |")
        else:
            md.append(f"| {art} | {s['median']:g} | [{s['iqr'][0]:g}, {s['iqr'][1]:g}] "
                      f"| {s['consistency_pct']:g}% | "
                      f"{s['confidence_median'] if s['confidence_median'] is not None else 'n/a'} |")
    if overall_stats:
        md += ["", f"**Overall usability (0-3)**: median {overall_stats['median']:g}, "
                   f"IQR [{overall_stats['iqr'][0]:g}, {overall_stats['iqr'][1]:g}], "
                   f"consistency {overall_stats['consistency_pct']:g}% "
                   f"(n = {overall_stats['n']})"]
    if pending:
        md += ["", f"Pending packets (no verdict yet): {', '.join(pending)}"]
    md_path = out_dir / f"{real_label}_screening.md"
    write_text(md_path, "\n".join(md) + "\n")

    print("\n".join(md))
    print(f"\n-> {json_path}\n-> {md_path}")


# --------------------------------------------------------------------------- #
# Rank statistics (numpy only, no scipy)                                       #
# --------------------------------------------------------------------------- #
def rankdata(x):
    """Average ranks (1-based); ties receive the mean of their rank positions."""
    x = np.asarray(x, dtype=float)
    order = np.argsort(x, kind="stable")
    ranks = np.empty(x.size, dtype=float)
    ranks[order] = np.arange(1, x.size + 1, dtype=float)
    for v in np.unique(x):
        m = x == v
        if np.count_nonzero(m) > 1:
            ranks[m] = ranks[m].mean()
    return ranks


def spearman_rho(a, b):
    """Spearman rank correlation = Pearson correlation of the average ranks
    (tie-safe general form, valid also when ties are present)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    if a.size < 2:
        return float("nan"), int(a.size)
    ra, rb = rankdata(a), rankdata(b)
    ra, rb = ra - ra.mean(), rb - rb.mean()
    denom = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    if denom == 0:                       # a constant vector has no rank order
        return float("nan"), int(a.size)
    return float((ra * rb).sum() / denom), int(a.size)


# --------------------------------------------------------------------------- #
# correlate: VLM severities vs quantitative metrics (Spearman)                 #
# --------------------------------------------------------------------------- #
METRIC_PATTERNS = [("sigma_mm", "sigma"), ("spacing_mm", "spacing"), ("p95_mm", "p95")]
VLM_VARS = ARTIFACTS + ["overall_usability"]


def load_screenings(screenings_dir):
    """Read every <label>_screening.json -> {label: {vlm_var: median}}."""
    out = {}
    for f in sorted(Path(screenings_dir).glob("*_screening.json")):
        data = read_json(f)
        row = {}
        for art in ARTIFACTS:
            s = (data.get("artifacts") or {}).get(art)
            row[art] = s["median"] if s else float("nan")
        ov = data.get("overall_usability")
        row["overall_usability"] = ov["median"] if ov else float("nan")
        out[data["label"]] = row
    return out


def load_metrics_csv(metrics_csv):
    """Parse comparison.csv (from benchmark.py report) tolerating header
    variants: picks the label column and any column whose header contains
    'sigma' / 'spacing' / 'p95'. Returns {norm_label: {metric: value}}."""
    with open(metrics_csv, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        sys.exit(f"Metrics CSV {metrics_csv} has no data rows.")
    header = [h.strip() for h in rows[0]]
    lower = [h.lower() for h in header]
    label_idx = next((i for i, h in enumerate(lower) if "scanner" in h or "label" in h), 0)
    col_map = {}
    for canon, pat in METRIC_PATTERNS:
        idx = next((i for i, h in enumerate(lower) if pat in h), None)
        if idx is not None:
            col_map[canon] = idx
    if not col_map:
        sys.exit(f"No metric columns (sigma/spacing/p95) found in {metrics_csv} "
                 f"(header: {header})")
    metrics = {}
    for row in rows[1:]:
        if not row or label_idx >= len(row) or not row[label_idx].strip():
            continue
        entry = {}
        for canon, idx in col_map.items():
            try:
                entry[canon] = float(row[idx])
            except (ValueError, IndexError):
                entry[canon] = float("nan")
        metrics[norm_key(row[label_idx])] = {"label": row[label_idx].strip(), **entry}
    return metrics, list(col_map)


def correlate(screenings_dir, metrics_csv, out_md):
    vlm = load_screenings(screenings_dir)
    if len(vlm) < 2:
        sys.exit(f"Need screenings for at least 2 devices in {screenings_dir} "
                 f"(found {len(vlm)}). Run 'collect' for each cloud first.")
    metrics, metric_names = load_metrics_csv(metrics_csv)

    matched = [(lbl, metrics[norm_key(lbl)]) for lbl in sorted(vlm)
               if norm_key(lbl) in metrics]
    unmatched = [lbl for lbl in sorted(vlm) if norm_key(lbl) not in metrics]
    if len(matched) < 2:
        sys.exit(f"Fewer than 2 devices match between screenings ({sorted(vlm)}) "
                 f"and metrics CSV ({[m['label'] for m in metrics.values()]}).")

    n_dev = len(matched)
    md = ["# VLM screening vs quantitative metrics: Spearman rank correlation", "",
          f"Devices matched: {n_dev} ({', '.join(lbl for lbl, _ in matched)})"
          + (f" · unmatched screenings: {', '.join(unmatched)}" if unmatched else ""),
          f"VLM side: median severity per artifact and median overall usability "
          f"across the fresh-context repetitions (prompt `{PROMPT_VERSION}`).",
          "",
          "| VLM variable | " + " | ".join(metric_names) + " |",
          "|---|" + "|".join(["---"] * len(metric_names)) + "|"]
    for var in VLM_VARS:
        cells = []
        for met in metric_names:
            a = [vlm[lbl][var] for lbl, _ in matched]
            b = [row[met] for _, row in matched]
            rho, n = spearman_rho(a, b)
            cells.append("n/a" if math.isnan(rho) else f"{rho:+.2f} (n={n})")
        md.append(f"| {var} | " + " | ".join(cells) + " |")

    md += ["",
           "## Method",
           "Spearman's rho is computed in numpy as the Pearson correlation of the",
           "average ranks (ties get the mean of their rank positions); this tie-safe",
           "general form is used instead of the 6*sum(d^2) shortcut. No scipy.",
           "",
           "## Limitations (declared)",
           f"- Only n = {n_dev} devices: rho is reported as a descriptive, exploratory",
           "  statistic. No p-values or significance claims are made; with n = 3 the",
           "  possible rho values are only 0, ±0.5 and ±1, so any single coefficient",
           "  is uninformative in isolation.",
           "- Median severities are ordinal (0-3) with heavy ties; ranks are averaged,",
           "  and a constant column yields 'n/a' (no rank order to correlate).",
           "- Correlation here indicates monotonic association only, not calibration",
           "  of the VLM scores against the metric scale."]
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    write_text(out_md, "\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\n-> {out_md}")


# --------------------------------------------------------------------------- #
# kappa: inter-rater agreement (numpy implementations)                         #
# --------------------------------------------------------------------------- #
def cohen_kappa_weighted(a, b, k=N_SEVERITY_LEVELS):
    """Cohen's weighted kappa with LINEAR weights for two raters.
    Disagreement weights v_ij = |i-j| / (k-1);
    kappa_w = 1 - sum(v*O) / sum(v*E), O = observed proportion matrix,
    E = outer product of the raters' marginals.
    Returns (kappa, n_items, exact_agreement_pct)."""
    a = np.asarray(a, dtype=int)
    b = np.asarray(b, dtype=int)
    n = a.size
    if n == 0:
        return float("nan"), 0, float("nan")
    O = np.zeros((k, k), dtype=float)
    for i, j in zip(a, b):
        O[i, j] += 1.0
    O /= n
    E = np.outer(O.sum(axis=1), O.sum(axis=0))
    idx = np.arange(k, dtype=float)
    V = np.abs(idx[:, None] - idx[None, :]) / (k - 1)
    d_obs, d_exp = float((V * O).sum()), float((V * E).sum())
    kappa = 1.0 - d_obs / d_exp if d_exp > 0 else float("nan")
    return kappa, n, 100.0 * float(np.mean(a == b))


def fleiss_kappa(counts):
    """Fleiss' kappa for m >= 3 raters. `counts` is the items x categories
    matrix of rating counts, with a CONSTANT number of raters per item.
    P_i = (sum_j n_ij^2 - m) / (m (m-1));  P_bar = mean_i P_i;
    P_e = sum_j p_j^2 with p_j = overall proportion of category j;
    kappa = (P_bar - P_e) / (1 - P_e)."""
    counts = np.asarray(counts, dtype=float)
    n_items = counts.shape[0]
    if n_items == 0:
        return float("nan")
    m = counts.sum(axis=1)
    if not np.all(m == m[0]) or m[0] < 2:
        return float("nan")
    m = float(m[0])
    P_i = (np.sum(counts ** 2, axis=1) - m) / (m * (m - 1))
    P_bar = float(P_i.mean())
    p_j = counts.sum(axis=0) / (n_items * m)
    P_e = float(np.sum(p_j ** 2))
    return (P_bar - P_e) / (1 - P_e) if (1 - P_e) > 0 else float("nan")


def _round_half_up(x):
    return int(math.floor(float(x) + 0.5))


def load_ratings_csv(ratings_csv):
    """Read the human-ratings CSV (columns: assessor,label,artifact,severity).
    Returns {assessor: {(norm_label, artifact): severity}} plus a display map
    for labels."""
    ratings, label_names = {}, {}
    with open(ratings_csv, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        for need in ("assessor", "label", "artifact", "severity"):
            if need not in cols:
                sys.exit(f"Column '{need}' missing in {ratings_csv} "
                         f"(found: {reader.fieldnames})")
        for row in reader:
            assessor = row[cols["assessor"]].strip()
            label = row[cols["label"]].strip()
            artifact = row[cols["artifact"]].strip()
            if not assessor or not label:
                continue
            if artifact not in ARTIFACTS:
                print(f"WARNING: unknown artifact '{artifact}' in {ratings_csv} (row skipped)")
                continue
            try:
                sev = int(row[cols["severity"]])
            except (TypeError, ValueError):
                print(f"WARNING: non-integer severity '{row[cols['severity']]}' (row skipped)")
                continue
            if not 0 <= sev <= 3:
                print(f"WARNING: severity {sev} outside 0-3 (row skipped)")
                continue
            ratings.setdefault(assessor, {})[(norm_key(label), artifact)] = sev
            label_names[norm_key(label)] = label
    return ratings, label_names


def kappa_report(ratings_csv, screenings_dir, out_md):
    ratings, label_names = load_ratings_csv(ratings_csv)
    assessors = sorted(ratings)
    if len(assessors) < 2:
        sys.exit(f"Need at least 2 assessors in {ratings_csv} (found {assessors}).")

    md = ["# Inter-rater agreement: VLM screening", "",
          f"Ratings file: `{ratings_csv}` · assessors: {', '.join(assessors)} · "
          f"items = (cloud label, artifact), severities 0-3.", ""]

    # ---- human vs human ---------------------------------------------------
    md.append("## Human vs human")
    if len(assessors) == 2:
        a1, a2 = assessors
        shared = sorted(set(ratings[a1]) & set(ratings[a2]))
        va = [ratings[a1][it] for it in shared]
        vb = [ratings[a2][it] for it in shared]
        kw, n, agree = cohen_kappa_weighted(va, vb)
        md += [f"2 assessors -> **Cohen's weighted kappa (linear weights)**: "
               f"kappa_w = {'n/a (no expected disagreement)' if math.isnan(kw) else f'{kw:.3f}'} "
               f"over n = {n} shared items (exact agreement {agree:.1f}%)."]
    else:
        complete = sorted(set.intersection(*(set(ratings[a]) for a in assessors)))
        counts = np.zeros((len(complete), N_SEVERITY_LEVELS))
        for i, it in enumerate(complete):
            for a in assessors:
                counts[i, ratings[a][it]] += 1
        kf = fleiss_kappa(counts)
        md += [f"{len(assessors)} assessors -> **Fleiss' kappa** (complete cases only): "
               f"kappa = {'n/a' if math.isnan(kf) else f'{kf:.3f}'} over "
               f"n = {len(complete)} items rated by all assessors."]

    # ---- VLM vs human median ----------------------------------------------
    md += ["", "## VLM vs human median"]
    vlm = load_screenings(screenings_dir)
    vlm_items = {}
    for lbl, row in vlm.items():
        for art in ARTIFACTS:
            if not math.isnan(row[art]):
                vlm_items[(norm_key(lbl), art)] = _round_half_up(row[art])
    human_median = {}
    all_items = set().union(*(set(ratings[a]) for a in assessors))
    for it in sorted(all_items):
        vals = [ratings[a][it] for a in assessors if it in ratings[a]]
        if vals:
            human_median[it] = _round_half_up(float(np.median(vals)))
    shared = sorted(set(vlm_items) & set(human_median))
    if shared:
        kw, n, agree = cohen_kappa_weighted([vlm_items[it] for it in shared],
                                            [human_median[it] for it in shared])
        md += [f"VLM rating = per-item median severity across the fresh-context "
               f"repetitions, rounded half-up; human rating = per-item median across "
               f"assessors, rounded half-up. **Cohen's weighted kappa (linear)**: "
               f"kappa_w = {'n/a (no expected disagreement)' if math.isnan(kw) else f'{kw:.3f}'} "
               f"over n = {n} shared items (exact agreement {agree:.1f}%)."]
    else:
        md += ["No overlapping items between the screenings in "
               f"`{screenings_dir}` and the human ratings: nothing to compare."]

    # ---- methods + caveats -------------------------------------------------
    md += ["",
           "## Method",
           "- Cohen's weighted kappa (2 raters): kappa_w = 1 - sum(v*O)/sum(v*E),",
           "  with linear disagreement weights v_ij = |i-j|/(k-1) on the k=4 severity",
           "  categories, O the observed joint-proportion matrix and E the outer",
           "  product of the two raters' marginal distributions.",
           "- Fleiss' kappa (3+ raters): kappa = (P_bar - P_e)/(1 - P_e), with",
           "  P_i = (sum_j n_ij^2 - m)/(m(m-1)) per item, P_bar the mean over items",
           "  and P_e = sum_j p_j^2 from the pooled category proportions.",
           "- Both implemented in numpy; no scipy / sklearn.",
           "",
           "Interpretation scale (Landis & Koch, indicative only): <0 poor, 0-0.20",
           "slight, 0.21-0.40 fair, 0.41-0.60 moderate, 0.61-0.80 substantial,",
           "0.81-1.00 almost perfect.",
           "",
           "## Limitations (declared)",
           "- Item counts are small (devices x artifacts); kappa estimates are",
           "  unstable and reported descriptively, without confidence intervals.",
           "- Rounding medians (half-up) to integer categories discards part of the",
           "  dispersion information; see the per-cloud screening JSON for IQRs.",
           "- kappa is 'n/a' when the expected disagreement is zero (e.g. both raters",
           "  constant on the same category): agreement is then trivially perfect",
           "  but kappa is undefined."]
    out_md = Path(out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    write_text(out_md, "\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\n-> {out_md}")


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("prepare", help="build blind evaluation packets (prompt + manifest + schema)")
    pr.add_argument("--renders", required=True, help="directory with the standardised PNG renders of ONE cloud")
    pr.add_argument("--label", required=True, help="real cloud label (anonymised in the packets)")
    pr.add_argument("--reps", type=int, default=10, help="independent fresh-context repetitions (default 10)")
    pr.add_argument("--out", default="results/vlm", help="output root (default results/vlm)")

    co = sub.add_parser("collect", help="aggregate the verdict.json of one cloud (median, IQR, consistency)")
    co.add_argument("--label", required=True, help="real label (or its alias)")
    co.add_argument("--out", default="results/vlm", help="output root used at prepare time")

    cr = sub.add_parser("correlate", help="Spearman correlation between VLM severities and quantitative metrics")
    cr.add_argument("--screenings", default="results/vlm", help="directory with *_screening.json")
    cr.add_argument("--metrics", required=True, help="comparison.csv from benchmark.py report")
    cr.add_argument("--out", default="results/vlm/correlation.md", help="output Markdown file")

    ka = sub.add_parser("kappa", help="inter-rater agreement: Cohen (2 assessors) / Fleiss (3+), plus VLM vs human median")
    ka.add_argument("--ratings", required=True, help="CSV with columns: assessor,label,artifact,severity")
    ka.add_argument("--screenings", default="results/vlm", help="directory with *_screening.json")
    ka.add_argument("--out", default="results/vlm/kappa.md", help="output Markdown file")

    args = p.parse_args()
    if getattr(args, "label", None):
        args.label = safe_label(args.label)

    if args.cmd == "prepare":
        if args.reps < 1:
            sys.exit("--reps must be >= 1")
        prepare_packets(args.renders, args.label, args.reps, args.out)
    elif args.cmd == "collect":
        collect_verdicts(args.label, args.out)
    elif args.cmd == "correlate":
        correlate(args.screenings, args.metrics, args.out)
    elif args.cmd == "kappa":
        kappa_report(args.ratings, args.screenings, args.out)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
handheld-lidar-benchmark
========================
A reproducible, instrument-agnostic protocol to benchmark handheld LiDAR /
SLAM scanners for building documentation, using CloudCompare as the geometry
engine (driven head-less from the command line).

Metrics (computed identically for every scanner -> fair comparison):
  * Point density            -> pts/m^2 and equivalent point spacing (mm)
  * Surface noise (sigma)    -> RMS of point-to-plane residuals on a flat patch
                                (dominant plane isolated with RANSAC -> operator
                                independent, robust to imperfect segmentation)

Fitness-for-purpose classes are anchored on USIBD Level of Accuracy (LOA) and
on drawing-scale graphical tolerance (0.2 mm x scale). See README.md.

Output: per-scanner JSON, plus an aggregated comparison table in CSV and Markdown.

Input formats: anything CloudCompare opens natively (LAS/LAZ, E57, PLY, BIN, ...).
For E57 the acquisition duration cannot be read automatically (no LAS GPS-time):
enter it manually with --duration-min (CLI) or "acq_min" (batch manifest).

Usage
-----
  python benchmark.py scan  --cloud BLK2GO.las           --label BLK2GO
  python benchmark.py plane --wall  BLK2GO_wall.bin      --label BLK2GO
  python benchmark.py report
  # or run everything from a manifest:
  python benchmark.py batch --manifest scanners.json

CloudCompare is auto-detected; override with --cc-exe or the CLOUDCOMPARE env var.
Requires: Python 3.8+, numpy, and a CloudCompare installation (>= 2.11).
"""

import argparse
import csv
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Configuration & defaults (locked equity parameters — same for every scanner) #
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "density_radius_m": 0.05,    # surface-density neighbourhood radius
    "roughness_kernel_m": 0.03,  # roughness kernel (global noise indicator)
    "export_step_m": 0.10,       # spatial subsampling for stats export ONLY
    "ransac_inlier_m": 0.015,    # RANSAC inlier threshold for plane isolation
    "ransac_iters": 2000,
    "acq_area_m2": 0,            # surveyed surface area (m2); >0 enables time/m2
    "duration_min_override": None,  # manual acquisition time when GPS-time is absent
    # M3C2 direct comparison (only meaningful on a SHARED scene); values match the paper
    "m3c2_normal_scale": 0.30,   # normal estimation diameter (m)
    "m3c2_proj_scale": 0.20,     # projection (cylinder) diameter (m)
    "m3c2_max_depth": 5.0,       # max search depth along the normal (m)
    "m3c2_subsample": 0.05,      # core-point subsampling radius (m)
    "m3c2_outlier_sigma": 3.0,   # exclude |residual - mean| > k*sigma from the stats
}

# Fitness-for-purpose thresholds (mm). See README for the USIBD-LOA mapping.
SPACING_CLASSES = [(5, "High (LOA30)"), (10, "Medium (LOA20)"), (25, "Low (LOA10)")]
SIGMA_CLASSES = [(5, "High (LOA30)"), (10, "Medium (LOA20)"), (30, "Low (LOA10)")]
# geometric deviation vs the reference cloud, classified on the 95th-percentile |distance|
DEVIATION_CLASSES = [(5, "High (LOA30)"), (15, "Medium (LOA20)"), (50, "Low (LOA10)")]


def classify(value_mm, table):
    for thr, name in table:
        if value_mm <= thr:
            return name
    return "Insufficient"


# Class names ranked worst-first, to derive the overall class of a device.
CLASS_RANK = ["Insufficient", "Low (LOA10)", "Medium (LOA20)", "High (LOA30)"]


def overall_class(*classes):
    """Overall fitness class of a device = the minimum (worst) among the classes
    available for it. Non-class markers ('' or 'REGISTRATION FAILED') are ignored."""
    ranked = [c for c in classes if c in CLASS_RANK]
    return min(ranked, key=CLASS_RANK.index) if ranked else ""


def safe_label(s):
    """Filesystem- and CloudCompare-safe label. CloudCompare's -SAVE_CLOUDS FILE
    splits on spaces, so spaces in a label (e.g. 'Geox 02') break the save; we
    replace any non [A-Za-z0-9._-] run with a single underscore."""
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s).strip()).strip("_") or "scan"


# --------------------------------------------------------------------------- #
# CloudCompare discovery                                                       #
# --------------------------------------------------------------------------- #
def find_cloudcompare(explicit=None):
    """Locate the CloudCompare executable across OSes."""
    if explicit:
        return explicit
    if os.environ.get("CLOUDCOMPARE"):
        return os.environ["CLOUDCOMPARE"]
    for name in ("CloudCompare", "cloudcompare.CloudCompare"):
        found = shutil.which(name)
        if found:
            return found
    candidates = {
        "Windows": [r"C:\Program Files\CloudCompare\CloudCompare.exe"],
        "Darwin": ["/Applications/CloudCompare.app/Contents/MacOS/CloudCompare"],
        "Linux": ["/usr/bin/CloudCompare", "/usr/local/bin/CloudCompare"],
    }.get(platform.system(), [])
    for c in candidates:
        if Path(c).exists():
            return c
    sys.exit("CloudCompare not found. Use --cc-exe PATH or set $CLOUDCOMPARE.")


def run_cc(cc_exe, args, log_path):
    cmd = [cc_exe, "-SILENT", "-LOG_FILE", str(log_path)] + args
    subprocess.run(cmd, check=True)
    return log_path.read_text(errors="ignore") if log_path.exists() else ""


def parse_npoints(log_text):
    m = re.search(r"cloud with (\d+) points", log_text)
    return int(m.group(1)) if m else None


def gps_duration_min(cloud_path):
    """Acquisition duration (minutes) from the LAS GPS-time field, or None.
    Works for .las/.laz that carry gps_time (e.g. FJD Trion, BLK2GO); returns None
    for formats/devices without it (e.g. .e57 exports, iPhone/Polycam PLY) -> enter
    the time manually (--duration-min on the CLI, "acq_min" in the batch manifest)."""
    p = str(cloud_path).lower()
    if not (p.endswith(".las") or p.endswith(".laz")):
        return None
    try:
        import laspy
        las = laspy.read(str(cloud_path))
        if "gps_time" not in [d.name for d in las.point_format.dimensions]:
            return None
        t = np.asarray(las.gps_time)
        dur = float(t.max() - t.min())
        return round(dur / 60.0, 2) if dur > 0 else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Geometry helpers                                                            #
# --------------------------------------------------------------------------- #
def fit_plane_pca(xyz):
    """PCA plane fit. Returns (normal, signed point-to-plane distances)."""
    c = xyz - xyz.mean(axis=0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    normal = vt[2]
    return normal, c @ normal


def ransac_plane(xyz, thr, iters, seed=0, max_abs_nz=None):
    """Isolate the dominant plane (RANSAC). Returns the inlier points.
    If max_abs_nz is set, only near-vertical planes (|normal_z| <= max_abs_nz,
    i.e. walls) are considered; returns an empty array if none qualifies."""
    rng = np.random.default_rng(seed)
    n = xyz.shape[0]
    score = xyz[rng.choice(n, size=min(n, 60000), replace=False)]
    best_cnt, best_normal, best_p = -1, None, None
    for _ in range(iters):
        s = xyz[rng.choice(n, 3, replace=False)]
        nv = np.cross(s[1] - s[0], s[2] - s[0])
        norm = np.linalg.norm(nv)
        if norm < 1e-9:
            continue
        nv = nv / norm
        if max_abs_nz is not None and abs(nv[2]) > max_abs_nz:
            continue  # skip floors / ceilings, keep walls only
        cnt = int(np.count_nonzero(np.abs((score - s[0]) @ nv) < thr))
        if cnt > best_cnt:
            best_cnt, best_normal, best_p = cnt, nv, s[0]
    if best_normal is None:
        return np.empty((0, 3))
    return xyz[np.abs((xyz - best_p) @ best_normal) < thr]


def auto_plane_sigma(xyz, thr=0.015, patch_radius=2.0, iters=3000, seed=0, vertical_only=True):
    """Automatic surface-noise estimate, no manual segmentation:
    find the dominant *wall* (vertical plane, RANSAC), then measure sigma on a
    COMPACT local patch so the result reflects local sensor noise, not large-scale
    drift. Restricting to vertical walls keeps the metric comparable across scans
    (a floor and a wall are not interchangeable). Falls back to any dominant plane
    if no large wall is found. Returns (sigma_mm or None, info dict)."""
    if xyz.shape[0] < 200:
        return None, {}
    on_wall = True
    inl = ransac_plane(xyz, thr, iters, seed, max_abs_nz=0.4 if vertical_only else None)
    if inl.shape[0] < 100:                       # no sizeable wall -> fall back
        inl = ransac_plane(xyz, thr, iters, seed, max_abs_nz=None)
        on_wall = False
    if inl.shape[0] < 100:
        return None, {}
    rng = np.random.default_rng(seed)
    cand = inl[rng.choice(inl.shape[0], size=min(400, inl.shape[0]), replace=False)]
    r2, best_c, best_cnt = patch_radius ** 2, None, -1
    for c in cand:
        cnt = int(np.count_nonzero(np.sum((inl - c) ** 2, axis=1) < r2))
        if cnt > best_cnt:
            best_cnt, best_c = cnt, c
    patch = inl[np.sum((inl - best_c) ** 2, axis=1) < r2]
    normal, dist = fit_plane_pca(patch)
    return round(float(dist.std()) * 1000, 2), {
        "patch_points": int(patch.shape[0]),
        "measured_on_wall": bool(on_wall and abs(normal[2]) < 0.5),
    }


# --------------------------------------------------------------------------- #
# Core analyses                                                               #
# --------------------------------------------------------------------------- #
def analyze_scan(cc_exe, cloud, label, params, results_dir):
    """Density + global roughness on a full scan."""
    asc = results_dir / f"{label}_metrics.asc"
    log = results_dir / f"{label}_scan.log"
    # REMOVE_ALL_SFS guarantees the last two ASC columns are [density, roughness],
    # regardless of the input file's colours / scalar fields.
    cc_args = [
        "-C_EXPORT_FMT", "ASC", "-PREC", "4", "-SEP", "SPACE", "-AUTO_SAVE", "OFF",
        "-O", str(cloud), "-REMOVE_ALL_SFS",
        "-DENSITY", str(params["density_radius_m"]), "-TYPE", "SURFACE",
        "-ROUGH", str(params["roughness_kernel_m"]),
        "-SS", "SPATIAL", str(params["export_step_m"]),
        "-SAVE_CLOUDS", "FILE", str(asc),
    ]
    log_text = run_cc(cc_exe, cc_args, log)

    d = np.genfromtxt(asc)
    xyz, density, rough = d[:, 0:3], d[:, -2], d[:, -1]
    dens = density[np.isfinite(density)]
    rgh = rough[np.isfinite(rough)]
    dens_median = float(np.median(dens))
    spacing_mm = 1000.0 / np.sqrt(dens_median) if dens_median > 0 else float("nan")

    # Automatic surface noise (no manual segmentation needed).
    sigma_auto, pinfo = auto_plane_sigma(xyz)

    # Acquisition time: auto from LAS GPS-time, with optional manual override.
    duration = params.get("duration_min_override") or gps_duration_min(cloud)
    area = params.get("acq_area_m2") or 0

    return {
        "label": label,
        "cloud": str(cloud),
        "n_points": parse_npoints(log_text),
        "bbox_m": [round(float(np.ptp(xyz[:, i])), 2) for i in range(3)],
        "density_radius_m": params["density_radius_m"],
        "density_median_pts_m2": round(dens_median, 1),
        "density_mean_pts_m2": round(float(dens.mean()), 1),
        "spacing_mm": round(float(spacing_mm), 2),
        "density_class": classify(spacing_mm, SPACING_CLASSES),
        "roughness_kernel_m": params["roughness_kernel_m"],
        "roughness_median_mm": round(float(np.median(rgh)) * 1000, 2),
        "roughness_p90_mm": round(float(np.percentile(rgh, 90)) * 1000, 2),
        # auto noise on the dominant plane (local patch)
        "sigma_planar_mm": sigma_auto,
        "noise_class": classify(sigma_auto, SIGMA_CLASSES) if sigma_auto else "",
        "noise_method": "auto (dominant wall, local patch)",
        "measured_on_wall": pinfo.get("measured_on_wall"),
        "acq_duration_min": duration,
        "acq_s_per_m2": (round(duration * 60.0 / area, 2) if duration and area > 0 else None),
    }


def analyze_plane(cc_exe, wall, label, params, results_dir, use_ransac=True):
    """Surface noise (sigma) from point-to-plane residuals on a segmented patch."""
    asc = results_dir / f"{label}_wall.asc"
    log = results_dir / f"{label}_plane.log"
    cc_args = [
        "-C_EXPORT_FMT", "ASC", "-PREC", "5", "-SEP", "SPACE", "-AUTO_SAVE", "OFF",
        "-O", str(wall), "-REMOVE_ALL_SFS", "-SAVE_CLOUDS", "FILE", str(asc),
    ]
    run_cc(cc_exe, cc_args, log)

    xyz = np.genfromtxt(asc)[:, 0:3]
    n_tot = xyz.shape[0]
    if use_ransac:
        fit = ransac_plane(xyz, params["ransac_inlier_m"], params["ransac_iters"])
        method = f"RANSAC (inlier {params['ransac_inlier_m']*1000:.0f} mm)"
    else:
        fit, method = xyz, "PCA on full selection"
    _, dist = fit_plane_pca(fit)
    sigma_mm = round(float(dist.std()) * 1000, 3)

    return {
        "label": label,
        "wall": str(wall),
        "method": method,
        "n_points_selection": int(n_tot),
        "n_points_plane": int(fit.shape[0]),
        "inlier_pct": round(100.0 * fit.shape[0] / n_tot, 1),
        "sigma_planar_mm": sigma_mm,
        "max_dev_mm": round(float(np.abs(dist).max()) * 1000, 2),
        "noise_class": classify(sigma_mm, SIGMA_CLASSES),
    }


# --------------------------------------------------------------------------- #
# M3C2 direct comparison (reference scan vs another scan of the SAME scene)    #
# --------------------------------------------------------------------------- #
def write_m3c2_params(path, normal_scale, proj_scale, max_depth, subsample):
    # Full key set expected by the qM3C2 loader (CloudCompare >= 2.11).
    # M3C2VER=1 is mandatory: without it the plugin rejects the file and aborts.
    path.write_text(
        "[General]\n"
        "M3C2VER=1\n"
        "ExportDensityAtProjScale=false\n"
        "ExportStdDevInfo=false\n"
        "MaxThreadCount=0\n"
        "MinPoints4Stat=5\n"
        "NormalMaxScale=0\n"
        "NormalMinScale=0\n"
        "NormalMode=0\n"
        "NormalPreferedOri=4\n"
        f"NormalScale={normal_scale}\n"
        "NormalStep=0\n"
        "NormalUseCorePoints=false\n"
        "PM1Scale=0\n"
        "PM2Scale=0\n"
        "PositiveSearchOnly=false\n"
        "ProjDestIndex=0\n"
        "RegistrationError=0\n"
        "RegistrationErrorEnabled=false\n"
        f"SearchDepth={max_depth}\n"
        f"SearchScale={proj_scale}\n"
        "SubsampleEnabled=true\n"
        f"SubsampleRadius={subsample}\n"
        "UseMedian=false\n"
        "UseMinPoints4Stat=false\n"
        "UseOriginalCloud=false\n"
        "UseSinglePass4Depth=false\n"
        "bestAccuracy=false\n"
    )


def identify_signed_sf(d, n_xyz=3):
    """Pick the continuous, *signed* scalar-field column = the M3C2 distance.
    Skips binary fields (significant change) and non-negative ones (RGB, uncertainty)."""
    best_col, best_std = None, -1.0
    for i in range(n_xyz, d.shape[1]):
        col = d[:, i]
        fin = col[np.isfinite(col)]
        if fin.size == 0 or np.unique(fin).size <= 2 or fin.min() >= 0:
            continue
        if fin.std() > best_std:
            best_std, best_col = float(fin.std()), i
    return best_col


def analyze_m3c2(cc_exe, reference, compared, label, params, results_dir, dist_col=None):
    pfile = results_dir / f"{label}_m3c2_params.txt"
    write_m3c2_params(pfile, params["m3c2_normal_scale"], params["m3c2_proj_scale"],
                      params["m3c2_max_depth"], params["m3c2_subsample"])
    ply_ref = results_dir / f"{label}_m3c2_ref.ply"
    ply_cmp = results_dir / f"{label}_m3c2_cmp.ply"
    ply_res = results_dir / f"{label}_m3c2.ply"   # M3C2 result (core points) carrying distance SF
    log = results_dir / f"{label}_m3c2.log"
    # M3C2 appends a THIRD cloud (the core-points result carrying the distance SF).
    # Use PLY to preserve scalar fields (ASC text format doesn't preserve them).
    cc_args = [
        "-C_EXPORT_FMT", "PLY", "-AUTO_SAVE", "OFF",
        "-O", str(reference), "-O", str(compared), "-REMOVE_ALL_SFS",
        "-M3C2", str(pfile),
        "-CLEAR_NORMALS",   # M3C2 leaves per-point normals; drop them so the only
                            # signed scalar field left is the M3C2 distance itself.
        "-SAVE_CLOUDS", "FILE", f"{ply_ref} {ply_cmp} {ply_res}",
    ]
    run_cc(cc_exe, cc_args, log)

    # Read the M3C2 distance scalar field from the PLY (preserves named scalar fields).
    import plyfile
    ply = plyfile.PlyData.read(str(ply_res))
    names = [p.name for p in ply["vertex"].properties]
    dname = next((n for n in names if "m3c2" in n.lower() and "dist" in n.lower()), None)
    if dname is None:
        dname = next((n for n in names if "dist" in n.lower() and "uncert" not in n.lower()), None)
    if dname is None:
        sys.exit(f"M3C2 distance scalar field not found in {ply_res} (fields: {names})")
    dist = np.asarray(ply["vertex"][dname]).astype(float)
    fin = dist[np.isfinite(dist)] * 1000.0  # -> mm
    valid_pct = round(100.0 * fin.size / dist.size, 1)
    if fin.size == 0:
        sys.exit("M3C2 found no overlapping points (0% valid): the clouds are not aligned. "
                 "Co-registration likely failed (check registration fitness).")
    # Outlier exclusion (locked equity parameter, as in the paper): residuals farther
    # than k*sigma from the mean (default k=3) are dropped, so a handful of gross
    # mismatches (clutter, moving objects, unmasked vegetation) cannot inflate the
    # statistics of any scanner. Never empties the sample (Chebyshev: >= 8/9 kept).
    k_sig = params.get("m3c2_outlier_sigma", 3.0)
    n_raw = fin.size
    mu, sd = float(fin.mean()), float(fin.std())
    if k_sig and sd > 0:
        fin = fin[np.abs(fin - mu) <= k_sig * sd]
    n_out = int(n_raw - fin.size)
    a = np.abs(fin)
    out = {
        "label": label, "reference": str(reference), "compared": str(compared),
        "dist_field": dname, "n_core_points": int(dist.size),
        "valid_pct": valid_pct,
        "outliers_excluded_pct": round(100.0 * n_out / n_raw, 2),
        "m3c2_mean_mm": round(float(fin.mean()), 3),
        "m3c2_median_mm": round(float(np.median(fin)), 3),
        "m3c2_std_mm": round(float(fin.std()), 3),
        "m3c2_p95abs_mm": round(float(np.percentile(a, 95)), 3),
        "params": {"normal_scale_m": params["m3c2_normal_scale"],
                   "proj_scale_m": params["m3c2_proj_scale"],
                   "max_depth_m": params["m3c2_max_depth"],
                   "subsample_m": params["m3c2_subsample"],
                   "outlier_sigma": k_sig},
    }
    for t in (5, 15, 50):
        out[f"within_{t}mm_pct"] = round(100.0 * float(np.mean(a <= t)), 1)
    return out


# --------------------------------------------------------------------------- #
# Geometric deviation vs a reference cloud (co-registration + M3C2)            #
# This answers the core question: how far does a low-cost scanner deviate from #
# the best instrument (the reference, e.g. BLK2GO), on the same scene?         #
# --------------------------------------------------------------------------- #
def _export_ply(cc_exe, cloud, out_ply, voxel, results_dir):
    """Down-sample a cloud and export it to PLY (so Open3D can read it)."""
    run_cc(cc_exe, ["-C_EXPORT_FMT", "PLY", "-AUTO_SAVE", "OFF", "-O", str(cloud),
                    "-REMOVE_ALL_SFS", "-SS", "SPATIAL", str(voxel),
                    "-SAVE_CLOUDS", "FILE", str(out_ply)], results_dir / "_reg_export.log")


def register_clouds(cc_exe, reference, target, results_dir, voxel=0.04, init=None):
    """Register `target` onto `reference`, then point-to-plane ICP refinement (Open3D).
    The coarse alignment comes either from a user-supplied `init` 4x4 matrix
    (operator point-pair picking in CloudCompare, robust on real partial-overlap
    scans) or, if none, from automatic FPFH + RANSAC global registration.
    Returns (4x4 transform target->reference, ICP fitness, ICP inlier RMSE)."""
    import open3d as o3d
    reg = o3d.pipelines.registration
    ref_ply, tgt_ply = results_dir / "_reg_ref.ply", results_dir / "_reg_tgt.ply"
    _export_ply(cc_exe, reference, ref_ply, voxel, results_dir)
    _export_ply(cc_exe, target, tgt_ply, voxel, results_dir)
    ref = o3d.io.read_point_cloud(str(ref_ply))
    tgt = o3d.io.read_point_cloud(str(tgt_ply))

    if init is not None:
        coarse_T = np.asarray(init, dtype=float)
    else:
        # Automatic global registration: FPFH + RANSAC on aggressively down-sampled clouds.
        ds_voxel = max(0.10, voxel * 3)
        ref_ds, tgt_ds = ref.voxel_down_sample(ds_voxel), tgt.voxel_down_sample(ds_voxel)

        def feat(p):
            p.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=ds_voxel * 2, max_nn=30))
            return reg.compute_fpfh_feature(
                p, o3d.geometry.KDTreeSearchParamHybrid(radius=ds_voxel * 5, max_nn=100))

        ref_f, tgt_f = feat(ref_ds), feat(tgt_ds)
        d = ds_voxel * 1.5
        coarse_T = reg.registration_ransac_based_on_feature_matching(
            tgt_ds, ref_ds, tgt_f, ref_f, True, d,
            reg.TransformationEstimationPointToPoint(False), 3,
            [reg.CorrespondenceCheckerBasedOnEdgeLength(0.9),
             reg.CorrespondenceCheckerBasedOnDistance(d)],
            reg.RANSACConvergenceCriteria(100000, 0.999)).transformation

    # Point-to-plane ICP requires normals on both clouds
    ref.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100))
    tgt.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100))
    # Multi-scale ICP: tolerate a rough manual init (~0.5 m off), then tighten.
    T, fine = coarse_T, None
    for maxd in (voxel * 12, voxel * 4, voxel * 2):
        fine = reg.registration_icp(
            tgt, ref, maxd, T, reg.TransformationEstimationPointToPlane(),
            reg.ICPConvergenceCriteria(max_iteration=50))
        T = fine.transformation
    return np.asarray(T), float(fine.fitness), float(fine.inlier_rmse)


def _write_matrix(T, path):
    Path(path).write_text("\n".join(" ".join(f"{v:.8f}" for v in row) for row in T) + "\n")


def analyze_deviation(cc_exe, reference, target, label, params, results_dir, voxel=0.04, init=None):
    """Register `target` onto `reference`, then M3C2 -> geometric deviation.
    `init` is an optional 4x4 matrix (operator point-pair pre-alignment) used to
    seed ICP instead of automatic FPFH+RANSAC global registration."""
    T, fitness, rmse = register_clouds(cc_exe, reference, target, results_dir, voxel, init)
    mat = results_dir / f"{label}_align.txt"
    _write_matrix(T, mat)
    aligned = results_dir / f"{label}_aligned.bin"
    run_cc(cc_exe, ["-C_EXPORT_FMT", "BIN", "-AUTO_SAVE", "OFF", "-O", str(target),
                    "-APPLY_TRANS", str(mat), "-SAVE_CLOUDS", "FILE", str(aligned)],
           results_dir / f"{label}_align.log")
    m = analyze_m3c2(cc_exe, reference, aligned, label, params, results_dir)
    p95 = m["m3c2_p95abs_mm"]
    # Registration is unreliable if ICP found ~no inliers or M3C2 sees little overlap:
    # in that case the deviation numbers are meaningless, so flag instead of reporting them.
    ok = fitness >= 0.10 and m["valid_pct"] >= 30.0
    if not ok:
        print(f"WARNING [{label}]: registration unreliable (ICP fitness {fitness:.3f}, "
              f"M3C2 valid {m['valid_pct']}%): deviation figures are NOT meaningful, "
              "class flagged as REGISTRATION FAILED. Retry with --init-transform "
              "(CloudCompare point-pair pre-alignment).", file=sys.stderr)
    res = {
        "label": label, "reference": str(reference), "target": str(target),
        "registration_fitness": round(fitness, 3), "registration_rmse_mm": round(rmse * 1000, 2),
        "m3c2_valid_pct": m["valid_pct"], "registration_ok": ok,
        "deviation_mean_mm": m["m3c2_mean_mm"], "deviation_std_mm": m["m3c2_std_mm"],
        "deviation_p95abs_mm": round(p95, 2),
        "deviation_class": classify(p95, DEVIATION_CLASSES) if ok else "REGISTRATION FAILED",
    }
    for t in (5, 15, 50):
        res[f"within_{t}mm_pct"] = m[f"within_{t}mm_pct"]
    return res


# --------------------------------------------------------------------------- #
# Reporting                                                                   #
# --------------------------------------------------------------------------- #
COLUMNS = [
    ("label", "Scanner"),
    ("n_points", "Points"),
    ("density_median_pts_m2", "Density (pts/m^2)"),
    ("spacing_mm", "Spacing (mm)"),
    ("density_class", "Density class"),
    ("sigma_planar_mm", "Noise sigma (mm)"),
    ("noise_class", "Noise class"),
]


def build_report(results_dir, out_dir):
    scans, planes, m3c2, deviation = {}, {}, {}, {}
    for f in results_dir.glob("*.json"):
        data = json.loads(f.read_text())
        if f.stem.endswith("_plane"):
            planes[data["label"]] = data
        elif f.stem.endswith("_deviation"):
            deviation[data["label"]] = data
        elif f.stem.endswith("_m3c2"):
            m3c2[data["label"]] = data
        else:
            scans[data["label"]] = data
    if not (scans or planes or m3c2 or deviation):
        sys.exit(f"No results found in {results_dir}. Run 'scan'/'plane'/'deviation' first.")

    columns = list(COLUMNS)
    if any(s.get("acq_duration_min") for s in scans.values()):
        columns += [("acq_duration_min", "Acq. time (min)")]
        if any(s.get("acq_s_per_m2") for s in scans.values()):
            columns += [("acq_s_per_m2", "Acq. (s/m²)")]
    if deviation:
        columns += [("deviation_p95abs_mm", "Deviation p95 (mm)"),
                    ("within_5mm_pct", "% <=5mm"), ("within_15mm_pct", "% <=15mm"),
                    ("within_50mm_pct", "% <=50mm"), ("deviation_class", "Deviation class")]
    if m3c2:
        columns += [("m3c2_mean_mm", "M3C2 mean (mm)"), ("m3c2_std_mm", "M3C2 sigma (mm)")]
    # Overall class = the worst among the classes available for the device.
    columns += [("overall_class", "Overall class")]

    rows = []
    for label in sorted(set(scans) | set(planes) | set(m3c2) | set(deviation)):
        row = {"label": label}
        for k in ("n_points", "density_median_pts_m2", "spacing_mm", "density_class"):
            row[k] = scans.get(label, {}).get(k, "")
        # noise: prefer a manual wall (plane.json) if present, else the auto value from scan
        for k in ("sigma_planar_mm", "noise_class"):
            row[k] = planes.get(label, {}).get(k) or scans.get(label, {}).get(k, "")
        for k in ("acq_duration_min", "acq_s_per_m2"):
            row[k] = scans.get(label, {}).get(k) or ""
        for k in ("deviation_p95abs_mm", "within_5mm_pct", "within_15mm_pct",
                  "within_50mm_pct", "deviation_class"):
            row[k] = deviation.get(label, {}).get(k, "")
        for k in ("m3c2_mean_mm", "m3c2_std_mm"):
            row[k] = m3c2.get(label, {}).get(k, "")
        row["overall_class"] = overall_class(row.get("density_class", ""),
                                             row.get("noise_class", ""),
                                             row.get("deviation_class", ""))
        rows.append(row)

    csv_path = out_dir / "comparison.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([h for _, h in columns])
        for r in rows:
            w.writerow([r.get(k, "") for k, _ in columns])

    md = ["# Scanner comparison", "",
          "| " + " | ".join(h for _, h in columns) + " |",
          "|" + "|".join(["---"] * len(columns)) + "|"]
    for r in rows:
        md.append("| " + " | ".join(str(r.get(k, "")) for k, _ in columns) + " |")
    md_path = out_dir / "comparison.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")

    print("\n".join(md))
    print(f"\n-> {csv_path}\n-> {md_path}")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def save_json(results_dir, name, obj):
    out = results_dir / f"{name}.json"
    out.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    print(f"-> {out}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cc-exe", help="path to CloudCompare executable")
    p.add_argument("--results-dir", default="results", help="where per-scan JSON is stored")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="density + global roughness on a full scan")
    s.add_argument("--cloud", required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--radius", type=float, default=DEFAULTS["density_radius_m"])
    s.add_argument("--duration-min", type=float, default=None, help="acquisition time (min) if no GPS-time")
    s.add_argument("--area-m2", type=float, default=0, help="surveyed area (m2) to get time per m2")

    pl = sub.add_parser("plane", help="surface-noise sigma on a segmented flat patch")
    pl.add_argument("--wall", required=True)
    pl.add_argument("--label", required=True)
    pl.add_argument("--inlier", type=float, default=DEFAULTS["ransac_inlier_m"])
    pl.add_argument("--no-ransac", action="store_true", help="plain PCA (no plane isolation)")

    m = sub.add_parser("m3c2", help="direct M3C2 distance vs a reference scan (SAME scene only)")
    m.add_argument("--reference", required=True, help="reference scan (e.g. the most accurate device)")
    m.add_argument("--compared", required=True, help="scan to compare, same environment")
    m.add_argument("--label", required=True)
    m.add_argument("--normal-scale", type=float, default=DEFAULTS["m3c2_normal_scale"])
    m.add_argument("--proj-scale", type=float, default=DEFAULTS["m3c2_proj_scale"])
    m.add_argument("--max-depth", type=float, default=DEFAULTS["m3c2_max_depth"])
    m.add_argument("--dist-col", type=int, default=None, help="override M3C2 distance column index")

    dv = sub.add_parser("deviation", help="geometric deviation vs a reference (co-registration + M3C2)")
    dv.add_argument("--reference", required=True, help="reference cloud (e.g. the best instrument, BLK2GO)")
    dv.add_argument("--target", required=True, help="low-cost scan of the SAME scene")
    dv.add_argument("--label", required=True)
    dv.add_argument("--voxel", type=float, default=0.04, help="registration down-sampling voxel (m)")
    dv.add_argument("--init-transform", default=None,
                    help="4x4 matrix file (CloudCompare point-pair pre-alignment) to seed ICP")

    sub.add_parser("report", help="aggregate results into comparison.csv + comparison.md")

    b = sub.add_parser("batch", help="run scan+plane (and deviation vs the manifest "
                                     "'reference', if set) for every scanner in a JSON manifest")
    b.add_argument("--manifest", required=True)

    args = p.parse_args()
    if getattr(args, "label", None):
        args.label = safe_label(args.label)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(exist_ok=True)
    params = dict(DEFAULTS)

    if args.cmd == "report":
        build_report(results_dir, Path("."))
        return

    cc_exe = find_cloudcompare(args.cc_exe)

    if args.cmd == "scan":
        params["density_radius_m"] = args.radius
        params["duration_min_override"] = args.duration_min
        params["acq_area_m2"] = args.area_m2
        save_json(results_dir, args.label, analyze_scan(cc_exe, args.cloud, args.label, params, results_dir))
    elif args.cmd == "plane":
        params["ransac_inlier_m"] = args.inlier
        res = analyze_plane(cc_exe, args.wall, args.label, params, results_dir, use_ransac=not args.no_ransac)
        save_json(results_dir, f"{args.label}_plane", res)
    elif args.cmd == "m3c2":
        params["m3c2_normal_scale"] = args.normal_scale
        params["m3c2_proj_scale"] = args.proj_scale
        params["m3c2_max_depth"] = args.max_depth
        res = analyze_m3c2(cc_exe, args.reference, args.compared, args.label,
                           params, results_dir, dist_col=args.dist_col)
        save_json(results_dir, f"{args.label}_m3c2", res)
    elif args.cmd == "deviation":
        init = np.loadtxt(args.init_transform) if args.init_transform else None
        if init is not None and init.shape != (4, 4):
            sys.exit(f"--init-transform must be a 4x4 matrix (got shape {init.shape})")
        res = analyze_deviation(cc_exe, args.reference, args.target, args.label,
                                params, results_dir, voxel=args.voxel, init=init)
        save_json(results_dir, f"{args.label}_deviation", res)
    elif args.cmd == "batch":
        manifest_path = Path(args.manifest)
        manifest = json.loads(manifest_path.read_text())
        params.update(manifest.get("params", {}))
        base_dir = manifest_path.resolve().parent

        def resolve(rel):
            """Manifest paths are resolved against the manifest's own folder."""
            q = Path(rel)
            return str(q if q.is_absolute() else base_dir / q)

        def entry_params(entry):
            """Per-device params: 'acq_min' = manual acquisition time (e.g. for .e57)."""
            ep = dict(params)
            if entry.get("acq_min") is not None:
                ep["duration_min_override"] = entry["acq_min"]
            return ep

        # Optional reference cloud: measured like any device, then used to compute the
        # geometric deviation of every enabled scanner against it.
        reference = manifest.get("reference") or {}
        ref_cloud = resolve(reference["cloud"]) if reference.get("cloud") else None
        if ref_cloud:
            ref_label = safe_label(reference.get("label", "REFERENCE"))
            save_json(results_dir, ref_label,
                      analyze_scan(cc_exe, ref_cloud, ref_label, entry_params(reference), results_dir))
            if reference.get("wall"):
                save_json(results_dir, f"{ref_label}_plane",
                          analyze_plane(cc_exe, resolve(reference["wall"]), ref_label, params, results_dir))

        for sc in manifest["scanners"]:
            label = safe_label(sc["label"])
            if not sc.get("enabled", True):
                print(f"[batch] {label}: disabled in manifest, skipped.")
                continue
            sc_params = entry_params(sc)
            if sc.get("cloud"):
                save_json(results_dir, label,
                          analyze_scan(cc_exe, resolve(sc["cloud"]), label, sc_params, results_dir))
            if sc.get("wall"):
                save_json(results_dir, f"{label}_plane",
                          analyze_plane(cc_exe, resolve(sc["wall"]), label, sc_params, results_dir))
            if ref_cloud and sc.get("cloud"):
                # Deviation failures (bad registration, no overlap) must not kill the
                # whole batch: report them clearly and move on to the next device.
                try:
                    save_json(results_dir, f"{label}_deviation",
                              analyze_deviation(cc_exe, ref_cloud, resolve(sc["cloud"]),
                                                label, sc_params, results_dir))
                except (SystemExit, Exception) as e:  # noqa: BLE001 - keep the batch alive
                    print(f"[batch] {label}: deviation vs {reference.get('label', 'reference')} "
                          f"FAILED: {e}", file=sys.stderr)
        build_report(results_dir, Path("."))


if __name__ == "__main__":
    main()

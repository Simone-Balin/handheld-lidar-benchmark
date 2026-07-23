#!/usr/bin/env python3
"""
render_std
==========
Standardized point-cloud renders for blind (VLM) evaluation and paper figures,
companion to benchmark.py (handheld-lidar-benchmark).

Every cloud is rendered with the SAME locked parameters, so the resulting PNGs
are directly comparable across scanners. Reproducibility beats beauty: no GPU,
no Open3D, no matplotlib. A deterministic orthographic rasterizer built on
numpy + Pillow only (1 point = 1 pixel, z-buffered).

Fixed views (all orthographic):
  * plan          top view, XY projection (highest point wins the z-buffer)
  * elev_x        elevation looking along +X (YZ plane, Z up)
  * elev_y        elevation looking along +Y (XZ plane, Z up)
  * iso           isometric axonometry (azimuth 45 deg, elevation 35.264 deg)
  * section_120   horizontal section: only points with z between
                  floor+1.15 m and floor+1.25 m, projected on XY
                  (floor level = 2nd percentile of z)

Locked equity parameters (identical for every cloud):
  * image 1600 x 1200 px, white background, point size 1 px
  * z-buffer: the point nearest to the observer wins each pixel
  * colour ramp on elevation (z), range locked to the 2nd..98th percentile
    of the cloud, hand-coded viridis-like LUT (32 anchors -> 256 entries)
  * clouds above 8 M points are uniformly subsampled to 8 M with seed 0
    (display only; declared in the log and in render_params.json)

Usage
-----
  python render_std.py --cloud BLK2GO.las --label BLK2GO
  python render_std.py --cloud room.ply   --label iPhone --out results/renders

Output: <out>/<label>/{plan,elev_x,elev_y,iso,section_120}.png, a 2x3
contact_sheet.png with view labels, and render_params.json (full audit trail).
Requires: Python 3.8+, numpy, Pillow, laspy (.las/.laz), plyfile (.ply).
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Configuration & defaults (locked equity parameters, same for every cloud)    #
# --------------------------------------------------------------------------- #
PARAMS = {
    "image_width_px": 1600,
    "image_height_px": 1200,
    "margin_px": 40,               # blank frame around the drawing
    "background_rgb": [255, 255, 255],
    "point_size_px": 1,
    "depth_test": "z-buffer, point nearest to the observer wins",
    "z_range_percentiles": [2.0, 98.0],   # colour ramp range on z
    "floor_percentile": 2.0,              # floor level estimate
    "section_band_above_floor_m": [1.15, 1.25],
    "max_points": 8_000_000,       # uniform random subsampling above this
    "sample_seed": 0,
    "iso_azimuth_deg": 45.0,
    "iso_elevation_deg": 35.264,   # atan(1/sqrt(2)) -> true isometric
}

VIEW_ORDER = ["plan", "elev_x", "elev_y", "iso", "section_120"]

# Hand-coded viridis-like colormap: 32 RGB anchors (dark purple -> teal ->
# yellow), linearly interpolated to a 256-entry LUT. No matplotlib involved.
VIRIDIS_ANCHORS = [
    (68, 1, 84), (71, 13, 96), (72, 24, 106), (72, 35, 116),
    (71, 45, 123), (69, 55, 129), (66, 64, 134), (62, 73, 137),
    (58, 82, 139), (54, 90, 140), (50, 98, 141), (46, 106, 142),
    (43, 114, 142), (40, 122, 142), (37, 130, 142), (34, 137, 141),
    (32, 145, 140), (30, 152, 138), (30, 160, 135), (33, 167, 132),
    (39, 174, 127), (48, 181, 121), (60, 188, 114), (74, 194, 106),
    (90, 200, 97), (108, 206, 86), (127, 211, 75), (147, 216, 62),
    (168, 219, 50), (190, 223, 38), (216, 226, 28), (253, 231, 37),
]
LUT_SIZE = 256


def log(msg):
    print(f"[render_std] {msg}")


def safe_label(s):
    """Filesystem-safe label (same convention as benchmark.py)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s).strip()).strip("_") or "scan"


def build_lut():
    """Expand the 32 viridis-like anchors into a (LUT_SIZE, 3) uint8 table."""
    anchors = np.asarray(VIRIDIS_ANCHORS, dtype=np.float64)
    xs = np.linspace(0.0, 1.0, anchors.shape[0])
    xq = np.linspace(0.0, 1.0, LUT_SIZE)
    lut = np.stack([np.interp(xq, xs, anchors[:, c]) for c in range(3)], axis=1)
    return np.clip(np.round(lut), 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Cloud loading (float32 local coordinates, memory-friendly)                   #
# --------------------------------------------------------------------------- #
def _load_las(path):
    """Read a .las/.laz in chunks (laspy 2.x). Coordinates are re-anchored to
    the header minima in float64 BEFORE the float32 cast, so georeferenced
    clouds (UTM-scale offsets) keep millimetre precision."""
    import laspy
    try:
        with laspy.open(str(path)) as rd:
            n = int(rd.header.point_count)
            anchor = np.asarray(rd.header.mins, dtype=np.float64)
            if anchor.shape != (3,) or not np.all(np.isfinite(anchor)):
                anchor = None
            xyz = np.empty((n, 3), dtype=np.float32)
            i = 0
            for pts in rd.chunk_iterator(2_000_000):
                cx = np.asarray(pts.x, dtype=np.float64)
                cy = np.asarray(pts.y, dtype=np.float64)
                cz = np.asarray(pts.z, dtype=np.float64)
                if anchor is None:
                    anchor = np.array([cx.min(), cy.min(), cz.min()])
                m = cx.size
                xyz[i:i + m, 0] = (cx - anchor[0]).astype(np.float32)
                xyz[i:i + m, 1] = (cy - anchor[1]).astype(np.float32)
                xyz[i:i + m, 2] = (cz - anchor[2]).astype(np.float32)
                i += m
                del cx, cy, cz
            return xyz[:i], anchor
    except AttributeError:
        # very old laspy without the streaming API: read the file whole
        las = laspy.read(str(path))
        cx = np.asarray(las.x, dtype=np.float64)
        cy = np.asarray(las.y, dtype=np.float64)
        cz = np.asarray(las.z, dtype=np.float64)
        anchor = np.array([cx.min(), cy.min(), cz.min()])
        xyz = np.column_stack([(cx - anchor[0]).astype(np.float32),
                               (cy - anchor[1]).astype(np.float32),
                               (cz - anchor[2]).astype(np.float32)])
        del las, cx, cy, cz
        return xyz, anchor


def _load_ply(path):
    from plyfile import PlyData
    ply = PlyData.read(str(path))
    v = ply["vertex"]
    cx = np.asarray(v["x"], dtype=np.float64)
    cy = np.asarray(v["y"], dtype=np.float64)
    cz = np.asarray(v["z"], dtype=np.float64)
    del ply, v
    anchor = np.array([cx.min(), cy.min(), cz.min()])
    xyz = np.column_stack([(cx - anchor[0]).astype(np.float32),
                           (cy - anchor[1]).astype(np.float32),
                           (cz - anchor[2]).astype(np.float32)])
    del cx, cy, cz
    return xyz, anchor


def load_cloud(path):
    """Load .las/.laz (laspy) or .ply (plyfile) -> (xyz float32, anchor).
    xyz is expressed relative to `anchor` (float64), non-finite rows dropped."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"Cloud not found: {p}")
    suffix = p.suffix.lower()
    if suffix in (".las", ".laz"):
        xyz, anchor = _load_las(p)
    elif suffix == ".ply":
        xyz, anchor = _load_ply(p)
    else:
        sys.exit(f"Unsupported format '{suffix}' (use .las, .laz or .ply)")
    if xyz.shape[0] == 0:
        sys.exit(f"No points found in {p}")
    finite = np.isfinite(xyz).all(axis=1)
    if not finite.all():
        log(f"dropped {int((~finite).sum()):,} non-finite points")
        xyz = xyz[finite]
    return xyz, anchor


def subsample(xyz, max_points, seed):
    """Uniform random subsampling (display only). Deterministic: fixed seed."""
    n = xyz.shape[0]
    if n <= max_points:
        return xyz, False
    rng = np.random.default_rng(seed)
    sel = rng.choice(n, size=max_points, replace=False)
    sel.sort()  # keep file order -> deterministic and cache-friendly
    log(f"subsampled {n:,} -> {max_points:,} points "
        f"(uniform random, seed={seed}, DISPLAY ONLY)")
    return xyz[sel], True


# --------------------------------------------------------------------------- #
# Orthographic rasterizer (numpy z-buffer, 1 point = 1 pixel)                  #
# --------------------------------------------------------------------------- #
def iso_basis(az_deg, el_deg):
    """Camera basis for the isometric view. The camera sits in the direction
    (cos el cos az, cos el sin az, sin el) from the scene and looks back at it;
    with az=45 and el=35.264 the view direction is (-1,-1,-1)/sqrt(3)."""
    az, el = np.radians(az_deg), np.radians(el_deg)
    cam = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    d = -cam / np.linalg.norm(cam)               # view direction (into the scene)
    r = np.cross(d, [0.0, 0.0, 1.0])
    r = r / np.linalg.norm(r)                    # screen right
    u = np.cross(r, d)                           # screen up
    return r, u, d


def view_bases():
    """Fixed (right, up, viewdir) triples for every view. viewdir points from
    the observer INTO the scene, so the z-buffer keeps the minimum of
    dot(point, viewdir): the point nearest to the observer wins."""
    bases = {
        "plan":   (np.array([1.0, 0.0, 0.0]),    # top view, north (+Y) up
                   np.array([0.0, 1.0, 0.0]),
                   np.array([0.0, 0.0, -1.0])),
        "elev_x": (np.array([0.0, -1.0, 0.0]),   # looking along +X, Z up
                   np.array([0.0, 0.0, 1.0]),
                   np.array([1.0, 0.0, 0.0])),
        "elev_y": (np.array([1.0, 0.0, 0.0]),    # looking along +Y, Z up
                   np.array([0.0, 0.0, 1.0]),
                   np.array([0.0, 1.0, 0.0])),
        "iso": iso_basis(PARAMS["iso_azimuth_deg"], PARAMS["iso_elevation_deg"]),
    }
    bases["section_120"] = bases["plan"]         # section is a filtered plan
    return {k: tuple(v.astype(np.float32) for v in basis)
            for k, basis in bases.items()}


def render_view(xyz, colors, basis, note_if_empty=None):
    """Rasterize `xyz` (N,3 float32) orthographically onto a fixed-size white
    canvas. Deterministic z-buffer: pixels are resolved with a stable lexsort
    on (pixel index, depth), so the nearest point wins and ties break on file
    order. Returns (PIL.Image, per-view metadata dict)."""
    W, H = PARAMS["image_width_px"], PARAMS["image_height_px"]
    margin = PARAMS["margin_px"]
    flat = np.full((W * H, 3), 255, dtype=np.uint8)

    n = xyz.shape[0]
    meta = {"n_points": int(n)}
    if n == 0:
        img = Image.fromarray(flat.reshape(H, W, 3))
        if note_if_empty:
            d = ImageDraw.Draw(img)
            d.text((W // 2, H // 2), note_if_empty, fill=(120, 120, 120),
                   font=_font(28), anchor="mm")
        meta.update({"mm_per_px": None, "extent_m": [0.0, 0.0]})
        return img, meta

    right, up, viewdir = basis
    sx = xyz @ right
    sy = xyz @ up
    depth = xyz @ viewdir

    sx_min, sx_max = float(sx.min()), float(sx.max())
    sy_min, sy_max = float(sy.min()), float(sy.max())
    dx, dy = sx_max - sx_min, sy_max - sy_min
    cand = []
    if dx > 0:
        cand.append((W - 2 * margin) / dx)
    if dy > 0:
        cand.append((H - 2 * margin) / dy)
    scale = min(cand) if cand else 1.0           # px per metre, uniform

    ox = (W - dx * scale) / 2.0                  # centre the drawing
    oy = (H - dy * scale) / 2.0
    px = np.clip(np.floor((sx - sx_min) * scale + ox), 0, W - 1).astype(np.int32)
    py = np.clip(np.floor((sy - sy_min) * scale + oy), 0, H - 1).astype(np.int32)
    del sx, sy
    pix = (np.int64(H - 1) - py) * W + px        # image origin is top-left
    del px, py

    # deterministic z-buffer: sort by (pixel, depth), keep the first per pixel
    order = np.lexsort((depth, pix))
    pix_sorted = pix[order]
    first = np.ones(order.size, dtype=bool)
    first[1:] = pix_sorted[1:] != pix_sorted[:-1]
    winners = order[first]
    flat[pix[winners]] = colors[winners]
    del depth, pix, order, pix_sorted, first

    meta.update({
        "n_pixels_hit": int(winners.size),
        "px_per_m": round(scale, 4),
        "mm_per_px": round(1000.0 / scale, 3) if scale > 0 else None,
        "extent_m": [round(dx, 3), round(dy, 3)],
    })
    return Image.fromarray(flat.reshape(H, W, 3)), meta


# --------------------------------------------------------------------------- #
# Contact sheet (2 x 3 grid with view labels, Pillow only)                     #
# --------------------------------------------------------------------------- #
def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                            # Pillow < 10.1
        return ImageFont.load_default()


def contact_sheet(images, metas, label, info_lines):
    """Compose the five views + one info tile into a labelled 2x3 grid."""
    cw, ch, strip, gap = 800, 600, 44, 8
    cell_h = strip + ch
    sheet_w = 3 * cw + 4 * gap
    sheet_h = 2 * cell_h + 3 * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    f_label, f_info = _font(24), _font(19)

    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    cells = VIEW_ORDER + ["info"]
    for k, name in enumerate(cells):
        row, col = divmod(k, 3)
        x0 = gap + col * (cw + gap)
        y0 = gap + row * (cell_h + gap)
        # label strip
        draw.rectangle([x0, y0, x0 + cw - 1, y0 + strip - 1], fill=(235, 235, 235))
        if name == "info":
            title = f"{label} (parameters)"
        else:
            mm = metas[name].get("mm_per_px")
            title = f"{name}   1 px = {mm:g} mm" if mm else name
        draw.text((x0 + 12, y0 + strip // 2), title, fill=(0, 0, 0),
                  font=f_label, anchor="lm")
        # content
        if name == "info":
            ty = y0 + strip + 20
            for line in info_lines:
                draw.text((x0 + 16, ty), line, fill=(40, 40, 40), font=f_info)
                ty += 30
        else:
            thumb = images[name].resize((cw, ch), resample)
            sheet.paste(thumb, (x0, y0 + strip))
        draw.rectangle([x0, y0, x0 + cw - 1, y0 + strip + ch - 1],
                       outline=(180, 180, 180))
    return sheet


# --------------------------------------------------------------------------- #
# Pipeline                                                                    #
# --------------------------------------------------------------------------- #
def render_cloud(cloud, label, out_dir):
    out = Path(out_dir) / label
    out.mkdir(parents=True, exist_ok=True)

    log(f"loading {cloud}")
    xyz, anchor = load_cloud(cloud)
    n_file = xyz.shape[0]
    log(f"{n_file:,} points loaded (anchor: "
        f"{anchor[0]:.3f}, {anchor[1]:.3f}, {anchor[2]:.3f})")
    xyz, sampled = subsample(xyz, PARAMS["max_points"], PARAMS["sample_seed"])
    n_render = xyz.shape[0]

    # locked colour ramp: z from the 2nd to the 98th percentile of the cloud
    z = xyz[:, 2]
    p_lo, p_hi = PARAMS["z_range_percentiles"]
    z_lo, z_hi = (float(v) for v in np.percentile(z, [p_lo, p_hi]))
    span = max(z_hi - z_lo, 1e-9)
    lut = build_lut()
    t = np.clip((z - z_lo) / span, 0.0, 1.0)
    colors = lut[np.minimum((t * LUT_SIZE).astype(np.int32), LUT_SIZE - 1)]
    del t

    # floor level and horizontal section band (1.15..1.25 m above the floor)
    floor_z = float(np.percentile(z, PARAMS["floor_percentile"]))
    band = PARAMS["section_band_above_floor_m"]
    sec_lo, sec_hi = floor_z + band[0], floor_z + band[1]
    sec_mask = (z >= sec_lo) & (z <= sec_hi)
    log(f"z colour range [{z_lo:.3f}, {z_hi:.3f}] m (local); "
        f"floor at {floor_z:.3f} m; section band [{sec_lo:.3f}, {sec_hi:.3f}] m "
        f"({int(sec_mask.sum()):,} points)")

    bases = view_bases()
    images, metas = {}, {}
    for name in VIEW_ORDER:
        if name == "section_120":
            img, meta = render_view(xyz[sec_mask], colors[sec_mask],
                                    bases[name],
                                    note_if_empty="no points in section band")
        else:
            img, meta = render_view(xyz, colors, bases[name])
        path = out / f"{name}.png"
        img.save(path)
        meta["file"] = path.name
        images[name], metas[name] = img, meta
        log(f"view {name:<11} {meta['n_points']:>12,} pts -> {path}")

    info_lines = [
        f"source: {Path(cloud).name}",
        f"points: {n_render:,} rendered / {n_file:,} in file"
        + (f" (subsampled, seed {PARAMS['sample_seed']})" if sampled else ""),
        f"canvas: {PARAMS['image_width_px']} x {PARAMS['image_height_px']} px,"
        f" white bg, point = {PARAMS['point_size_px']} px, z-buffer",
        "projection: orthographic (5 fixed views)",
        f"colour: z ramp, viridis-like LUT ({len(VIRIDIS_ANCHORS)} anchors),",
        f"  range = p{p_lo:g}..p{p_hi:g} of z"
        f" = [{z_lo + anchor[2]:.3f}, {z_hi + anchor[2]:.3f}] m",
        f"floor (p{PARAMS['floor_percentile']:g} of z): {floor_z + anchor[2]:.3f} m",
        f"section band: floor +{band[0]:g} .. +{band[1]:g} m",
    ]
    sheet = contact_sheet(images, metas, label, info_lines)
    sheet_path = out / "contact_sheet.png"
    sheet.save(sheet_path)
    log(f"contact sheet -> {sheet_path}")

    params = {
        "tool": "render_std.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"cloud": str(cloud), "label": label,
                  "format": Path(cloud).suffix.lower().lstrip("."),
                  "n_points_file": int(n_file)},
        "coordinate_anchor_subtracted": [round(float(v), 6) for v in anchor],
        "subsampling": {
            "applied": bool(sampled),
            "max_points": PARAMS["max_points"],
            "seed": PARAMS["sample_seed"],
            "method": "uniform random without replacement (display only)",
            "n_points_rendered": int(n_render),
        },
        "image": {k: PARAMS[k] for k in
                  ("image_width_px", "image_height_px", "margin_px",
                   "background_rgb", "point_size_px", "depth_test")},
        "colormap": {
            "style": "viridis-like, hand-coded (no matplotlib)",
            "n_anchors": len(VIRIDIS_ANCHORS),
            "lut_size": LUT_SIZE,
            "z_range_percentiles": PARAMS["z_range_percentiles"],
            "z_range_m_absolute": [round(z_lo + anchor[2], 4),
                                   round(z_hi + anchor[2], 4)],
        },
        "floor": {"percentile": PARAMS["floor_percentile"],
                  "elevation_m_absolute": round(floor_z + anchor[2], 4)},
        "section_120": {
            "band_above_floor_m": band,
            "z_min_m_absolute": round(sec_lo + anchor[2], 4),
            "z_max_m_absolute": round(sec_hi + anchor[2], 4),
            "n_points": int(sec_mask.sum()),
        },
        "iso_view": {"azimuth_deg": PARAMS["iso_azimuth_deg"],
                     "elevation_deg": PARAMS["iso_elevation_deg"]},
        "percentiles_computed_on": ("rendered subset" if sampled else "full cloud"),
        "views": metas,
    }
    params_path = out / "render_params.json"
    params_path.write_text(json.dumps(params, indent=2, ensure_ascii=False))
    log(f"parameters -> {params_path}")
    return params


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cloud", required=True, help="point cloud (.las/.laz/.ply)")
    p.add_argument("--label", required=True, help="scanner label (output subfolder)")
    p.add_argument("--out", default="results/renders",
                   help="output folder (default: results/renders)")
    args = p.parse_args()
    render_cloud(args.cloud, safe_label(args.label), args.out)


if __name__ == "__main__":
    main()

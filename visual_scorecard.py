#!/usr/bin/env python3
"""
visual_scorecard
================
Translate the benchmark results (comparison.csv from benchmark.py) into a
plain-language "report card" that non-experts can read: every millimetre value
is anchored to an everyday object (credit card, coin, pencil, golf ball...),
each metric gets a traffic-light dot derived from its High/Medium/Low class,
and each device gets a use-case verdict in common language.

Outputs (all PNG, >= 200 dpi, English texts for international workshops):
  * card_<label>.png   -> one A5-landscape scorecard per device
  * ruler_common.png   -> a 0-50 mm ruler with the reference objects and one
                          marker per device at its error (p95 if available,
                          otherwise the surface-noise sigma)
  * poster.png         -> all cards stacked + the common ruler (Pillow)
  * poster_16x9.png    -> the same content laid out for a 16:9 slide

Usage
-----
  python visual_scorecard.py scorecard --csv comparison.csv --out results/scorecards
  python visual_scorecard.py scorecard --csv comparison.csv --scanners scanners.json

The optional scanners JSON (same manifest used by benchmark.py batch) may carry
an indicative price per device: any of the keys "price", "price_eur", "cost".

Requires: Python 3.8+, numpy, matplotlib, pillow. No CloudCompare needed here.
"""

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # head-less rendering, consistent with the CLI pipeline
import matplotlib.pyplot as plt
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Tangible equivalents (mm): the citable constants that anchor every value     #
# --------------------------------------------------------------------------- #
# kind: "thick" (stackable flat object), "across" (diameter), "wide" (width)
TANGIBLES = [
    {"name": "a credit card",  "plural": "credit cards",  "mm": 0.76,  "kind": "thick",  "stack": True,  "short": "credit card"},
    {"name": "a 1-euro coin",  "plural": "1-euro coins",  "mm": 2.33,  "kind": "thick",  "stack": True,  "short": "1-euro coin (thickness)"},
    {"name": "a pencil",       "plural": "pencils",       "mm": 7.0,   "kind": "across", "stack": False, "short": "pencil"},
    {"name": "a 1-euro coin",  "plural": "1-euro coins",  "mm": 23.25, "kind": "across", "stack": False, "short": "1-euro coin (diameter)"},
    {"name": "a golf ball",    "plural": "golf balls",    "mm": 42.7,  "kind": "across", "stack": False, "short": "golf ball"},
    {"name": "a smartphone",   "plural": "smartphones",   "mm": 72.0,  "kind": "wide",   "stack": False, "short": "smartphone"},
]

# --------------------------------------------------------------------------- #
# Palette: colour-blind-safe status colours + neutral chart chrome (light)     #
# Status colours are paired with a written class label everywhere, so colour   #
# never carries the meaning alone (the yellow is low-contrast by design and    #
# is mitigated exactly this way).                                              #
# --------------------------------------------------------------------------- #
SURFACE = "#fcfcfb"      # card / chart surface
PAGE = "#f9f9f7"         # poster background plane
INK = "#0b0b0b"          # primary text
INK_2 = "#52514e"        # secondary text
MUTED = "#898781"        # captions, axis labels
GRIDLINE = "#e1e0d9"     # hairline separators
BASELINE = "#c3c2b7"     # axis / strong separators
GOOD_TEXT = "#006300"    # success text on light surface

STATUS = {
    "green":  "#0ca30c",  # High
    "yellow": "#fab219",  # Medium
    "red":    "#d03b3b",  # Low and Fail
    "grey":   "#898781",  # metric absent / not rated
}
DOT_EDGE = (0.043, 0.043, 0.043, 0.35)  # hairline ring around every dot

# class rank: worst rated metric sets the overall verdict (conservative:
# fitness-for-purpose is limited by the weakest requirement it must satisfy)
CLASS_RANKS = {"high": 3, "medium": 2, "low": 1, "fail": 0, "insufficient": 0}
RANK_WORDS = {3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "INSUFFICIENT"}
RANK_DOTS = {3: "green", 2: "yellow", 1: "red", 0: "red"}

# verdicts in common language (rank 0 falls back to the most cautious one)
VERDICTS = {
    3: ("detailed as-built drawings (1:50), renovation surveys",
        "sub-centimetre heritage documentation"),
    2: ("standard building documentation (1:100)",
        "detailed drawings"),
    1: ("volume estimates, quick reconnaissance",
        "measured drawings"),
}
VERDICTS[0] = VERDICTS[1]

A5_IN = (210 / 25.4, 148.5 / 25.4)   # A5 landscape, inches
MIN_DPI = 200


# --------------------------------------------------------------------------- #
# Plain-English anchoring of a millimetre value to the nearest object          #
# --------------------------------------------------------------------------- #
def _fmt_mm(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def tangible_phrase(mm):
    """Return a short English sentence anchoring `mm` to the nearest everyday
    object, e.g. 'thinner than 3 stacked credit cards (3 x 0.76 mm)'."""
    if mm is None or not math.isfinite(mm) or mm <= 0:
        return ""
    smallest = TANGIBLES[0]
    if mm <= smallest["mm"]:
        return f"thinner than {smallest['name']} ({_fmt_mm(smallest['mm'])} mm)"
    # nearest object on a log scale (sizes span two orders of magnitude)
    obj = min(TANGIBLES, key=lambda o: abs(math.log(mm / o["mm"])))
    ratio = mm / obj["mm"]
    bare = obj["name"].split(" ", 1)[1]  # "a credit card" -> "credit card"
    if 0.85 <= ratio <= 1.25:                       # "about the size of ..."
        return {
            "thick":  f"about as thick as {obj['name']} ({_fmt_mm(obj['mm'])} mm)",
            "across": f"about a {bare}'s width ({_fmt_mm(obj['mm'])} mm)",
            "wide":   f"about a {bare}'s width ({_fmt_mm(obj['mm'])} mm)",
        }[obj["kind"]]
    if ratio < 0.85:                                # comfortably smaller
        if obj["kind"] == "thick":
            return f"thinner than {obj['name']} ({_fmt_mm(obj['mm'])} mm)"
        return f"smaller than {obj['name']} ({_fmt_mm(obj['mm'])} mm across)"
    # larger than the nearest object: stack flat objects, else step up one size
    if obj["stack"]:
        n = math.ceil(ratio)
        if n <= 8:
            return f"thinner than {n} stacked {obj['plural']} ({n} x {_fmt_mm(obj['mm'])} mm)"
    bigger = [o for o in TANGIBLES if o["mm"] > mm]
    if bigger:
        nxt = bigger[0]
        if nxt["kind"] == "thick":
            return f"thinner than {nxt['name']} ({_fmt_mm(nxt['mm'])} mm)"
        return f"smaller than {nxt['name']} ({_fmt_mm(nxt['mm'])} mm across)"
    widest = TANGIBLES[-1]
    return f"about {mm / widest['mm']:.1f} smartphone widths ({_fmt_mm(widest['mm'])} mm each)"


# --------------------------------------------------------------------------- #
# comparison.csv parsing (tolerant to missing columns / older versions)        #
# --------------------------------------------------------------------------- #
# target key -> (accepted substrings, rejected substrings), case-insensitive
_COLUMN_HINTS = {
    "label":           (("scanner", "label", "device"), ()),
    "spacing_mm":      (("spacing",), ()),
    "density_class":   (("density class",), ()),
    "sigma_mm":        (("noise sigma", "sigma"), ("m3c2",)),
    "noise_class":     (("noise class",), ()),
    "p95_mm":          (("deviation p95", "p95"), ("m3c2",)),
    "deviation_class": (("deviation class",), ()),
    "overall_class":   (("overall class",), ()),
}


def _resolve_columns(fieldnames):
    """Map our keys onto whatever headers this CSV version actually has."""
    out = {}
    lowered = [(h, h.lower()) for h in fieldnames if h]
    for key, (accept, reject) in _COLUMN_HINTS.items():
        for needle in accept:
            hit = next((h for h, l in lowered
                        if needle in l and not any(r in l for r in reject)), None)
            if hit:
                out[key] = hit
                break
    return out


def _to_float(s):
    try:
        v = float(str(s).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def class_rank(cls):
    """'High' -> 3 ... None if empty / failed / unrecognised."""
    c = str(cls or "").strip().lower()
    for word, rank in CLASS_RANKS.items():
        if c.startswith(word):
            return rank
    return None


def class_dot(cls):
    r = class_rank(cls)
    return STATUS[RANK_DOTS[r]] if r is not None else STATUS["grey"]


def class_word(cls):
    r = class_rank(cls)
    return RANK_WORDS[r] if r is not None else "not rated"


def loa_tag(cls):
    m = re.search(r"\((LOA\d+)\)", str(cls or ""))
    return m.group(1) if m else ""


def load_rows(csv_path):
    """Read comparison.csv into a list of per-device dicts (missing -> None)."""
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = _resolve_columns(reader.fieldnames or [])
        if "label" not in cols:
            sys.exit(f"Could not find a scanner/label column in {csv_path} "
                     f"(headers: {reader.fieldnames})")
        rows = []
        for raw in reader:
            label = str(raw.get(cols["label"], "")).strip()
            if not label:
                continue
            get = lambda k: raw.get(cols[k], "") if k in cols else ""
            rows.append({
                "label": label,
                "spacing_mm": _to_float(get("spacing_mm")),
                "density_class": str(get("density_class")).strip(),
                "sigma_mm": _to_float(get("sigma_mm")),
                "noise_class": str(get("noise_class")).strip(),
                "p95_mm": _to_float(get("p95_mm")),
                "deviation_class": str(get("deviation_class")).strip(),
                "overall_class": str(get("overall_class")).strip(),
            })
    if not rows:
        sys.exit(f"No device rows found in {csv_path}")
    return rows


def _norm_label(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def load_prices(scanners_json):
    """Optional {label -> price string} from the benchmark manifest."""
    if not scanners_json:
        return {}
    data = json.loads(Path(scanners_json).read_text(encoding="utf-8"))
    entries = data.get("scanners", data) if isinstance(data, dict) else data
    prices = {}
    for sc in entries:
        if not isinstance(sc, dict) or "label" not in sc:
            continue
        price = next((sc[k] for k in ("price", "price_eur", "cost") if k in sc), None)
        if price is None:
            continue
        if isinstance(price, (int, float)):
            price = f"EUR {price:,.0f}"
        prices[_norm_label(sc["label"])] = str(price)
    return prices


def overall_rank(row):
    """Overall class: the explicit 'Overall class' column when the CSV has one,
    otherwise the worst rank among the rated metrics (None if nothing rated)."""
    explicit = class_rank(row.get("overall_class"))
    if explicit is not None:
        return explicit
    ranks = [r for r in (class_rank(row["density_class"]),
                         class_rank(row["noise_class"]),
                         class_rank(row["deviation_class"])) if r is not None]
    return min(ranks) if ranks else None


# --------------------------------------------------------------------------- #
# Card rendering (A5 landscape, one per device)                                #
# --------------------------------------------------------------------------- #
def _dot(ax, x, y, color, size=13):
    ax.plot([x], [y], marker="o", markersize=size, markerfacecolor=color,
            markeredgecolor=DOT_EDGE, markeredgewidth=1.1, linestyle="none",
            transform=ax.transAxes, clip_on=False, zorder=5)


def _indicator_row(ax, y0, title, caption, value_mm, cls, anchor_prefix):
    """One metric row: title/caption, big value, traffic dot + class, anchor."""
    ax.text(0.055, y0 + 0.052, title, fontsize=12, fontweight="bold", color=INK)
    ax.text(0.055, y0 + 0.014, caption, fontsize=8.2, color=MUTED)
    if value_mm is not None:
        ax.text(0.585, y0 + 0.036, f"{_fmt_mm(value_mm)} mm", fontsize=18,
                fontweight="bold", color=INK, ha="right")
        _dot(ax, 0.635, y0 + 0.052, class_dot(cls))
        ax.text(0.665, y0 + 0.052, class_word(cls), fontsize=11,
                fontweight="bold", color=INK, va="center")
        tag = loa_tag(cls)
        if tag:
            ax.text(0.665, y0 + 0.012, tag, fontsize=8, color=MUTED)
        phrase = tangible_phrase(value_mm)
        if phrase:
            ax.text(0.075, y0 - 0.046, f"{anchor_prefix} {phrase}",
                    fontsize=10, color=INK_2)
    else:
        ax.text(0.585, y0 + 0.036, "not measured", fontsize=12,
                color=MUTED, ha="right")
        _dot(ax, 0.635, y0 + 0.052, STATUS["grey"])
        ax.text(0.665, y0 + 0.052, "not rated", fontsize=11,
                color=MUTED, va="center")
        ax.text(0.075, y0 - 0.046, f"{anchor_prefix} not part of this test run",
                fontsize=10, color=MUTED)


def draw_card(row, out_path, price=None, dpi=MIN_DPI):
    """Render one device scorecard as an A5-landscape PNG."""
    fig = plt.figure(figsize=A5_IN, dpi=dpi)
    fig.patch.set_facecolor(SURFACE)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # hairline frame
    ax.add_patch(plt.Rectangle((0.012, 0.016), 0.976, 0.968, fill=False,
                               edgecolor=(0.043, 0.043, 0.043, 0.10),
                               linewidth=1.2, transform=ax.transAxes))

    # ---- header ----------------------------------------------------------- #
    ax.text(0.055, 0.925, row["label"], fontsize=21, fontweight="bold", color=INK)
    ax.text(0.055, 0.872, "Handheld LiDAR scanner scorecard", fontsize=9.5, color=MUTED)
    if price:
        ax.text(0.945, 0.925, price, fontsize=13, color=INK_2, ha="right")
        ax.text(0.945, 0.872, "indicative price", fontsize=8.5, color=MUTED, ha="right")
    ax.plot([0.055, 0.945], [0.845, 0.845], color=BASELINE, lw=1.2,
            transform=ax.transAxes)

    # ---- indicator rows --------------------------------------------------- #
    rows_spec = [
        (0.745, "Surface noise (sigma)", "random error measured on a flat wall",
         row["sigma_mm"], row["noise_class"], "Typical error:"),
        (0.585, "Point spacing", "distance between neighbouring measured points",
         row["spacing_mm"], row["density_class"], "Gap between points:"),
        (0.425, "Deviation vs reference (p95)",
         "95% of points are closer than this to the reference scan",
         row["p95_mm"], row["deviation_class"], "Worst-case error:"),
    ]
    for i, (y0, title, caption, val, cls, prefix) in enumerate(rows_spec):
        _indicator_row(ax, y0, title, caption, val, cls, prefix)
        if i < len(rows_spec) - 1:
            ax.plot([0.055, 0.945], [y0 - 0.078, y0 - 0.078], color=GRIDLINE,
                    lw=0.9, transform=ax.transAxes)

    # ---- verdict ---------------------------------------------------------- #
    ax.plot([0.055, 0.945], [0.315, 0.315], color=BASELINE, lw=1.2,
            transform=ax.transAxes)
    ax.text(0.055, 0.276, "OVERALL VERDICT", fontsize=8.5, color=MUTED)
    rank = overall_rank(row)
    if rank is not None:
        _dot(ax, 0.075, 0.228, STATUS[RANK_DOTS[rank]], size=12)
        ax.text(0.103, 0.228, f"Accuracy class: {RANK_WORDS[rank]}",
                fontsize=12.5, fontweight="bold", color=INK, va="center")
        ax.text(0.103, 0.192, "set by the weakest rated metric",
                fontsize=8, color=MUTED)
        suitable, not_for = VERDICTS[rank]
        ax.text(0.055, 0.135, "✓", fontsize=12, fontweight="bold",
                color=GOOD_TEXT)
        ax.text(0.085, 0.135, "Suitable for:", fontsize=10.5,
                fontweight="bold", color=INK)
        ax.text(0.245, 0.135, suitable, fontsize=10.5, color=INK_2)
        ax.text(0.055, 0.075, "✗", fontsize=12, fontweight="bold",
                color=STATUS["red"])
        ax.text(0.085, 0.075, "Not for:", fontsize=10.5,
                fontweight="bold", color=INK)
        ax.text(0.245, 0.075, not_for, fontsize=10.5, color=INK_2)
    else:
        _dot(ax, 0.075, 0.228, STATUS["grey"], size=12)
        ax.text(0.103, 0.228, "Not enough rated metrics for a verdict",
                fontsize=12, color=INK_2, va="center")

    ax.text(0.055, 0.030, "Class bounds: USIBD LOA (deviation), "
            "Historic England 2024 (spacing, noise)",
            fontsize=7.2, color=MUTED)

    fig.savefig(out_path, dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Common ruler (0-50 mm): objects above the scale, devices below               #
# --------------------------------------------------------------------------- #
def draw_ruler(rows, out_path, dpi=MIN_DPI):
    """0-50 mm ruler: reference objects above, one marker per device below
    (p95 deviation if available, otherwise the noise sigma)."""
    fig = plt.figure(figsize=(12.5, 4.4), dpi=dpi)
    fig.patch.set_facecolor(SURFACE)
    ax = fig.add_axes([0.03, 0.02, 0.94, 0.80])
    ax.set_axis_off()
    ax.set_xlim(-0.8, 50.8)
    ax.set_ylim(-3.10, 2.35)

    fig.text(0.03, 0.945, "How big is each scanner's error?",
             fontsize=13.5, fontweight="bold", color=INK)
    fig.text(0.03, 0.885, "Everyday objects above the scale, scanners below "
             "(95th-percentile deviation where available, otherwise noise sigma). "
             "Scale in millimetres.", fontsize=9.5, color=INK_2)

    # ---- scale ------------------------------------------------------------ #
    ax.plot([0, 50], [0, 0], color=BASELINE, lw=1.6, zorder=2)
    for mm in range(0, 51):
        h = 0.22 if mm % 10 == 0 else (0.15 if mm % 5 == 0 else 0.08)
        ax.plot([mm, mm], [0, -h], color=BASELINE, lw=1.0 if mm % 5 == 0 else 0.6)
    for mm in range(0, 51, 10):
        ax.text(mm, -0.42, str(mm), fontsize=9, color=MUTED, ha="center")
    ax.text(50.6, -0.42, "mm", fontsize=9, color=MUTED, ha="left")

    # ---- reference objects (above) ----------------------------------------- #
    heights = [0.62, 1.22, 1.82]
    idx = 0
    for obj in TANGIBLES:
        if obj["mm"] > 50:
            continue
        x, h = obj["mm"], heights[idx % 3]
        idx += 1
        ax.plot([x, x], [0.06, h - 0.12], color=MUTED, lw=1.0, zorder=3)
        ax.plot([x], [h - 0.12], marker="o", markersize=4, color=MUTED,
                linestyle="none")
        ha = "left" if x < 4 else ("right" if x > 46 else "center")
        ax.text(x if ha != "left" else x - 0.3, h + 0.02,
                f"{obj['short']}  {_fmt_mm(obj['mm'])} mm",
                fontsize=9, color=INK_2, ha=ha)

    # ---- device markers (below) -------------------------------------------- #
    devices = []
    for row in rows:
        val, metric, cls = row["p95_mm"], "p95", row["deviation_class"]
        if val is None:
            val, metric, cls = row["sigma_mm"], "sigma", row["noise_class"]
        if val is None:
            continue
        devices.append((val, metric, cls, row["label"]))
    devices.sort(key=lambda d: d[0])

    label_rows = [-1.18, -1.95, -2.72]
    last_x, level = -99.0, 0
    for val, metric, cls, label in devices:
        x = min(val, 49.6)
        clipped = val > 50
        level = (level + 1) % 3 if (x - last_x) < 8.0 else 0
        last_x = x
        y_lab = label_rows[level]
        color = class_dot(cls)
        ax.plot([x], [-0.62], marker="^", markersize=12, markerfacecolor=color,
                markeredgecolor=DOT_EDGE, markeredgewidth=1.1,
                linestyle="none", zorder=5)
        ax.plot([x, x], [-0.74, y_lab + 0.16], color=GRIDLINE, lw=1.0, zorder=3)
        ha = "left" if x < 4 else ("right" if x > 46 else "center")
        arrow = "  → off scale" if clipped else ""
        ax.text(x, y_lab, label, fontsize=9.5, fontweight="bold",
                color=INK, ha=ha)
        ax.text(x, y_lab - 0.30,
                f"{_fmt_mm(val)} mm ({metric}) · {class_word(cls)}{arrow}",
                fontsize=8.5, color=INK_2, ha=ha)

    fig.text(0.97, 0.02, "green = HIGH · yellow = MEDIUM · "
             "red = LOW / FAIL · grey = not rated",
             fontsize=8, color=MUTED, ha="right")

    fig.savefig(out_path, dpi=dpi, facecolor=SURFACE)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Poster composition (Pillow)                                                  #
# --------------------------------------------------------------------------- #
POSTER_TITLE = "Handheld LiDAR scanners: a plain-language scorecard"
POSTER_SUB = "Benchmark results anchored to everyday objects"


def _font(size, bold=False):
    names = ("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:                      # very old Pillow: fixed-size fallback
        return ImageFont.load_default()


def _paste_scaled(canvas, img, box_x, box_y, box_w, box_h):
    """Fit `img` into the given box (downscale only, keep aspect, centre)."""
    scale = min(box_w / img.width, box_h / img.height, 1.0)
    w, h = int(img.width * scale), int(img.height * scale)
    img = img.resize((w, h), Image.LANCZOS)
    canvas.paste(img, (box_x + (box_w - w) // 2, box_y + (box_h - h) // 2))
    return h


def compose_poster(card_paths, ruler_path, out_path, dpi=MIN_DPI):
    """Stack all cards vertically, with the common ruler at the bottom."""
    cards = [Image.open(p).convert("RGB") for p in card_paths]
    ruler = Image.open(ruler_path).convert("RGB")
    content_w = max(img.width for img in cards)
    margin, gap, title_h = 48, 30, 150
    ruler_h = int(ruler.height * min(content_w / ruler.width, 1.0))
    height = (title_h + margin + sum(c.height for c in cards)
              + gap * len(cards) + ruler_h + margin)
    canvas = Image.new("RGB", (content_w + 2 * margin, height), PAGE)
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 42), POSTER_TITLE, font=_font(52, bold=True), fill=INK)
    draw.text((margin, 108), POSTER_SUB, font=_font(30), fill=INK_2)
    y = title_h + margin
    for c in cards:
        canvas.paste(c, (margin + (content_w - c.width) // 2, y))
        y += c.height + gap
    _paste_scaled(canvas, ruler, margin, y, content_w, ruler_h)
    canvas.save(out_path, dpi=(dpi, dpi))


def compose_poster_16x9(card_paths, ruler_path, out_path, dpi=MIN_DPI,
                        size=(3200, 1800)):
    """Slide version: cards in a grid on top, the common ruler across the bottom."""
    cards = [Image.open(p).convert("RGB") for p in card_paths]
    ruler = Image.open(ruler_path).convert("RGB")
    W, H = size
    margin, gap, title_h = 44, 28, 140
    canvas = Image.new("RGB", (W, H), PAGE)
    draw = ImageDraw.Draw(canvas)
    draw.text((margin, 34), POSTER_TITLE, font=_font(48, bold=True), fill=INK)
    draw.text((margin, 96), POSTER_SUB, font=_font(27), fill=INK_2)

    ruler_box_h = int(H * 0.32)
    cards_top = title_h + gap
    cards_h = H - cards_top - ruler_box_h - 2 * gap
    n = len(cards)
    cols = 1 if n == 1 else (2 if n in (2, 4) else 3)
    rows_n = math.ceil(n / cols)
    cell_w = (W - margin * 2 - gap * (cols - 1)) // cols
    cell_h = (cards_h - gap * (rows_n - 1)) // rows_n
    for i, c in enumerate(cards):
        r, k = divmod(i, cols)
        x = margin + k * (cell_w + gap)
        y = cards_top + r * (cell_h + gap)
        _paste_scaled(canvas, c, x, y, cell_w, cell_h)
    _paste_scaled(canvas, ruler, margin, H - ruler_box_h - gap,
                  W - 2 * margin, ruler_box_h)
    canvas.save(out_path, dpi=(dpi, dpi))


# --------------------------------------------------------------------------- #
# Pipeline                                                                     #
# --------------------------------------------------------------------------- #
def safe_label(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s).strip()).strip("_") or "scan"


def build_scorecards(csv_path, out_dir, scanners_json=None, dpi=MIN_DPI):
    """Generate all cards, the common ruler and both posters. Returns paths."""
    dpi = max(int(dpi), MIN_DPI)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(csv_path)
    prices = load_prices(scanners_json)

    card_paths = []
    for row in rows:
        path = out_dir / f"card_{safe_label(row['label'])}.png"
        draw_card(row, path, price=prices.get(_norm_label(row["label"])), dpi=dpi)
        card_paths.append(path)
        print(f"-> {path}")

    ruler_path = out_dir / "ruler_common.png"
    draw_ruler(rows, ruler_path, dpi=dpi)
    print(f"-> {ruler_path}")

    poster_path = out_dir / "poster.png"
    compose_poster(card_paths, ruler_path, poster_path, dpi=dpi)
    print(f"-> {poster_path}")

    poster16_path = out_dir / "poster_16x9.png"
    compose_poster_16x9(card_paths, ruler_path, poster16_path, dpi=dpi)
    print(f"-> {poster16_path}")
    return card_paths + [ruler_path, poster_path, poster16_path]


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scorecard", help="render cards, ruler and posters from comparison.csv")
    s.add_argument("--csv", required=True, help="comparison.csv from 'benchmark.py report'")
    s.add_argument("--out", default="results/scorecards", help="output directory")
    s.add_argument("--scanners", default=None,
                   help="optional scanners JSON manifest carrying per-device prices")
    s.add_argument("--dpi", type=int, default=MIN_DPI, help="raster resolution (min 200)")

    args = p.parse_args()
    if args.cmd == "scorecard":
        build_scorecards(args.csv, args.out, scanners_json=args.scanners, dpi=args.dpi)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
LiDAR Scanner Benchmark - desktop app (no terminal, no paths to type).

Add a scan -> the app computes density and surface noise automatically (it finds
a flat wall on its own; no manual segmentation). Mark the best instrument as the
reference and compute the geometric deviation of every other scanner against it.
Then save the comparison table.

Run by double-clicking "LiDAR Benchmark.bat", or:  python gui.py
"""

import json
import os
import threading
from pathlib import Path

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import benchmark as bm

BASE = Path(__file__).resolve().parent
RESULTS = BASE / "results"
RESULTS.mkdir(exist_ok=True)

CLOUD_TYPES = [("Point clouds", "*.las *.laz *.e57 *.ply *.pcd *.pts *.xyz"),
               ("All files", "*.*")]
COLS = [("scanner", "Scanner", 140), ("points", "Points", 75),
        ("spacing", "Spacing (mm)", 85), ("dclass", "Density class", 100),
        ("sigma", "Noise σ (mm)", 85), ("nclass", "Noise class", 100),
        ("acq", "Acq. (min)", 75),
        ("devp95", "Deviation p95 (mm)", 120), ("devclass", "Deviation class", 105)]


class App:
    def __init__(self, root):
        self.root = root
        self.params = dict(bm.DEFAULTS)
        self.busy = False
        self.items = {}        # label -> tree item id
        self.clouds = {}       # label -> original cloud path
        self.sigma = {}        # label -> planar noise sigma (mm), to auto-pick the reference
        self.reference = None  # label of the reference scanner
        self.auto_dev = tk.BooleanVar(value=True)  # auto-compute deviation when 2+ scans
        root.title("LiDAR Scanner Benchmark")
        root.geometry("1080x480")
        root.minsize(940, 400)

        self.cc_exe = self._locate_cloudcompare()

        top = ttk.Frame(root, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="LiDAR Scanner Benchmark",
                  font=("Segoe UI", 14, "bold")).pack(side="left")
        self.add_btn = ttk.Button(top, text="➕  Add scan…", command=self.on_add)
        self.add_btn.pack(side="right")
        self.rm_btn = ttk.Button(top, text="🗑  Remove", command=self.on_remove)
        self.rm_btn.pack(side="right", padx=6)

        mid = ttk.Frame(root, padding=(12, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in COLS], show="headings",
                                 height=10, selectmode="extended")
        for key, head, w in COLS:
            self.tree.heading(key, text=head)
            self.tree.column(key, width=w, anchor="center")
        self.tree.column("scanner", anchor="w")
        self.tree.tag_configure("ref", background="#fff3cd")  # highlight the reference row
        self.tree.bind("<Double-1>", self._on_edit)           # double-click 'Acq. (min)' to edit
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        bottom = ttk.Frame(root, padding=12)
        bottom.pack(fill="x")
        self.ref_btn = ttk.Button(bottom, text="★ Set reference", command=self.on_set_reference)
        self.ref_btn.pack(side="left")
        self.dev_btn = ttk.Button(bottom, text="Deviation vs ref", command=self.on_deviation)
        self.dev_btn.pack(side="left", padx=6)
        self.adev_btn = ttk.Button(bottom, text="Deviation (assisted)…", command=self.on_deviation_assisted)
        self.adev_btn.pack(side="left")
        self.auto_chk = ttk.Checkbutton(bottom, text="Auto", variable=self.auto_dev)
        self.auto_chk.pack(side="left", padx=6)
        ttk.Button(bottom, text="Open table", command=self.on_open).pack(side="right")
        ttk.Button(bottom, text="Save table", command=self.on_report).pack(side="right", padx=6)
        self.status = ttk.Label(bottom, text="Ready. Add a scan to begin.")
        self.status.pack(side="left", padx=14)

        self._load_existing()
        self.root.after(600, self._maybe_auto_deviation)  # auto-run on launch if 2+ scans pending

    # -- setup helpers ----------------------------------------------------- #
    def _locate_cloudcompare(self):
        try:
            return bm.find_cloudcompare()
        except SystemExit:
            messagebox.showinfo("Locate CloudCompare",
                                "CloudCompare was not found automatically.\nPlease select CloudCompare.exe.")
            p = filedialog.askopenfilename(title="Select CloudCompare executable",
                                           filetypes=[("CloudCompare", "*.exe *"), ("All", "*.*")])
            if not p:
                messagebox.showerror("CloudCompare required", "Cannot run without CloudCompare. Closing.")
                self.root.destroy()
                raise SystemExit
            return p

    def _load_existing(self):
        """Show results already on disk; fill deviation cells where present."""
        devs = {}
        for f in sorted(RESULTS.glob("*.json")):
            if f.stem.endswith("_deviation"):
                try:
                    d = json.loads(f.read_text()); devs[d["label"]] = d
                except Exception:
                    pass
                continue
            if f.stem.endswith(("_plane", "_m3c2")):
                continue
            try:
                self._add_row(json.loads(f.read_text()))
            except Exception:
                pass
        for lab, d in devs.items():
            if lab in self.items:
                self.tree.set(self.items[lab], "devp95", d.get("deviation_p95abs_mm", ""))
                self.tree.set(self.items[lab], "devclass", d.get("deviation_class", ""))

    # -- actions: scans ---------------------------------------------------- #
    def on_add(self):
        if self.busy:
            return
        files = filedialog.askopenfilenames(title="Select one or more scans", filetypes=CLOUD_TYPES)
        if files:
            self._set_busy(True)
            threading.Thread(target=self._process, args=(list(files),), daemon=True).start()

    def _process(self, files):
        ok, fails = 0, []
        for f in files:
            name = Path(f).name
            if Path(f).is_dir():
                fails.append((name, "this is a folder, not a file — open it and pick the .las inside")); continue
            if f.lower().endswith(".trajectory.las"):
                fails.append((name, "trajectory file (the scanner path), not the point cloud")); continue
            label = bm.safe_label(Path(f).stem)
            self._post(lambda l=label: self.status.config(text=f"Processing  {l}  …  (~30 s)"))
            try:
                res = bm.analyze_scan(self.cc_exe, f, label, self.params, RESULTS)
                (RESULTS / f"{label}.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
                self._post(lambda r=res: self._add_row(r)); ok += 1
            except Exception as e:
                fails.append((name, str(e).splitlines()[-1][:200] if str(e) else "unknown error"))
        self._post(lambda: self.status.config(text=f"Done.  {ok} added, {len(fails)} skipped."))
        if fails:
            msg = "These files were skipped:\n\n" + "\n".join(f"•  {n}\n    → {why}" for n, why in fails)
            self._post(lambda m=msg: messagebox.showwarning("Some files were skipped", m))
        self._post(self._after_adds)

    def _after_adds(self):
        self._set_busy(False)
        self._maybe_auto_deviation()

    # -- actions: geometric deviation -------------------------------------- #
    def _set_reference_label(self, label):
        """Mark `label` as the reference row (used by manual button and auto-pick)."""
        self.reference = label
        for it in self.tree.get_children():
            self.tree.item(it, tags=())
        if label in self.items:
            self.tree.item(self.items[label], tags=("ref",))

    def on_set_reference(self):
        if self.busy:
            return
        sel = self.tree.selection()
        if len(sel) != 1:
            self.status.config(text="Select exactly one row, then Set reference."); return
        label = self.tree.set(sel[0], "scanner")
        if not self.clouds.get(label):
            self.status.config(text=f"No source file stored for {label}; re-add it to use as reference."); return
        self._set_reference_label(label)
        self.status.config(text=f"Reference: {label}.")
        self._maybe_auto_deviation()

    def _maybe_auto_deviation(self):
        """When 2+ scans are present, auto-pick the reference (lowest noise σ = best
        instrument) and compute the deviation of every other scan against it."""
        if self.busy or not self.auto_dev.get() or len(self.items) < 2:
            return
        if self.reference is None or not self.clouds.get(self.reference):
            cand = [(s, l) for l, s in self.sigma.items()
                    if s is not None and self.clouds.get(l)]
            if not cand:
                return
            self._set_reference_label(min(cand)[1])
        ref_cloud = self.clouds.get(self.reference)
        if not ref_cloud or not Path(ref_cloud).exists():
            return
        targets = [(l, self.items[l], self.clouds.get(l), None)
                   for l in self.items if l != self.reference]
        targets = [t for t in targets
                   if t[2] and Path(t[2]).exists() and not self.tree.set(t[1], "devp95")]
        if not targets:
            return
        self.status.config(text=f"Auto-deviation: reference = {self.reference} (lowest σ)…")
        self._set_busy(True)
        threading.Thread(target=self._process_dev, args=(ref_cloud, targets), daemon=True).start()

    def on_deviation(self):
        if self.busy:
            return
        if not self.reference:
            messagebox.showinfo("No reference",
                                "Select a scanner (e.g. the BLK2GO) and click 'Set reference' first."); return
        ref_cloud = self.clouds.get(self.reference)
        if not ref_cloud or not Path(ref_cloud).exists():
            messagebox.showwarning("Reference file missing", "The reference scan's original file was not found."); return
        targets = [(lab, self.items[lab], self.clouds.get(lab), None)
                   for lab in self.items if lab != self.reference]
        targets = [t for t in targets if t[2] and Path(t[2]).exists()]
        if not targets:
            messagebox.showinfo("Nothing to compare", "No other scans with an available source file."); return
        self._set_busy(True)
        threading.Thread(target=self._process_dev, args=(ref_cloud, targets), daemon=True).start()

    def on_deviation_assisted(self):
        """Deviation for one selected scan, seeded by a CloudCompare point-pair matrix."""
        if self.busy:
            return
        if not self.reference:
            messagebox.showinfo("No reference", "Set a reference first."); return
        ref_cloud = self.clouds.get(self.reference)
        if not ref_cloud or not Path(ref_cloud).exists():
            messagebox.showwarning("Reference file missing", "The reference scan's file was not found."); return
        sel = self.tree.selection()
        if len(sel) != 1:
            self.status.config(text="Select exactly one (non-reference) row, then Deviation (assisted)."); return
        lab = self.tree.set(sel[0], "scanner")
        if lab == self.reference:
            self.status.config(text="That row is the reference; select a different scan."); return
        cloud = self.clouds.get(lab)
        if not cloud or not Path(cloud).exists():
            messagebox.showwarning("File missing", f"Source file for {lab} not found."); return
        mfile = filedialog.askopenfilename(
            title=f"Pre-alignment matrix for {lab} (CloudCompare point-pair → 4×4 .txt)",
            filetypes=[("Matrix", "*.txt *.asc *.mat"), ("All", "*.*")])
        if not mfile:
            return
        try:
            init = np.loadtxt(mfile)
            if init.shape != (4, 4):
                messagebox.showwarning("Bad matrix", f"Expected a 4×4 matrix, got shape {init.shape}."); return
        except Exception as e:
            messagebox.showwarning("Cannot read matrix", str(e)); return
        self._set_busy(True)
        threading.Thread(target=self._process_dev,
                         args=(ref_cloud, [(lab, sel[0], cloud, init)]), daemon=True).start()

    def _process_dev(self, ref_cloud, targets):
        done, failed_reg, fails = 0, 0, []
        for lab, item, cloud, init in targets:
            mode = "assisted" if init is not None else "auto"
            self._post(lambda l=lab, mo=mode: self.status.config(text=f"Deviation ({mo})  {l}  vs reference … (~3 min)"))
            try:
                res = bm.analyze_deviation(self.cc_exe, ref_cloud, cloud, lab, self.params, RESULTS, init=init)
                (RESULTS / f"{lab}_deviation.json").write_text(json.dumps(res, indent=2, ensure_ascii=False))
                self._post(lambda it=item, r=res: (self.tree.set(it, "devp95", r["deviation_p95abs_mm"]),
                                                   self.tree.set(it, "devclass", r["deviation_class"])))
                if res.get("registration_ok"):
                    done += 1
                else:
                    failed_reg += 1
            except Exception as e:
                fails.append((lab, str(e).splitlines()[-1][:200] if str(e) else "error"))
        self._post(lambda: self.status.config(
            text=f"Deviation done.  {done} ok, {failed_reg} bad registration, {len(fails)} error."))
        if failed_reg:
            self._post(lambda: messagebox.showwarning(
                "Registration failed",
                "Some scans could not be registered automatically (low overlap / different frames).\n\n"
                "Use 'Deviation (assisted)…': in CloudCompare align the scan to the reference with "
                "point-pair picking, save the 4×4 matrix, and select it."))
        if fails:
            msg = "\n".join(f"•  {n}: {why}" for n, why in fails)
            self._post(lambda m=msg: messagebox.showwarning("Deviation: some errored", m))
        self._post(lambda: self._set_busy(False))

    # -- actions: remove / report / open ----------------------------------- #
    def on_remove(self):
        if self.busy:
            return
        sel = self.tree.selection()
        if not sel:
            self.status.config(text="Select a row first, then Remove."); return
        for item in sel:
            label = self.tree.set(item, "scanner")
            for name in (f"{label}.json", f"{label}_plane.json", f"{label}_m3c2.json",
                         f"{label}_deviation.json", f"{label}_metrics.asc", f"{label}_scan.log",
                         f"{label}_wall.asc", f"{label}_plane.log", f"{label}_m3c2.asc",
                         f"{label}_m3c2_ref.asc", f"{label}_m3c2_cmp.asc", f"{label}_m3c2.log",
                         f"{label}_m3c2_params.txt", f"{label}_aligned.bin",
                         f"{label}_align.txt", f"{label}_align.log"):
                try:
                    (RESULTS / name).unlink()
                except OSError:
                    pass
            self.items.pop(label, None)
            self.clouds.pop(label, None)
            self.sigma.pop(label, None)
            if self.reference == label:
                self.reference = None
            self.tree.delete(item)
        self.status.config(text="Removed. Save the table to refresh it.")

    def on_report(self):
        try:
            bm.build_report(RESULTS, BASE)
            messagebox.showinfo("Saved", f"Table saved:\n{BASE / 'comparison.md'}\n{BASE / 'comparison.csv'}")
        except SystemExit:
            messagebox.showwarning("Nothing to save", "Add at least one scan first.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_open(self):
        md = BASE / "comparison.md"
        if not md.exists():
            self.on_report()
        if md.exists():
            try:
                os.startfile(md)
            except AttributeError:
                import subprocess, sys
                subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(md)])

    # -- ui plumbing ------------------------------------------------------- #
    def _add_row(self, r):
        label = r.get("label", "")
        vals = (label, r.get("n_points", ""), r.get("spacing_mm", ""), r.get("density_class", ""),
                r.get("sigma_planar_mm", ""), r.get("noise_class", ""), r.get("acq_duration_min") or "",
                r.get("deviation_p95abs_mm", ""), r.get("deviation_class", ""))
        if label in self.items:
            self.tree.item(self.items[label], values=vals)
        else:
            self.items[label] = self.tree.insert("", "end", values=vals)
        if r.get("cloud"):
            self.clouds[label] = r["cloud"]
        try:
            self.sigma[label] = float(r["sigma_planar_mm"])
        except (KeyError, TypeError, ValueError):
            pass

    def _on_edit(self, event):
        """Double-click the 'Acq. (min)' cell to type acquisition time manually
        (fallback for devices without GPS-time, e.g. iPhone / Raven)."""
        if self.busy:
            return
        col = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item or not col:
            return
        idx = int(col[1:]) - 1
        if idx < 0 or idx >= len(COLS) or COLS[idx][0] != "acq":
            return
        x, y, w, h = self.tree.bbox(item, col)
        entry = ttk.Entry(self.tree)
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, self.tree.set(item, "acq"))
        entry.focus_set()

        def commit(_=None):
            v = entry.get().strip()
            entry.destroy()
            self.tree.set(item, "acq", v)
            jf = RESULTS / f"{self.tree.set(item, 'scanner')}.json"
            if jf.exists():
                try:
                    d = json.loads(jf.read_text())
                    d["acq_duration_min"] = float(v) if v else None
                    jf.write_text(json.dumps(d, indent=2, ensure_ascii=False))
                except (ValueError, OSError):
                    pass

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: entry.destroy())

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.add_btn, self.rm_btn, self.ref_btn, self.dev_btn, self.adev_btn, self.auto_chk):
            b.config(state=state)

    def _post(self, fn):
        self.root.after(0, fn)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()

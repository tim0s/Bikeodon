"""
Interactive Watopia map alignment tool.

Run:  python3 tools/watopia_align.py
      python3 tools/watopia_align.py --route '%Volcano%'

Use the +/− buttons (or type a value and press Enter) to nudge each bound
until the coloured GPS tracks line up with the roads on the map.
"""
import json, sqlite3, argparse
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageDraw, ImageFont, ImageTk

DB    = "bikeodon.db"
MAP   = "/Users/timos/watopia.png"
SCALE = 0.15          # display scale: 8192 * 0.15 ≈ 1229 px wide
COLORS = ["#FF4400", "#00AAFF", "#00FF88", "#FFCC00", "#FF00AA", "#AA00FF", "#00FFFF"]


class AlignApp:
    def __init__(self, root, initial_route=""):
        self.root = root
        root.title("Watopia Alignment")

        self.vars = {
            "lat_max": tk.DoubleVar(value=-11.62593),
            "lat_min": tk.DoubleVar(value=-11.74086),
            "lon_min": tk.DoubleVar(value=166.87745),
            "lon_max": tk.DoubleVar(value=167.03256),
        }
        self.step    = tk.DoubleVar(value=0.002)
        self.route_q = tk.StringVar(value=initial_route)

        self.display_scale = SCALE
        self._base_step    = 0.002   # step at default zoom level
        self.map_img = Image.open(MAP).convert("RGBA")
        self.W, self.H = self.map_img.size

        self._load_routes()
        self._build_ui()
        self.render()

    @property
    def _route_center(self):
        """Mean lat/lon of all loaded GPS points — used as zoom pivot."""
        lats = [p[0] for _, pts in self.routes for p in pts if p[0] is not None]
        lons = [p[1] for _, pts in self.routes for p in pts if p[1] is not None]
        return (sum(lats) / len(lats), sum(lons) / len(lons)) if lats else (-11.706, 166.97)

    # ------------------------------------------------------------------

    def _load_routes(self):
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT name, points_json FROM activities"
            " WHERE sport_type='VirtualRide' AND points_json IS NOT NULL"
        ).fetchall()
        conn.close()

        # Keep only routes whose mean position is within or close to Watopia bounds
        LAT_MIN, LAT_MAX = -11.76, -11.61
        LON_MIN, LON_MAX = 166.86, 167.05
        self.routes = []
        for name, pj in rows:
            pts = json.loads(pj)
            valid = [p for p in pts if p[0] is not None and p[1] is not None]
            if not valid:
                continue
            clat = sum(p[0] for p in valid) / len(valid)
            clon = sum(p[1] for p in valid) / len(valid)
            if LAT_MIN <= clat <= LAT_MAX and LON_MIN <= clon <= LON_MAX:
                self.routes.append((name, pts))

    # ------------------------------------------------------------------

    # Fixed viewport size for the scrollable map area
    VIEW_W = 1100
    VIEW_H = 820

    def _build_ui(self):
        # Scrollable map viewport
        view_frame = ttk.Frame(self.root)
        view_frame.grid(row=0, column=0, rowspan=40, padx=4, pady=4, sticky="nsew")

        self.canvas = tk.Canvas(view_frame, width=self.VIEW_W, height=self.VIEW_H,
                                bg="#111", scrollregion=(0, 0, self.VIEW_W, self.VIEW_H))
        hbar = ttk.Scrollbar(view_frame, orient="horizontal", command=self.canvas.xview)
        vbar = ttk.Scrollbar(view_frame, orient="vertical",   command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=hbar.set, yscrollcommand=vbar.set)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")

        ctrl = ttk.Frame(self.root, padding=12)
        ctrl.grid(row=0, column=1, sticky="n")

        r = 0

        def add_field(label, key):
            nonlocal r
            ttk.Label(ctrl, text=label).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)
            )
            r += 1
            v = self.vars[key]
            e = ttk.Entry(ctrl, textvariable=v, width=10)
            e.grid(row=r, column=0, padx=(0, 2))
            e.bind("<Return>", lambda _: self.render())
            ttk.Button(
                ctrl, text="−", width=3,
                command=lambda v=v: (v.set(round(v.get() - self.step.get(), 6)), self.render()),
            ).grid(row=r, column=1)
            ttk.Button(
                ctrl, text="+", width=3,
                command=lambda v=v: (v.set(round(v.get() + self.step.get(), 6)), self.render()),
            ).grid(row=r, column=2)
            r += 1

        add_field("Latitude max  (N edge ↑)", "lat_max")
        add_field("Latitude min  (S edge ↓)", "lat_min")
        add_field("Longitude min (W edge ←)", "lon_min")
        add_field("Longitude max (E edge →)", "lon_max")

        # Zoom controls — stretch/shrink range, keeping route centre pinned
        def zoom(axis, factor):
            if axis == "x":
                lo, hi  = self.vars["lon_min"], self.vars["lon_max"]
                pivot   = self._route_center[1]
            else:
                lo, hi  = self.vars["lat_min"], self.vars["lat_max"]
                pivot   = self._route_center[0]
            frac      = (pivot - lo.get()) / (hi.get() - lo.get())
            new_range = (hi.get() - lo.get()) * factor
            lo.set(round(pivot - frac * new_range, 6))
            hi.set(round(pivot + (1 - frac) * new_range, 6))
            self.render()

        ttk.Label(ctrl, text="Zoom X (stretch horizontal)").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        r += 1
        ttk.Button(ctrl, text="− wider",  width=8, command=lambda: zoom("x", 1.02)).grid(row=r, column=0)
        ttk.Button(ctrl, text="+ narrower", width=8, command=lambda: zoom("x", 0.98)).grid(row=r, column=1, columnspan=2)
        r += 1

        ttk.Label(ctrl, text="Zoom Y (stretch vertical)").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        r += 1
        ttk.Button(ctrl, text="− taller",  width=8, command=lambda: zoom("y", 1.02)).grid(row=r, column=0)
        ttk.Button(ctrl, text="+ shorter", width=8, command=lambda: zoom("y", 0.98)).grid(row=r, column=1, columnspan=2)
        r += 1

        ttk.Label(ctrl, text="View zoom").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        r += 1

        def view_zoom(factor):
            self.display_scale = max(0.05, min(1.0, self.display_scale * factor))
            # Keep pixels-per-step constant: finer step when zoomed in
            self.step.set(round(self._base_step * SCALE / self.display_scale, 6))
            self.render()

        ttk.Button(ctrl, text="− out", width=8, command=lambda: view_zoom(0.8)).grid(row=r, column=0)
        ttk.Button(ctrl, text="+ in",  width=8, command=lambda: view_zoom(1.25)).grid(row=r, column=1, columnspan=2)
        r += 1

        ttk.Separator(ctrl, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=10
        )
        r += 1

        ttk.Label(ctrl, text="Step").grid(row=r, column=0, sticky="w")
        ttk.Entry(ctrl, textvariable=self.step, width=8).grid(
            row=r, column=1, columnspan=2, sticky="w"
        )
        r += 1
        for i, (lbl, val) in enumerate([
            ("0.001", 0.001), ("0.002", 0.002), ("0.005", 0.005), ("0.01", 0.01)
        ]):
            ttk.Button(
                ctrl, text=lbl, width=6,
                command=lambda v=val: self.step.set(v),
            ).grid(row=r + i // 2, column=i % 2, padx=1, pady=1)
        r += 2

        ttk.Separator(ctrl, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=10
        )
        r += 1

        ttk.Label(ctrl, text="Route filter (blank = all)").grid(
            row=r, column=0, columnspan=3, sticky="w"
        )
        r += 1
        e2 = ttk.Entry(ctrl, textvariable=self.route_q, width=16)
        e2.grid(row=r, column=0, columnspan=2)
        e2.bind("<Return>", lambda _: self.render())
        ttk.Button(ctrl, text="Go", command=self.render).grid(row=r, column=2)
        r += 1

        ttk.Separator(ctrl, orient="horizontal").grid(
            row=r, column=0, columnspan=3, sticky="ew", pady=10
        )
        r += 1

        self.bounds_lbl = ttk.Label(ctrl, text="", font=("Courier", 9), justify="left")
        self.bounds_lbl.grid(row=r, column=0, columnspan=3, sticky="w")

    # ------------------------------------------------------------------

    def render(self):
        lon_min = self.vars["lon_min"].get()
        lon_max = self.vars["lon_max"].get()
        lat_min = self.vars["lat_min"].get()
        lat_max = self.vars["lat_max"].get()
        W, H = self.W, self.H

        def to_px(lat, lon):
            x = (lon - lon_min) / (lon_max - lon_min) * W
            y = (lat_max - lat) / (lat_max - lat_min) * H
            return int(x), int(y)

        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        q = self.route_q.get().strip().lower()
        shown = 0
        for i, (name, pts) in enumerate(self.routes):
            if q and q not in name.lower():
                continue
            pxs = [to_px(p[0], p[1]) for p in pts if p[0] is not None and p[1] is not None]
            if len(pxs) > 1:
                draw.line(pxs, fill=COLORS[i % len(COLORS)], width=16)
            shown += 1

        out = Image.alpha_composite(self.map_img, overlay)
        d = ImageDraw.Draw(out)

        try:
            fnt = ImageFont.load_default(size=140)
        except TypeError:
            fnt = ImageFont.load_default()

        kw = dict(font=fnt, fill="white", stroke_width=5, stroke_fill="black")
        pad = 80
        d.text((W // 2, pad),     "▲ N  (lat max)", anchor="mt", **kw)
        d.text((W // 2, H - pad), "▼ S  (lat min)", anchor="mb", **kw)
        d.text((pad, H // 2),     "◀ W\n(lon min)", anchor="lm", **kw)
        d.text((W - pad, H // 2), "E ▶\n(lon max)", anchor="rm", **kw)

        dw = int(self.W * self.display_scale)
        dh = int(self.H * self.display_scale)
        self.canvas.configure(scrollregion=(0, 0, dw, dh))
        display = out.resize((dw, dh), Image.LANCZOS).convert("RGB")
        self.tk_img = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_img)

        self.bounds_lbl.config(
            text=(
                f"lat  {lat_min:.5f} – {lat_max:.5f}\n"
                f"lon  {lon_min:.5f} – {lon_max:.5f}\n"
                f"({shown} route(s) shown)"
            )
        )


# ----------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", default="", help="Route name filter")
    args = ap.parse_args()

    root = tk.Tk()
    root.resizable(False, False)
    AlignApp(root, initial_route=args.route)
    root.mainloop()

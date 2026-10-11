#!/usr/bin/env python3
"""
Museum Signs - 80 x 50 mm exhibit labels printed on A4.

Copyright (c) 2026 Manfred Amann - MIT License (see LICENSE).

  Double-click "Museum Signs.command"  -> opens the editor window.
  python museum_signs.py --pdf         -> builds the PDF from signs.csv without the window.

Data lives in signs.csv (also editable in Numbers/Excel).
Logos and the years they were introduced live in logos/logos.csv.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, asdict

from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

HERE = os.path.dirname(os.path.abspath(__file__))
SIGNS_CSV = os.path.join(HERE, "signs.csv")
EXAMPLE_CSV = os.path.join(HERE, "signs.example.csv")   # used when signs.csv does not exist yet
SETTINGS_JSON = os.path.join(HERE, ".settings.json")
LOGO_DIR = os.path.join(HERE, "logos")
LOGO_CSV = os.path.join(LOGO_DIR, "logos.csv")
FONT_DIR = os.path.join(HERE, "fonts")
OUT_PDF = os.path.join(HERE, "Museum Signs.pdf")

# ----------------------------------------------------------------------------
# Design (all sizes in PDF points; 1 mm = 2.835 pt)
# ----------------------------------------------------------------------------
MM = 72.0 / 25.4
SIGN_W, SIGN_H = 80 * MM, 50 * MM
PAD = 5 * MM                       # inner margin of every sign

FONT_REG, FONT_BOLD = "LibreFranklin", "LibreFranklin-Bold"
FONT_FILES = {FONT_REG: "LibreFranklin-Regular.ttf",
              FONT_BOLD: "LibreFranklin-Bold.ttf"}

TITLE_MAX, TITLE_MIN, TITLE_LEAD = 13.5, 9.0, 1.12   # up to 2 lines
DESC_MAX, DESC_MIN, DESC_LEAD = 9.0, 6.0, 1.30       # shrinks only if needed
YEAR_SIZE = 11.0
GAP_TITLE_DESC = 3.6 * MM
GAP_DESC_FOOTER = 2.2 * MM
GAP_TEXT_LOGO = 2.5 * MM
LOGO_AREA = 200 * MM * MM          # logos get the same visual "weight"
LOGO_MAX_W, LOGO_MAX_H = 26 * MM, 15 * MM
INK = (0.08, 0.08, 0.08)
CUT_GREY = (0.62, 0.62, 0.62)

for name, fn in FONT_FILES.items():
    pdfmetrics.registerFont(TTFont(name, os.path.join(FONT_DIR, fn)))


def _face(font):
    f = pdfmetrics.getFont(font).face
    return f.capHeight / 1000.0, -f.descent / 1000.0


def text_w(s, font, size):
    return pdfmetrics.stringWidth(s, font, size)


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
@dataclass
class Sign:
    title: str = ""
    description: str = ""
    year: str = ""
    company: str = "None"
    logo: str = "auto"             # "auto" or a file name from logos.csv


FIELDS = ["title", "description", "year", "company", "logo"]


def load_signs(path=SIGNS_CSV):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        if not any(r.get(k) for k in ("title", "description", "year")):
            continue
        out.append(Sign(title=r.get("title", ""), description=r.get("description", ""),
                        year=r.get("year", ""), company=r.get("company", "") or "None",
                        logo=r.get("logo", "") or "auto"))
    return out


def save_signs(signs, path=SIGNS_CSV):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for s in signs:
            w.writerow(asdict(s))
    os.replace(tmp, path)


@dataclass
class Logo:
    company: str
    from_year: int
    file: str
    scale: float
    note: str
    path: str
    aspect: float


def load_logos():
    logos = []
    with open(LOGO_CSV, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            p = os.path.join(LOGO_DIR, r["file"].strip())
            if not os.path.exists(p):
                print("WARNING: logo file missing:", p, file=sys.stderr)
                continue
            with Image.open(p) as im:
                aspect = im.width / im.height
            logos.append(Logo(r["company"].strip(), int(r["from_year"] or 0), r["file"].strip(),
                              float(r.get("scale") or 1), (r.get("note") or "").strip(), p, aspect))
    return logos


LOGOS = load_logos()
COMPANIES = ["None"] + list(dict.fromkeys(l.company for l in LOGOS))


def company_logos(company):
    return sorted([l for l in LOGOS if l.company.lower() == (company or "").lower()],
                  key=lambda l: l.from_year)


def parse_year(s):
    m = re.search(r"\d{4}", s or "")
    return int(m.group()) if m else None


def auto_logo(company, year):
    cands = company_logos(company)
    if not cands:
        return None
    y = parse_year(year)
    if y is None:
        return cands[-1]
    chosen = cands[0]
    for l in cands:
        if l.from_year <= y:
            chosen = l
    return chosen


def logo_for(sign):
    if (sign.logo or "auto") != "auto":
        for l in company_logos(sign.company):
            if l.file == sign.logo:
                return l
    return auto_logo(sign.company, sign.year)


# ----------------------------------------------------------------------------
# Layout engine -> list of drawing operations (shared by PDF and preview)
# ----------------------------------------------------------------------------
def _break_word(word, font, size, width):
    parts, cur = [], ""
    for ch in word:
        if cur and text_w(cur + ch, font, size) > width:
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    return parts + ([cur] if cur else [])


def _ellipsize(line, font, size, width):
    while line and text_w(line + "…", font, size) > width:
        line = line[:-1].rstrip()
    return line + "…"


def flow_text(text, font, size, lead, top, bottom, width_at, max_lines=None):
    """Greedy word-wrap into a region whose width may vary with height.
    Returns (lines, complete) where lines = [(baseline_y, text, width_avail)]."""
    cap, desc = _face(font)
    y = top - cap * size
    lines = []
    paras = text.replace("\r", "").split("\n")
    for pi, para in enumerate(paras):
        words = para.split()
        if not words:
            if pi not in (0, len(paras) - 1):
                y -= lead * 0.6        # blank line = small paragraph gap
            continue
        i = 0
        while i < len(words):
            if y - desc * size < bottom or (max_lines and len(lines) >= max_lines):
                return lines, False
            w = width_at(y + cap * size, y - desc * size)
            line = words[i]
            if text_w(line, font, size) > w:
                chunks = _break_word(line, font, size, w)
                line, rest = chunks[0], "".join(chunks[1:])
                if rest:
                    words[i] = rest
                else:
                    i += 1
            else:
                i += 1
                while i < len(words) and text_w(line + " " + words[i], font, size) <= w:
                    line += " " + words[i]
                    i += 1
                # allow breaking a hyphenated compound ("Computing-Tabulating-...")
                if i < len(words) and "-" in words[i][:-1]:
                    nxt = words[i]
                    for k in range(len(nxt) - 2, 0, -1):
                        if nxt[k] == "-" and text_w(line + " " + nxt[:k + 1], font, size) <= w:
                            line += " " + nxt[:k + 1]
                            words[i] = nxt[k + 1:]
                            break
            lines.append((y, line, w))
            y -= lead * size
    return lines, True


def layout_sign(sign):
    ops = []
    warn = []
    full_w = SIGN_W - 2 * PAD

    # ---- logo (bottom right) ----
    logo = logo_for(sign)
    logo_w = logo_h = 0
    if logo:
        ar = logo.aspect
        logo_w = (LOGO_AREA * ar) ** 0.5 * logo.scale
        logo_h = logo_w / ar
        if logo_w > LOGO_MAX_W:
            logo_w, logo_h = LOGO_MAX_W, LOGO_MAX_W / ar
        if logo_h > LOGO_MAX_H:
            logo_w, logo_h = LOGO_MAX_H * ar, LOGO_MAX_H
        ops.append(("image", SIGN_W - PAD - logo_w, PAD, logo_w, logo_h, logo.path))

    # ---- year (bottom left) ----
    year_top = PAD
    year = sign.year.strip()
    if year:
        ys = YEAR_SIZE
        avail = full_w - (logo_w + GAP_TEXT_LOGO if logo else 0)
        while ys > 7 and text_w(year, FONT_BOLD, ys) > avail:
            ys -= 0.25
        ops.append(("text", PAD, PAD, FONT_BOLD, ys, year))
        year_top = PAD + _face(FONT_BOLD)[0] * ys

    # ---- title (top left, bold, max 2 lines) ----
    top = SIGN_H - PAD
    title = sign.title.strip()
    desc_top = top
    if title:
        size = TITLE_MAX
        while True:
            lines, ok = flow_text(title, FONT_BOLD, size, TITLE_LEAD, top, 0,
                                  lambda a, b: full_w, max_lines=2)
            if ok or size <= TITLE_MIN:
                break
            size -= 0.25
        if not ok:
            warn.append("Title too long")
            y, l, w = lines[-1]
            lines[-1] = (y, _ellipsize(l, FONT_BOLD, size, w), w)
        for y, l, _ in lines:
            ops.append(("text", PAD, y, FONT_BOLD, size, l))
        desc_top = lines[-1][0] - GAP_TITLE_DESC   # title baseline -> top of description caps

    # ---- description (wraps around logo, stays above year) ----
    desc = sign.description.strip()
    if desc:
        bottom = (year_top + GAP_DESC_FOOTER) if year else PAD
        logo_zone = PAD + logo_h + GAP_TEXT_LOGO if logo else -1
        narrow = full_w - logo_w - GAP_TEXT_LOGO

        def width_at(line_top, line_bottom):
            return narrow if line_bottom < logo_zone else full_w

        size = DESC_MAX
        while True:
            lines, ok = flow_text(desc, FONT_REG, size, DESC_LEAD, desc_top, bottom, width_at)
            if ok or size <= DESC_MIN:
                break
            size -= 0.25
        if size < DESC_MAX:
            warn.append(f"Description shrunk to {size:g} pt")
        if not ok:
            warn.append("Description too long - cut off")
            if lines:
                y, l, w = lines[-1]
                lines[-1] = (y, _ellipsize(l, FONT_REG, size, w), w)
        for y, l, _ in lines:
            ops.append(("text", PAD, y, FONT_REG, size, l))
    return ops, warn


# ----------------------------------------------------------------------------
# Sheet layouts (A4)
# ----------------------------------------------------------------------------
A4_P = (210 * MM, 297 * MM)
A4_L = (297 * MM, 210 * MM)
LAYOUTS = {
    "14": "14 per sheet (maximum, 3.5 mm page margin)",
    "12": "12 per sheet (landscape, 5 mm page margin)",
    "10": "10 per sheet (portrait, wide page margins)",
}


def sheet_slots(layout):
    """Return (pagesize, [(x, y, rotated)]) - x/y = lower-left corner of the slot box."""
    slots = []
    if layout == "10":
        pw, ph = A4_P
        x0, y0 = (pw - 2 * SIGN_W) / 2, (ph - 5 * SIGN_H) / 2
        for r in range(5):
            for c in range(2):
                slots.append((x0 + c * SIGN_W, ph - y0 - (r + 1) * SIGN_H, False))
        return A4_P, slots
    pw, ph = A4_L
    grid_w = 3 * SIGN_W + (SIGN_H if layout == "14" else 0)
    x0, y0 = (pw - grid_w) / 2, (ph - 4 * SIGN_H) / 2
    for r in range(4):
        for c in range(3):
            slots.append((x0 + c * SIGN_W, ph - y0 - (r + 1) * SIGN_H, False))
    if layout == "14":    # two more signs, turned 90 deg, in the strip on the right
        sy0 = (ph - 2 * SIGN_W) / 2
        for k in range(2):
            slots.append((x0 + 3 * SIGN_W, ph - sy0 - (k + 1) * SIGN_W, True))
    return A4_L, slots


def _merged_cut_lines(boxes):
    """Merge collinear edges so dotted lines don't double up."""
    h, v = {}, {}
    for x, y, w, hh in boxes:
        for yy in (y, y + hh):
            h.setdefault(round(yy, 2), []).append((x, x + w))
        for xx in (x, x + w):
            v.setdefault(round(xx, 2), []).append((y, y + hh))

    def merge(iv):
        iv = sorted(iv)
        out = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= out[-1][1] + 0.01:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out
    segs = []
    for yy, iv in h.items():
        segs += [(a, yy, b, yy) for a, b in merge(iv)]
    for xx, iv in v.items():
        segs += [(xx, a, xx, b) for a, b in merge(iv)]
    return segs


# ----------------------------------------------------------------------------
# PDF output
# ----------------------------------------------------------------------------
def _draw_ops(c, ops):
    c.setFillColorRGB(*INK)
    for op in ops:
        if op[0] == "text":
            _, x, y, font, size, s = op
            c.setFont(font, size)
            c.drawString(x, y, s)
        elif op[0] == "image":
            _, x, y, w, h, path = op
            c.drawImage(path, x, y, w, h, mask="auto")


def make_pdf(signs, out=OUT_PDF, layout="14", cut_lines=True):
    pagesize, slots = sheet_slots(layout)
    c = rl_canvas.Canvas(out, pagesize=pagesize)
    c.setTitle("Museum Signs")
    c.setAuthor("Computer Museum")
    per = len(slots)
    pages = max(1, (len(signs) + per - 1) // per)
    for p in range(pages):
        chunk = signs[p * per:(p + 1) * per]
        boxes = []
        for sign, (x, y, rot) in zip(chunk, slots):
            ops, _ = layout_sign(sign)
            c.saveState()
            if rot:
                c.translate(x + SIGN_H, y)
                c.rotate(90)
                boxes.append((x, y, SIGN_H, SIGN_W))
            else:
                c.translate(x, y)
                boxes.append((x, y, SIGN_W, SIGN_H))
            _draw_ops(c, ops)
            c.restoreState()
        if cut_lines and boxes:
            c.setStrokeColorRGB(*CUT_GREY)
            c.setLineWidth(0.35)
            c.setDash(1, 1.6)
            for x1, y1, x2, y2 in _merged_cut_lines(boxes):
                c.line(x1, y1, x2, y2)
        c.showPage()
    c.save()
    return out, pages


# ----------------------------------------------------------------------------
# Raster preview (same layout engine)
# ----------------------------------------------------------------------------
_pil_fonts = {}


def render_preview(sign, px_per_mm=5.0):
    s = px_per_mm / MM
    W, H = round(SIGN_W * s), round(SIGN_H * s)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    ops, warn = layout_sign(sign)
    ink = tuple(int(v * 255) for v in INK)
    for op in ops:
        if op[0] == "text":
            _, x, y, font, size, txt = op
            key = (font, round(size * s * 4))
            if key not in _pil_fonts:
                _pil_fonts[key] = ImageFont.truetype(os.path.join(FONT_DIR, FONT_FILES[font]), size * s)
            d.text((x * s, (SIGN_H - y) * s), txt, font=_pil_fonts[key], fill=ink, anchor="ls")
        elif op[0] == "image":
            _, x, y, w, h, path = op
            lg = Image.open(path).convert("RGBA").resize((max(1, round(w * s)), max(1, round(h * s))),
                                                         Image.LANCZOS)
            img.paste(lg, (round(x * s), round((SIGN_H - y - h) * s)), lg)
    d.rectangle([0, 0, W - 1, H - 1], outline=(170, 170, 170))
    return img, warn


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
def load_settings():
    try:
        with open(SETTINGS_JSON) as f:
            return json.load(f)
    except Exception:
        return {"layout": "14", "cut_lines": True}


def save_settings(st):
    try:
        with open(SETTINGS_JSON, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def open_file(path):
    if sys.platform == "darwin":
        subprocess.run(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", path])


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    from PIL import ImageTk

    AUTO_PREFIX = "Automatic by year: "

    class App:
        def __init__(self, root):
            self.root = root
            self.signs = load_signs() or load_signs(EXAMPLE_CSV) or [Sign()]
            self.settings = load_settings()
            self.cur = None
            self.loading = False
            self.pending = None
            root.title("Museum Signs - 80 × 50 mm")
            root.minsize(980, 620)

            outer = ttk.Frame(root, padding=10)
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            outer.columnconfigure(1, weight=0)
            outer.rowconfigure(0, weight=1)

            # ---------- left: list of signs ----------
            left = ttk.Frame(outer)
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
            left.rowconfigure(1, weight=1)
            left.columnconfigure(0, weight=1)
            ttk.Label(left, text="Signs", font=("Helvetica", 14, "bold")).grid(row=0, column=0, sticky="w")
            self.tree = ttk.Treeview(left, columns=("n", "title", "year", "company"), show="headings",
                                     selectmode="extended")
            for col, txt, w, st in (("n", "#", 40, False), ("title", "Title", 260, True),
                                    ("year", "Year", 70, False), ("company", "Company", 80, False)):
                self.tree.heading(col, text=txt)
                self.tree.column(col, width=w, stretch=st, anchor="w")
            self.tree.grid(row=1, column=0, sticky="nsew")
            sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
            sb.grid(row=1, column=1, sticky="ns")
            self.tree.configure(yscrollcommand=sb.set)
            self.tree.bind("<<TreeviewSelect>>", self.on_select)

            bar = ttk.Frame(left)
            bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            ttk.Button(bar, text="+ New sign", command=self.new_sign).pack(side="left")
            ttk.Button(bar, text="Duplicate", command=self.duplicate).pack(side="left", padx=4)
            ttk.Button(bar, text="Delete", command=self.delete).pack(side="left")
            ttk.Button(bar, text="▼", width=3, command=lambda: self.move(1)).pack(side="right")
            ttk.Button(bar, text="▲", width=3, command=lambda: self.move(-1)).pack(side="right", padx=4)

            # ---------- right: editor + preview ----------
            right = ttk.Frame(outer)
            right.grid(row=0, column=1, sticky="nsew")
            right.columnconfigure(1, weight=1)
            r = 0
            ttk.Label(right, text="Edit sign", font=("Helvetica", 14, "bold")).grid(row=r, column=0, columnspan=2, sticky="w")
            r += 1
            self.v_title, self.v_year = tk.StringVar(), tk.StringVar()
            self.v_company, self.v_logo = tk.StringVar(), tk.StringVar()

            ttk.Label(right, text="Title").grid(row=r, column=0, sticky="w", pady=3)
            self.e_title = ttk.Entry(right, textvariable=self.v_title, width=48)
            self.e_title.grid(row=r, column=1, sticky="ew", pady=3)
            r += 1
            ttk.Label(right, text="Description").grid(row=r, column=0, sticky="nw", pady=3)
            self.t_desc = tk.Text(right, height=5, width=48, wrap="word", font=("Helvetica", 13),
                                  relief="solid", borderwidth=1, undo=True)
            self.t_desc.grid(row=r, column=1, sticky="ew", pady=3)
            r += 1
            ttk.Label(right, text="Year").grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(right, textvariable=self.v_year, width=14).grid(row=r, column=1, sticky="w", pady=3)
            r += 1
            ttk.Label(right, text="Company").grid(row=r, column=0, sticky="w", pady=3)
            self.c_company = ttk.Combobox(right, textvariable=self.v_company, values=COMPANIES,
                                          state="readonly", width=14)
            self.c_company.grid(row=r, column=1, sticky="w", pady=3)
            r += 1
            ttk.Label(right, text="Logo").grid(row=r, column=0, sticky="w", pady=3)
            self.c_logo = ttk.Combobox(right, textvariable=self.v_logo, state="readonly", width=52)
            self.c_logo.grid(row=r, column=1, sticky="ew", pady=3)
            r += 1
            ttk.Label(right, text="Preview").grid(row=r, column=0, sticky="nw", pady=(12, 3))
            self.preview = tk.Label(right, bd=0, bg="white")
            self.preview.grid(row=r, column=1, sticky="w", pady=(12, 3))
            r += 1
            self.l_warn = ttk.Label(right, text="", foreground="#c0392b")
            self.l_warn.grid(row=r, column=1, sticky="w")
            r += 1

            # ---------- bottom: print ----------
            pf = ttk.LabelFrame(outer, text="Print", padding=8)
            pf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
            ttk.Label(pf, text="Sheet layout:").pack(side="left")
            self.v_layout = tk.StringVar(value=LAYOUTS.get(self.settings.get("layout", "14"), LAYOUTS["14"]))
            cl = ttk.Combobox(pf, textvariable=self.v_layout, values=list(LAYOUTS.values()),
                              state="readonly", width=40)
            cl.pack(side="left", padx=6)
            cl.bind("<<ComboboxSelected>>", lambda e: self.store_settings())
            self.v_cut = tk.BooleanVar(value=self.settings.get("cut_lines", True))
            ttk.Checkbutton(pf, text="Dotted cut lines", variable=self.v_cut,
                            command=self.store_settings).pack(side="left", padx=8)
            ttk.Button(pf, text="Create PDF - ALL signs", command=lambda: self.pdf(False)).pack(side="right")
            ttk.Button(pf, text="Create PDF - selected only", command=lambda: self.pdf(True)).pack(side="right", padx=6)
            self.l_status = ttk.Label(outer, text="", foreground="#666")
            self.l_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

            # ---------- events ----------
            self.v_title.trace_add("write", self.on_edit)
            self.v_year.trace_add("write", self.on_edit)
            self.c_company.bind("<<ComboboxSelected>>", self.on_company)
            self.c_logo.bind("<<ComboboxSelected>>", self.on_edit)
            self.t_desc.bind("<<Modified>>", self.on_desc)
            mod = "Command" if sys.platform == "darwin" else "Control"
            root.bind_all(f"<{mod}-n>", lambda e: self.new_sign())
            root.bind_all(f"<{mod}-d>", lambda e: self.duplicate())
            root.bind_all(f"<{mod}-p>", lambda e: self.pdf(False))
            root.protocol("WM_DELETE_WINDOW", self.quit)

            self.refresh_tree(select=0)

        # ----- helpers -----
        def layout_key(self):
            for k, v in LAYOUTS.items():
                if v == self.v_layout.get():
                    return k
            return "14"

        def store_settings(self):
            self.settings = {"layout": self.layout_key(), "cut_lines": bool(self.v_cut.get())}
            save_settings(self.settings)
            self.update_status()

        def update_status(self):
            per = len(sheet_slots(self.layout_key())[1])
            n = len(self.signs)
            pages = max(1, (n + per - 1) // per)
            self.l_status.config(text=f"{n} sign(s) → {pages} A4 sheet(s) at {per} per sheet.   "
                                      f"Saved automatically to signs.csv")

        def refresh_tree(self, select=None):
            self.tree.delete(*self.tree.get_children())
            for i, s in enumerate(self.signs):
                self.tree.insert("", "end", iid=str(i),
                                 values=(i + 1, s.title or "(untitled)", s.year, s.company))
            if select is not None and self.signs:
                select = max(0, min(select, len(self.signs) - 1))
                self.tree.selection_set(str(select))
                self.tree.see(str(select))
                self.on_select()
            self.update_status()

        def update_row(self, i):
            s = self.signs[i]
            self.tree.item(str(i), values=(i + 1, s.title or "(untitled)", s.year, s.company))

        def logo_choices(self, s):
            al = auto_logo(s.company, s.year)
            if not al:
                return ["(no logo)"], {}
            m = {AUTO_PREFIX + (al.note or al.file): "auto"}
            for l in company_logos(s.company):
                m[(l.note or l.file)] = l.file
            return list(m.keys()), m

        def sync_logo_box(self):
            s = self.signs[self.cur]
            vals, self.logo_map = self.logo_choices(s)
            self.c_logo.configure(values=vals)
            inv = {v: k for k, v in self.logo_map.items()}
            self.loading = True
            self.v_logo.set(inv.get(s.logo, vals[0]))
            self.loading = False

        # ----- events -----
        def on_select(self, _=None):
            sel = self.tree.selection()
            if not sel:
                return
            i = int(sel[0])
            if i == self.cur:
                return
            self.cur = i
            s = self.signs[i]
            self.loading = True
            self.v_title.set(s.title)
            self.v_year.set(s.year)
            self.v_company.set(s.company if s.company in COMPANIES else "None")
            self.t_desc.delete("1.0", "end")
            self.t_desc.insert("1.0", s.description)
            self.t_desc.edit_modified(False)
            self.t_desc.edit_reset()
            self.loading = False
            self.sync_logo_box()
            self.update_preview()

        def on_desc(self, _=None):
            if self.t_desc.edit_modified():
                self.t_desc.edit_modified(False)
                self.on_edit()

        def on_company(self, _=None):
            if self.cur is None:
                return
            self.signs[self.cur].company = self.v_company.get()
            self.signs[self.cur].logo = "auto"
            self.on_edit()

        def on_edit(self, *_):
            if self.loading or self.cur is None:
                return
            s = self.signs[self.cur]
            s.title = self.v_title.get()
            s.year = self.v_year.get()
            s.company = self.v_company.get() or "None"
            s.description = self.t_desc.get("1.0", "end-1c")
            if hasattr(self, "logo_map") and self.v_logo.get() in self.logo_map:
                s.logo = self.logo_map[self.v_logo.get()]
            self.update_row(self.cur)
            self.sync_logo_box()
            if self.pending:
                self.root.after_cancel(self.pending)
            self.pending = self.root.after(250, self.commit)

        def commit(self):
            self.pending = None
            self.update_preview()
            save_signs(self.signs)

        def update_preview(self):
            if self.cur is None:
                return
            img, warn = render_preview(self.signs[self.cur], px_per_mm=5.5)
            self.photo = ImageTk.PhotoImage(img)
            self.preview.configure(image=self.photo)
            self.l_warn.configure(text="  •  ".join(warn))

        # ----- list actions -----
        def new_sign(self):
            comp = self.signs[self.cur].company if self.cur is not None and self.signs else "None"
            pos = (self.cur + 1) if self.cur is not None else len(self.signs)
            self.signs.insert(pos, Sign(company=comp))
            self.cur = None
            self.refresh_tree(select=pos)
            save_signs(self.signs)
            self.e_title.focus_set()

        def duplicate(self):
            if self.cur is None:
                return
            pos = self.cur + 1
            self.signs.insert(pos, Sign(**asdict(self.signs[self.cur])))
            self.cur = None
            self.refresh_tree(select=pos)
            save_signs(self.signs)

        def delete(self):
            sel = sorted((int(i) for i in self.tree.selection()), reverse=True)
            if not sel:
                return
            names = ", ".join(self.signs[i].title or "(untitled)" for i in sorted(sel))
            if not messagebox.askyesno("Delete", f"Delete {len(sel)} sign(s)?\n\n{names}"):
                return
            for i in sel:
                del self.signs[i]
            if not self.signs:
                self.signs.append(Sign())
            self.cur = None
            self.refresh_tree(select=min(sel))
            save_signs(self.signs)

        def move(self, d):
            if self.cur is None:
                return
            i, j = self.cur, self.cur + d
            if not 0 <= j < len(self.signs):
                return
            self.signs[i], self.signs[j] = self.signs[j], self.signs[i]
            self.cur = None
            self.refresh_tree(select=j)
            save_signs(self.signs)

        def pdf(self, selected_only):
            if self.pending:
                self.root.after_cancel(self.pending)
                self.commit()
            if selected_only:
                idx = sorted(int(i) for i in self.tree.selection())
                signs = [self.signs[i] for i in idx]
            else:
                signs = list(self.signs)
            signs = [s for s in signs if s.title or s.description or s.year]
            if not signs:
                messagebox.showinfo("Nothing to print", "There are no signs with content.")
                return
            try:
                out, pages = make_pdf(signs, OUT_PDF, self.layout_key(), bool(self.v_cut.get()))
            except Exception as e:
                messagebox.showerror("PDF failed", str(e))
                return
            self.l_status.config(text=f"PDF created: {len(signs)} sign(s) on {pages} sheet(s) → {out}")
            open_file(out)

        def quit(self):
            if self.pending:
                self.root.after_cancel(self.pending)
                self.commit()
            self.root.destroy()

    root = tk.Tk()
    App(root)
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.mainloop()


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", action="store_true", help="build the PDF without opening the window")
    ap.add_argument("--csv", default=SIGNS_CSV, help="input CSV (default: signs.csv)")
    ap.add_argument("-o", "--out", default=OUT_PDF)
    ap.add_argument("--layout", choices=list(LAYOUTS), default=None)
    ap.add_argument("--no-cut-lines", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if a.pdf:
        st = load_settings()
        signs = load_signs(a.csv) or (load_signs(EXAMPLE_CSV) if a.csv == SIGNS_CSV else [])
        out, pages = make_pdf(signs, a.out, a.layout or st.get("layout", "14"),
                              not a.no_cut_lines and st.get("cut_lines", True))
        print(f"{len(signs)} signs -> {pages} page(s): {out}")
        if not a.no_open:
            open_file(out)
    else:
        run_gui()


if __name__ == "__main__":
    main()

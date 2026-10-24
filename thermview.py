"""thermview - local viewer for the thermwatch store, in the shape of redditview.

Left column: the devices, and clicking one adds or drops it from the selection,
so any subset from none to all is reachable. A device whose battery the last
poll saw at or below 50% has a name that flashes, with a line saying why. Right
column: a **dropdown** for the window the graphs cover - picking one applies
straight away, through a two-line script that turns the choice into the address
it would have produced, and the option in use is marked - and a month grid whose
days link to that day. Middle: one graph per measurement, every selected device
drawn over the same axes in its own colour, with a key.

Times are stored UTC and shown in a **display zone** the settings screen picks
from a dropdown (`display.json` next to the scripts, gitignored); `?tz=<name>`
asks for one zone for a single view. A zone only changes how a time is written -
no data moves and no filter breaks - and a name with no zone data is refused and
said so rather than shown wrong.

The left column ends with **settings**, its own screen: every device that is
named in devices.json, present in the store without a name, or answered an
active scan when the page is asked for one. The Bluetooth address is shown and
never editable - it is the identity - while the name box and the dropdown of
the CSVs in `historical/` are, and one Save button writes devices.json and
stores the chosen file for that device. Same for a device that has never
answered: name it, choose the export the phone app made for it, and the history
comes in. A second button per device takes back exactly the rows one of those
files added, so a wrong export can be undone; a third deletes every row of that
device, but only after a page says how many would go and asks. Those two are
the only places this store loses data.

Stdlib only - http.server, sqlite3, and hand-written SVG. Every path it uses is
relative to the folder the script sits in, so a copy of that folder runs where it
stands:

    thermview.bat                 # serves http://127.0.0.1:8957/
    thermview.bat --port 8123
    python test_view.py           # offline checks, no server and no browser

The store is re-read when the file changes on disk, so a scheduled thermwatch
run shows up without restarting the viewer.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, HTTPServer

import html as _html

STORE = Path(__file__).resolve().parent / "thermometer.sqlite3"
CONFIG = Path(__file__).resolve().parent / "devices.json"


def norm_address(value: str) -> str:
    """`A4:C1:…`, `a4c1…` and `0xa4c1…` are one address."""
    return value.removeprefix("0x").replace(":", "").replace("-", "").lower()


def config_names() -> dict:
    """{address: display name} - read per request so a rename applies live."""
    try:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {norm_address(a): n for n, a in raw.items()
            if not n.startswith("_") and isinstance(a, str)}


def label(addr: str) -> str:
    """How a device is shown: the current config name, else its address."""
    return config_names().get(norm_address(addr), addr)

# Fixed so the same device keeps the same colour across pages.
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f"]

KINDS = (("temperature", "°C", "Temperature"), ("humidity", "%", "Humidity"))

# What the live panel shows. Battery and signal strength are read and stored,
# and they are useful next to a device, but they are not drawn as graphs.
LIVE_KINDS = KINDS + (("battery", "%", "Battery"),
                      ("signal_strength", "dBm", "Signal strength"))

SHOTS = Path(__file__).resolve().parent / "shots"

# Where the phone app's CSV exports live (see `--import` in thermwatch). The
# devices screen lists what is in here so a file can be chosen for a device;
# the files are data, not part of the tool.
HIST = Path(__file__).resolve().parent / "historical"

# What the page writes times in. The store keeps UTC and always will - a zone
# only decides how a time is shown, so changing it moves no data and breaks no
# filter. The settings screen offers a list and saves the choice here; a page
# can also carry `?tz=<name>` for one view. `ZONES` is only what the dropdown
# offers: any IANA name works in the address, and one the system has no data
# for is UTC, said on the page rather than shown wrong.
DISPLAY = Path(__file__).resolve().parent / "display.json"
ZONES = ("UTC", "Europe/London", "Europe/Paris", "Europe/Istanbul",
         "Asia/Karachi", "Asia/Kolkata", "America/New_York",
         "America/Los_Angeles", "Australia/Sydney")
LOW_BATTERY = 50.0     # at or below this, a device's name flashes
UNIT = None            # "C" or "F" for this request; None means the saved one


TZ = None            # the zone this request draws in; set per request


def display_doc() -> dict:
    """What the settings screen saved - a zone and a unit; {} when nothing has."""
    try:
        return json.loads(DISPLAY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_display(doc: dict) -> None:
    DISPLAY.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                       encoding="utf-8")


def save_zone(name: str) -> str:
    """Write the display zone. Returns the name that took effect."""
    try:
        ZoneInfo(name)                      # a name with no data is refused
    except Exception:
        return ""
    doc = display_doc()
    doc["tz"] = name
    write_display(doc)
    return name


def save_unit(name: str) -> str:
    """Write the display unit. Returns "C" or "F"; anything else is refused."""
    got = ("F" if str(name).upper().startswith("F") else
           "C" if str(name).upper().startswith("C") else "")
    if not got:
        return ""
    doc = display_doc()
    doc["unit"] = got
    write_display(doc)
    return got


def pick_display(params: dict) -> tuple:
    """Set the zone and the unit this request draws in; say what they are called.

    `?tz=` and `?unit=` win over the saved choice, so a shared link shows the
    same times and the same degrees whoever opens it. A zone with no data falls
    back to UTC and says so; a unit that is not C or F stays Celsius.
    """
    global TZ, UNIT
    name = (params.get("tz") or [display_doc().get("tz", "UTC")])[0]
    try:
        TZ = ZoneInfo(name)
    except Exception:
        TZ = ZoneInfo("UTC")
    unit = str((params.get("unit") or [display_doc().get("unit", "C")])[0])
    UNIT = "F" if unit.upper().startswith("F") else "C"
    return (TZ.tzname(now()) if name != "UTC" else "UTC"), unit_of("temperature")


def unit_now() -> str:
    """The unit this request draws temperatures in."""
    return UNIT or "C"


def conv(value: float, kind: str) -> float:
    """A stored value as this page draws it. Only temperature converts.

    The store keeps °C and always will - a unit only decides how a number is
    written, so changing it moves no data and breaks no filter. Humidity is a
    percentage, battery a percentage and signal a decibel: none of them is a
    temperature, so none of them is converted.
    """
    if kind == "temperature" and unit_now() == "F":
        return value * 9 / 5 + 32
    return value


def unit_of(kind: str) -> str:
    """The unit a value of this kind is drawn with."""
    if kind == "temperature":
        return "°F" if unit_now() == "F" else "°C"
    return {"humidity": "%", "battery": "%",
            "signal_strength": "dBm"}.get(kind, "")


def show(when: datetime) -> datetime:
    """A stored UTC time as the zone this page draws in."""
    return when.astimezone(TZ) if TZ else when

LIVE = {}          # address -> {"ts": iso, "values": {key: (value, unit)}}
LIVE_INFO = {"interval": 0.0, "next": None, "started": None, "note": "off"}

RANGES = (("1h", "last hour", 1), ("24h", "last 24 hours", 24),
          ("7d", "last 7 days", 24 * 7), ("30d", "last 30 days", 24 * 30),
          ("1y", "last year", 24 * 365))

W, H = 860, 210          # graph box
MAX_POINTS = 700         # a month of minute data is 40k points; draw buckets
PAD_L, PAD_R, PAD_T, PAD_B = 46, 14, 14, 26


def esc(value: str) -> str:
    return _html.escape(value, quote=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def store_stamp() -> tuple:
    try:
        st = STORE.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return (0, 0)


class StoreView:
    """One open sqlite connection, reopened when the file moves on disk."""
    con = None
    stamp = (0, 0)

    def refresh(self) -> None:
        s = store_stamp()
        if self.con is None:
            self.con = sqlite3.connect(STORE)
        if s != self.stamp:
            if self.con is not None:
                try:
                    self.con.close()
                except Exception:
                    pass
            self.con = sqlite3.connect(STORE)
            print(f"[store] the file changed while browsing - re-reading it "
                  f"({s[1]} bytes)", flush=True)
        self.stamp = s

    def rows(self, device_sql: str, args: list) -> list:
        if self.con is None:
            self.refresh()
        try:
            return self.con.execute(
                "SELECT ts_utc, address, key, value FROM readings "
                "WHERE ts_utc >= ? AND ts_utc <= ? " + device_sql,
                args,
            ).fetchall()
        except sqlite3.Error:
            return []

    def addresses(self) -> list:
        if self.con is None:
            self.refresh()
        try:
            return [r[0] for r in self.con.execute(
                "SELECT address FROM readings GROUP BY address "
                "ORDER BY address")]
        except sqlite3.Error:
            return []

    def months(self) -> list:
        if self.con is None:
            self.refresh()
        try:
            return [r[0] for r in self.con.execute(
                "SELECT substr(ts_utc,1,7) FROM readings GROUP BY "
                "substr(ts_utc,1,7) ORDER BY substr(ts_utc,1,7) DESC")]
        except sqlite3.Error:
            return []


Store = StoreView()


def parse(path: str) -> tuple[str, dict]:
    """`/…?a=1&b=2` -> (route, params)."""
    if path == "/":
        return "/", {}
    base, _, q = path.partition("?")
    out = {}
    for part in filter(None, q.split("&")):
        k, _, v = part.partition("=")
        out.setdefault(k, []).append(unquote(v))
    return base, out


def pick_devices(sel: list, all_names: list) -> list:
    """Same rules as the menu, but resolved to addresses (the identity)."""
    names = {label(a): a for a in all_names}
    want = [x for part in (sel or []) for x in part.split(",") if x != ""]
    if not want or want == ["*"]:
        return list(all_names)
    if want == ["-*"]:
        return []
    if want[0].startswith("-"):
        drop = {norm_name(x[1:]) for x in want if x.startswith("-")}
        return [a for a in all_names if norm_name(label(a)) not in drop]
    keep = {norm_name(x) for x in want}
    return [a for a in all_names
            if norm_name(label(a)) in keep or norm_name(a) in keep]


def norm_name(value: str) -> str:
    return value.strip().lower()


def span(params: dict) -> tuple[str, str, str]:
    """(from, to, label) for the request."""
    if params.get("date"):
        d = params["date"][0]
        return (d + "T00:00:00+00:00", d + "T23:59:59+00:00", d)
    if params.get("from") or params.get("to"):
        frm = params.get("from", [""])[0]
        to = params.get("to", [""])[0]
        return (frm or "0000-01-01T00:00:00+00:00",
                to or "9999-12-31T23:59:59+00:00",
                f"{frm[:10]} .. {to[:10]}")
    for key, label, hours in RANGES:
        if params.get("range", []) and params["range"][0] == key:
            frm = (now() - timedelta(hours=hours)).isoformat(
                timespec="seconds")
            return (frm, now().isoformat(timespec="seconds"), label)
    return ("0000-01-01T00:00:00+00:00",
            now().isoformat(timespec="seconds"), "all data")


def scene(kind: str, unit: str, title: str, by_device: dict,
          names: list) -> list:
    """One graph as primitives, so SVG and a real JPG draw the same thing.

    A primitive is a tuple: ("rect",x,y,w,h,fill) ("line",x1,y1,x2,y2,colour)
    ("poly",[(x,y)...],colour,width) ("dot",cx,cy,r,colour) or
    ("text",x,y,words,size,colour,anchor).
    """
    pts = {d: [(t, conv(v, kind)) for t, v in vs]
           for d, vs in by_device.items()}
    all_v = [v for vs in pts.values() for _t, v in vs]
    if not all_v:
        return [("text", PAD_L, H / 2, f"{title}: no points in this range",
                 12, "#8b95a1", "start")]
    xs = sorted({t for vs in pts.values() for t, _v in vs})
    lo, hi = min(all_v), max(all_v)
    if hi - lo < 1:
        lo, hi = lo - 0.5, hi + 0.5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    t0, t1 = xs[0], xs[-1]
    span_t = max((t1 - t0).total_seconds(), 1.0)

    def bucket(vs: list) -> tuple[list, str]:
        """Average long series into buckets, and say so in the chart."""
        if len(vs) <= MAX_POINTS:
            return list(vs), ""
        t0, t1 = vs[0][0], vs[-1][0]
        span = max((t1 - t0).total_seconds(), 1.0)
        step = span / MAX_POINTS
        groups = {}
        for t, v in vs:
            k = min(int((t - t0).total_seconds() // step), MAX_POINTS - 1)
            groups.setdefault(k, []).append(v)
        out = [(t0 + timedelta(seconds=step * (k + 0.5)),
                sum(vv) / len(vv)) for k, vv in sorted(groups.items())]
        hours = step / 3600.0
        size = f"{hours:.1f} h" if hours >= 1 else f"{step:.0f} s"
        return out, f"{len(vs)} points, {size} averages"

    def path_with_gaps(vs: list) -> list:
        """Points per continuous stretch; a real gap starts a new one.

        The expected spacing is the middle of all deltas, so a stretch with no
        data (a dead device, an offline night) breaks instead of bridging.
        """
        if len(vs) < 2:
            return [vs] if vs else []
        deltas = sorted((b[0] - a[0]).total_seconds()
                        for a, b in zip(vs, vs[1:]))
        step = deltas[len(deltas) // 2]
        parts, cur = [], []
        for i, (t, v) in enumerate(vs):
            if i and step > 0 and (t - vs[i - 1][0]).total_seconds() > step * 3:
                parts.append(cur)
                cur = []
            cur.append((t, v))
        parts.append(cur)
        return [part for part in parts if part]

    def px(t: datetime) -> float:
        return PAD_L + (t - t0).total_seconds() / span_t * (W - PAD_L - PAD_R)

    def py(v: float) -> float:
        return H - PAD_B - (v - lo) / (hi - lo) * (H - PAD_T - PAD_B)

    out = [("rect", 0, 0, W, H, "#101418")]
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        y = py(v)
        out.append(("line", PAD_L, y, W - PAD_R, y, "#2b333b"))
        out.append(("text", PAD_L - 6, y + 3, f"{v:.1f}", 10, "#8b95a1", "end"))
    steps = min(6, len(xs))
    for i in range(steps):
        t = t0 + timedelta(seconds=span_t * i / max(steps - 1, 1))
        x = px(t)
        out.append(("line", x, H - PAD_B, x, H - PAD_B + 4, "#8b95a1"))
        out.append(("text", x, H - PAD_B + 16, show(t).strftime("%m-%d %H:%M"), 10,
                    "#8b95a1", "middle"))
    note = ""
    for n, colour in zip(names, PALETTE):
        raw = pts.get(n, [])
        if not raw:
            continue
        vs, note2 = bucket(raw)
        note = note2 or note
        out.append(("poly", [[(px(t), py(v)) for t, v in part]
                    for part in path_with_gaps(vs)], colour, 2))
        last_t, last_v = vs[-1]
        out.append(("dot", px(last_t), py(last_v), 3, colour))
    if note:
        out.append(("text", W - PAD_R - 4, PAD_T + 10, note, 10, "#8b95a1",
                    "end"))
    return out


def svg_of(prims: list) -> str:
    """SVG markup for a scene."""
    out = []
    for p in prims:
        if p[0] == "rect":
            _k, x, y, w, h, fill = p
            out.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{w:.1f}' "
                       f"height='{h:.1f}' fill='{fill}'/>")
        elif p[0] == "line":
            _k, x1, y1, x2, y2, colour = p
            out.append(f"<line x1='{x1:.1f}' y1='{y1:.1f}' x2='{x2:.1f}' "
                       f"y2='{y2:.1f}' stroke='{colour}'/>")
        elif p[0] == "poly":
            _k, parts, colour, width = p
            d = " ".join(
                " ".join(f"{'M' if k == 0 else 'L'}{x:.1f},{y:.1f}"
                         for k, (x, y) in enumerate(part))
                for part in parts)
            out.append(f"<path d='{d}' fill='none' stroke='{colour}' "
                       f"stroke-width='{width}'/>")
        elif p[0] == "dot":
            _k, cx, cy, r, colour = p
            out.append(f"<circle cx='{cx:.1f}' cy='{cy:.1f}' r='{r}' "
                       f"fill='{colour}'/>")
        elif p[0] == "text":
            _k, x, y, words, size, colour, anchor = p
            out.append(f"<text x='{x:.1f}' y='{y:.1f}' fill='{colour}' "
                       f"font-size='{size}' text-anchor='{anchor}'>"
                       f"{esc(words)}</text>")
    return "".join(out)


def jpg_of(scenes: list, total_h: int) -> bytes:
    """Draw the same primitives into a JPEG. Pillow is already in the project."""
    from PIL import Image, ImageDraw
    try:
        from PIL import ImageFont
        font = ImageFont.load_default(11)
    except Exception:
        font = None

    def text(d, x, y, words, size, colour, anchor):
        if font:
            w = d.textlength(words, font=font)
        else:
            w = len(words) * size * 0.55
        x0 = x - w if anchor == "end" else (
            x - w / 2 if anchor == "middle" else x)
        if font:
            d.text((x0, y - size * 0.8), words, fill=colour, font=font)
        else:
            d.text((x0, y - size * 0.8), words, fill=colour)

    img = Image.new("RGB", (W, total_h), (14, 17, 20))
    d = ImageDraw.Draw(img)
    for off, prims in scenes:
        for p in prims:
            if p[0] == "rect":
                _k, x, y, w, h, fill = p
                d.rectangle([x, y + off, x + w, y + h + off], fill=fill)
            elif p[0] == "line":
                _k, x1, y1, x2, y2, colour = p
                d.line([x1, y1 + off, x2, y2 + off], fill=colour)
            elif p[0] == "poly":
                _k, parts, colour, width = p
                for part in parts:
                    d.line([(x, y + off) for x, y in part], fill=colour,
                           width=int(width))
            elif p[0] == "dot":
                _k, cx, cy, r, colour = p
                d.ellipse([cx - r, cy + off - r, cx + r, cy + off + r],
                          fill=colour)
            elif p[0] == "text":
                _k, x, y, words, size, colour, anchor = p
                text(d, x, y + off, words, size, colour, anchor)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def svg_doc(scenes: list, total_h: int) -> bytes:
    """A standalone SVG document from the same primitives."""
    parts = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' "
             f"height='{total_h}' viewBox='0 0 {W} {total_h}'>"]
    for off, prims in scenes:
        parts.append(f"<g transform='translate(0 {off})'>"
                     + svg_of(prims) + "</g>")
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


HEAD, GAP = 74, 24       # shot header block and the gap between graphs


def shot_scenes(params: dict) -> tuple[list, int]:
    """Everything a shot needs: the header block, then one block per graph."""
    selected, kinds, desc, _n, series = series_of(params)
    out = [(0, [
        ("rect", 0, 0, W, HEAD, "#0e1114"),
        ("text", 12, 24, f"thermview - {desc}", 15, "#c8d2dc", "start"),
        ("text", 12, 46, "devices: " + ", ".join(label(a) for a in selected)
         or "none", 11, "#8b95a1", "start"),
        ("text", 12, 62, "graphs: " + ", ".join(t for _k, _u, t in kinds),
         11, "#8b95a1", "start"),
    ])]
    off = HEAD
    for kind, unit, title in kinds:
        got = series.get(kind, {})
        if not got:
            continue
        prims = scene(kind, unit, title, got, selected)
        prims.insert(1, ("text", 8, 16, f"{title} ({unit})", 12, "#c8d2dc",
                        "start"))
        out.append((off, prims))
        off += H + GAP
    return out, off


def shot_file(params: dict, fmt: str) -> tuple[str, bytes, str]:
    """Write one shot into shots/ and return (name, bytes, content type)."""
    SHOTS.mkdir(exist_ok=True)
    scenes, total = shot_scenes(params)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    slug = "-".join(k for k, _u, _t in kinds_of(params)) or "none"
    if fmt == "jpg":
        data, ctype = jpg_of(scenes, total), "image/jpeg"
    else:
        data, ctype = svg_doc(scenes, total), "image/svg+xml"
    name = f"{stamp}-{slug}.{'jpg' if fmt == 'jpg' else 'svg'}"
    (SHOTS / name).write_bytes(data)
    return name, data, ctype


def graph(kind: str, unit: str, title: str, by_device: dict, names: list) -> str:
    """One inline SVG: x = time, y = value, one line per device."""
    prims = scene(kind, unit, title, by_device, names)
    return (f"<svg viewBox='0 0 {W} {H}' role='img' "
            f"aria-label='{esc(title)} graph' class='g'>"
            + svg_of(prims) + "</svg>")


def graph_svg(params: dict, kind: str) -> bytes:
    pick_display(params)   # a /graph image is drawn in the zone and unit asked for
    """One graph as a standalone SVG document, for the image route.

    The same primitives `graph` draws - no title inside, the page already has
    the heading above it - so the picture the browser loads on its own is the
    picture the page showed. It re-reads the store, which is what lets a new
    sample appear without the page being reloaded.
    """
    Store.refresh()
    selected, _kinds, _desc, _n, series = series_of(params)
    for k, unit, title in KINDS:
        if k == kind:
            prims = scene(k, unit, title, series.get(k, {}), selected)
            return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{W}' "
                    f"height='{H}' viewBox='0 0 {W} {H}'>"
                    + svg_of(prims) + "</svg>").encode("utf-8")
    return b"<svg xmlns='http://www.w3.org/2000/svg'/>"


def graph_src(params: dict, kind: str) -> str:
    """The address of one graph as its own image, with the current filters.

    `t=` is not here: the page script adds a fresh one on every redraw, which is
    what makes the browser ask again instead of reusing the copy it has.
    """
    parts = [f"k={_q(kind)}"]
    for key in ("dev", "range", "from", "to", "date", "month"):
        if params.get(key):
            parts.append(f"{key}={_q(params[key][0])}")
    return "/graph?" + "&".join(parts)


def auto_view(params: dict) -> bool:
    """Is this the last-hour view, where the graph redraws itself?

    Only that range, and only when the page polls at all. A wider window does
    not move on by itself, so re-drawing it every few seconds would ask for the
    same picture over and over.
    """
    return ((params.get("range") or [""])[0] == "1h"
            and LIVE_INFO.get("refresh", 0.0) > 0)


def auto_img(kind: str, unit: str, title: str, params: dict) -> str:
    """One graph as an image the page script can reload in place."""
    src = graph_src(params, kind)
    return (f"<img class='g auto' src='{src}' data-base='{src}' "
            f"alt='{esc(title)} ({esc(unit)}) graph - redraws itself' />")


def key_html(names: list) -> str:
    items = [f"<span style='color:{c}'>&#9632; {esc(label(n))}</span>"
             for n, c in zip(names, PALETTE)]
    return ("<div class='key'>" + " · ".join(items) + "</div>") if items else ""


def kinds_of(params: dict) -> list:
    """Which graphs to draw. No `g=` keeps the default set; `g=none` draws none.

    `g=temp`, `g=temperature`, `g=temp,hum` and `g=all` are accepted, so any
    subset from none to all is reachable.
    """
    want = params.get("g")
    if not want:
        return list(KINDS)
    alias = {}
    for kind, _u, title in KINDS:
        alias[kind] = kind
        alias[title.lower()] = kind
        alias[kind.split("_")[0]] = kind
        alias[kind[:4]] = kind
        alias[kind[:3]] = kind
    picked = []
    for part in ",".join(want).split(","):
        key = alias.get(part.strip().lower())
        if key and key not in picked:
            picked.append(key)
    return [k for k in KINDS if k[0] in picked]


def live_text(addr: str) -> str:
    """What the live panel says for one device, or why it says nothing."""
    got = LIVE.get(addr)
    if not got:
        state = LIVE_INFO.get("connected", {}).get(addr)
        if state is False:
            return "not reachable (no active session)"
        if state is True:
            return "connected, waiting for a reading"
        return "no live reading yet"
    parts = []
    for kind, _unit, _title in LIVE_KINDS:
        v = got["values"].get(kind)
        if v:
            parts.append(f"{_title.lower()} {conv(v[0], kind):.1f} "
                         f"{unit_of(kind) or v[1]}")
    stamp = show(datetime.fromisoformat(got["ts"])).strftime("%H:%M:%S")
    if not parts:
        return "connected, no reading"
    return (" · ".join(parts) + f" · as of {stamp} "
            + (TZ.tzname(datetime.fromisoformat(got["ts"])) if TZ else "UTC"))


async def live_forever(devices: dict, interval: float, turn_scan: float,
                      connect_wait: float) -> None:
    """Keep the GATT sessions open and poll the devices one at a time.

    One cycle takes the next device in the rotation and runs a short active
    scan for it, so two thermometers never compete for the adapter at once.
    """
    import thermwatch  # sits in the same folder

    sessions, connected = await thermwatch.open_sessions(
        list(devices), connect_wait)
    LIVE_INFO["connected"] = connected
    order = list(devices)
    i = 0
    LIVE_INFO["started"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    while True:
        addr = order[i % len(order)]
        i += 1
        LIVE_INFO["next"] = order[i % len(order)] if order else None
        found = await thermwatch.scan(turn_scan)
        # Whatever answered gets stored, so a scan that catches the other
        # thermometer is not thrown away; the rotation decides which one to
        # wake when nothing answers.
        for hit_addr, (dev, adv) in found.items():
            if hit_addr not in devices:
                continue
            LIVE_INFO.setdefault("connected", {})[hit_addr] = True
            decoded = thermwatch.decode(dev, adv)
            if not decoded["values"]:
                continue
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            thermwatch.store(ts, devices[hit_addr], hit_addr, decoded)
            LIVE[hit_addr] = {"ts": ts, "values": decoded["values"],
                              "model": decoded["model"]}
        await asyncio.sleep(max(interval - turn_scan, 0.5))


def start_live(devices: dict, interval: float, turn_scan: float,
               connect_wait: float) -> None:
    """Run live_forever in its own thread; the HTTP server stays single."""
    LIVE_INFO["note"] = "starting"

    def run():
        try:
            asyncio.run(live_forever(devices, interval, turn_scan,
                                     connect_wait))
        except Exception as e:            # a dead device must not kill the page
            LIVE_INFO["note"] = f"stopped: {e}"
            print(f"[live] stopped: {e}", flush=True)

    threading.Thread(target=run, name="thermview-live", daemon=True).start()


def live_status(all_count: int) -> str:
    """One line about what the poll is doing (shared by the page and /live)."""
    return (f"{LIVE_INFO['note']} · every {LIVE_INFO['interval']:.0f}s, "
            f"one device per turn · {len(LIVE)} of {all_count} with a reading")


def live_state() -> dict:
    """The live panel as data - every device is listed, so a span that went
    stale is rewritten too, not only the ones that still answer.
    """
    addrs = list(Store.addresses())
    for a in config_names():
        if a not in addrs:
            addrs.append(a)
    return {"status": live_status(len(Store.addresses())),
            "devices": {a: {"text": live_text(a),
                            "ts": LIVE.get(a, {}).get("ts", "")}
                        for a in addrs}}


LIVE_JS = ("<script>async function pull(){try{"
           "const r=await fetch('/live',{cache:'no-store'});const j=await r.json();"
           "const s=document.getElementById('lv-status');if(s)s.textContent=j.status;"
           "for(const k in j.devices){const e=document.getElementById('lv-'+k);"
           "if(e)e.textContent=j.devices[k].text;}}catch(e){}}"
           "function redraw(){for(const im of document.querySelectorAll('img.auto'))"
           "{const b=im.getAttribute('data-base');if(b)im.src=b+'&t='+Date.now();}}"
           "function tick(ms){setTimeout(function(){pull();redraw();tick(ms);},ms);}"
           "if(REFRESH>0){pull();tick(REFRESH*1000);}</script>")


# What a dropdown does when it is changed: it turns the choice into the address
# that address would have produced and goes there, so picking a window or a
# zone applies straight away instead of waiting for a submit button.
PICK_JS = ("<script>function pick(el){var b=el.getAttribute('data-base');"
           "if(!b)return;location.href=b+(el.value?encodeURIComponent(el.value):'');}"
           "function wire(){for(const s of document.querySelectorAll("
           "\"select[data-base]\"))s.onchange=()=>pick(s);}"
           "if(document.readyState=='complete')wire();"
           "else document.addEventListener('DOMContentLoaded',wire);"
           "</script>")


def live_js(seconds: float) -> str:
    """The poll script for one page, or nothing when live refresh is off.

    It does two things: it fetches a few hundred bytes of JSON and sets text in
    the live spans, and it re-asks for any graph marked `auto` - which only the
    last-hour page marks - by giving its address a fresh `t=`. A meta refresh
    would reload the whole page and redraw every graph; this leaves the menus
    alone and asks for one picture at a time.
    """
    if seconds <= 0:
        return ""
    return LIVE_JS.replace("REFRESH", f"{seconds:.0f}")


def side_html(selected: list, all_names: list, keep: dict) -> str:
    """The device menu. Clicking a device adds or drops it, keeping the rest."""
    lines = [f"<p class='m'>{len(all_names)} device(s) in the store · "
             f"selected {len(selected)} · names come from devices.json, so a "
             "rename is a label, not a new device</p>",
             f"<p class='m'>live: <span id='lv-status'>"
             f"{esc(live_status(len(all_names)))}</span></p>"]
    low_any = False
    for n in all_names:
        colour = PALETTE[all_names.index(n) % len(PALETTE)]
        on = any(norm_name(n) == norm_name(s) for s in selected)
        action = "hide" if on else "show"
        nxt = [x for x in selected if norm_name(x) != norm_name(n)] \
            if on else selected + [n]
        href = f"/?dev={_q(','.join(nxt))}&{keep}" if nxt else \
               f"/?dev=-*&{keep}"
        # A device whose battery the last poll saw at or below the mark gets a
        # name that flashes: it is the one thing the live panel says that needs
        # acting on, and a flat thermometer is about to stop answering.
        bat = LIVE.get(n, {}).get("values", {}).get("battery")
        low = bool(bat and bat[0] <= LOW_BATTERY)
        low_any = low_any or low
        lines.append(f"<p class='d'><span style='color:{colour}'>&#9632;</span>"
                     f"<a class='{'d low' if low else 'd'}' href='{href}' "
                     f"title='{action} this device'>"
                     f"{esc(label(n))} · {action}</a>"
                     f"<span class='lv' id='lv-{norm_address(n)}'>"
                     f"{esc(live_text(n))}</span></p>")
    if low_any:
        lines.append(f"<p class='m'>a flashing name is a battery at or below "
                     f"{LOW_BATTERY:.0f}% - the reading is still shown, the "
                     "device is what needs attention</p>")
        lines.append(f"<p class='d'><span style='color:{colour}'>&#9632;</span>"
                     f"<a class='d' href='{href}' title='{action} this device'>"
                     f"{esc(label(n))} · {action}</a>"
                     f"<span class='lv' id='lv-{norm_address(n)}'>"
                     f"{esc(live_text(n))}</span></p>")
    # Settings is part of the view: the range, the date and the devices you
    # had chosen go with it, so coming back lands where you were.
    lines.append(f"<p class='m'><a class='d' href='/devices"
                 f"{f'?{keep}' if keep else ''}'>settings</a></p>")
    lines.append("<p class='m'>"
                 f"<a class='d' href='/?dev=*&{keep}'>all</a> · "
                 f"<a class='d' href='/?dev=-*&{keep}'>none</a></p>")
    return "".join(lines)


def _q(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def keep_of(params: dict) -> str:
    """The other filters a link must carry so the view stays the same."""
    parts = []
    for k in ("from", "to", "date", "month", "range"):
        if params.get(k):
            parts.append(f"{k}={_q(params[k][0])}")
    return "&".join(parts)


def put_first(current: str, items: list, key=lambda v: v) -> list:
    """The dropdown's options with the one in use first.

    A browser leaves a `<select>` on its first option, so a page that is
    showing one thing and offers another opens with the dropdown already lying:
    clicking what it already shows changes nothing, and no change event fires -
    which is how a setting that is already in use looked unselectable. Starting
    the list at what is in use makes the untouched dropdown agree with the page,
    and every click a real change. `key` picks the value out of an option when
    an option is a (value, label) pair rather than a bare value.
    """
    return sorted(items, key=lambda v: key(v) != current)


def ranges_form(params: dict) -> str:
    """The window the graphs cover, as a dropdown that applies on selection.

    A `<select>` only sends to a server on submit, which is a click more than
    you want: picking a window should show it. So the page carries a two-line
    script that turns the choice into the address it would have produced -
    `/?dev=…&range=…` - and goes there. The option in use is marked, so the
    page says what it is showing. With no script the month grid beside it still
    reaches any day, so the view is reachable either way. The option in use is
    also the first one, so an untouched dropdown already shows what the page is
    showing and every click is a change the script can see.
    """
    dev = _q(params["dev"][0]) if params.get("dev") else "*"
    base = f"/?dev={dev}&range="
    cur = (params.get("range", [""])[0] or "")
    windows = [("", "all data")] + [(key, label) for key, label, _h in RANGES]
    opts = "".join(f'<option value="{key}">{esc(label)}'
                   f'{" · shown now" if key == cur else ""}</option>'
                   for key, label in put_first(cur, windows,
                                               key=lambda kv: kv[0]))
    return (f"<p class='m'>the window the graphs cover - "
            "pick one, it applies straight away</p><form>"
            f"<select class='r' data-base='{esc(base)}' "
            f"onchange='pick(this)'>{opts}</select></form>")


def aside_html(params: dict, names: list) -> str:
    """Right column: the window dropdown, then a month grid whose days set `date=`."""
    out = [ranges_form(params)]

    months = Store.months()
    want = params.get("month", [""])[0] or (months[0] if months else "")
    if not want:
        return "".join(out) + "<p class='m'>no dates yet</p>"
    y, m = (int(x) for x in want.split("-"))
    first = datetime(y, m, 1)
    nxt = datetime(y + (m == 12), (m % 12) + 1, 1)
    days = {}
    if Store.con is None:
        Store.refresh()
    try:
        days = {r[0]: r[1] for r in Store.con.execute(
            "SELECT substr(ts_utc,1,10), COUNT(DISTINCT ts_utc) "
            "FROM readings GROUP BY substr(ts_utc,1,10)")}
    except sqlite3.Error:
        pass
    out.append(f"<p class='m'>{first.strftime('%B %Y')}</p><div class='cal'>")
    labels = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")
    for i, d in enumerate(labels):
        out.append(f"<span class='hd'>{d}</span>")
    cur = first
    while cur < nxt:
        day = cur.strftime("%Y-%m-%d")
        n = days.get(day, 0)
        if n:
            out.append(f"<a class='cn' title='{n} sample(s) that day' "
                       f"href='/?date={day}&dev=*'>{cur.day}</a>")
        else:
            out.append(f"<span class='dd'>{cur.day}</span>")
        cur += timedelta(days=1)
    out.append("</div>")
    prev_m, next_m = None, None
    if months and want in months:
        i = months.index(want)
        if i + 1 < len(months):
            prev_m = months[i + 1]
        if i - 1 >= 0:
            next_m = months[i - 1]
    nav = []
    if prev_m:
        nav.append(f"<a class='d' href='/?month={prev_m}&dev=*'>"
                   f"&lsaquo; {prev_m}</a>")
    if next_m:
        nav.append(f"<a class='d' href='/?month={next_m}&dev=*'>{next_m} "
                   f"&rsaquo;</a>")
    if nav:
        out.append("<p class='m'>" + " ".join(nav) + "</p>")
    return "".join(out)


CSS = """
body{background:#0e1114;color:#c8d2dc;font-family:system-ui,sans-serif;
margin:1.2rem}
.wrap{display:flex;gap:1rem;max-width:126rem}
.side{flex:0 0 16rem;width:16rem}
.aside{flex:0 0 15rem;width:15rem}
.main{flex:1 1 0;min-width:1rem}
@media(max-width:61rem){.wrap{display:block}.aside{display:none}}
h1{font-size:1.15rem;margin:.2rem 0}
.m{color:#8b95a1;font-size:.8rem}
.d{font-size:.85rem}
a.d,a.cn{color:#7fd08a;text-decoration:none}
/* A device whose battery the last poll saw at or below LOW_BATTERY: the name
   flashes. The reading is still shown - the flashing is the attention. */
@keyframes blink{50%{opacity:.15}}
a.d.low{color:#e8b04a;font-weight:600;
        animation:blink 1.2s steps(2,jump-none) infinite}
@media(prefers-reduced-motion:reduce){a.d.low{animation:none;
        color:#e8b04a}}
/* The two dropdowns: a window and a display zone. They are real <select>s -
   the page's own script turns a change into the address that would have been
   asked for, so choosing applies without a submit button. */
select{background:#1a1f24;color:#c8d2dc;border:1px solid #2b333b;
        border-radius:.3rem;padding:.3rem;min-width:12rem;font-size:.85rem}
select.r,select.tz{margin:.4rem 0}
/* The devices screen: one block per device, the address in a <code> so it
   reads as fixed, and the form under it. Nothing is written unless the Save
   button is pressed. */
.dev{border:1px solid #2b333b;border-radius:.4rem;padding:.5rem .7rem;
      margin:.5rem 0;background:#141819}
.dev code{color:#e8b04a;font-family:monospace}
.dev input,.dev select{display:inline-block}
.rpt{color:#e8b04a;font-size:.85rem}
/* The yes of a delete: the big target, so the decision is not a small link
   that can be clicked by accident. */
.yes{display:inline-block;font-size:1rem;font-weight:600;padding:.5rem 1.1rem;
     border:#e8b04a solid 1px;border-radius:.4rem;background:#e8b04a;
     color:#12100e}
a.yes:hover{background:#f7c46a}
a.cn{display:inline-block;min-width:1.6rem}
.cal{display:grid;grid-template-columns:repeat(7,2.2rem);gap:.3rem}
.hd{color:#8b95a1;font-size:.7rem}
.dd{color:#5d6670}
.g{border:1px solid #2b333b;border-radius:.4rem}
svg.g{width:100%;height:auto}
img.g{width:100%;height:auto}
.key{font-size:.85rem;color:#c8d2dc}
"""


def series_of(params: dict) -> tuple:
    """(addresses shown, kinds asked for, range text, row count, series)."""
    all_names = Store.addresses()
    selected = pick_devices(params.get("dev"), all_names)
    frm, to, desc = span(params)
    if not selected:
        rows = []
    elif len(selected) < len(all_names):
        in_ = ", ".join(f"'{_q(n)}'" for n in selected)
        rows = Store.rows(f"AND address IN ({in_})", [frm, to])
    else:
        rows = Store.rows("", [frm, to])

    series: dict = {}
    for ts, addr, kind, value in rows:
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        series.setdefault(kind, {})
        series[kind].setdefault(addr, []).append((t, value))
    return selected, kinds_of(params), desc, len(rows), series


def graph_links(params: dict) -> str:
    """Links that switch which graphs are drawn, keeping the rest the same."""
    dev = _q(params["dev"][0]) if params.get("dev") else "*"
    keep = keep_of(params)
    items = [f"<a class='d' href='/?g={k}&dev={dev}&{keep}'>{t.lower()}</a>"
             for k, _u, t in KINDS]
    items.append(f"<a class='d' href='/?g=all&dev={dev}&{keep}'>all</a>")
    items.append(f"<a class='d' href='/?g=none&dev={dev}&{keep}'>none</a>")
    return ("<p class='m'>graphs: " + " · ".join(items) + "</p>")


def shot_links(params: dict) -> str:
    """Links that save the current view as a file on disk."""
    dev = _q(params["dev"][0]) if params.get("dev") else "*"
    keep = keep_of(params)
    g = _q(params["g"][0]) if params.get("g") else "all"
    items = [f"<a class='d' href='/shot?fmt={f}&dev={dev}&g={g}&{keep}' "
             f"title='save this view as {f.upper()}'>{f.upper()}</a>"
             for f in ("svg", "jpg")]
    items.append("<a class='d' href='/shots'>saved shots</a>")
    return "<p class='m'>save: " + " · ".join(items) + "</p>"


def page(params: dict) -> str:
    all_names = Store.addresses()
    selected, kinds, label, nrows, series = series_of(params)

    kinds = kinds_of(params)
    keep = keep_of(params)
    zone, degrees = pick_display(params)   # the header says what it draws in
    body = [
        "<h1>thermview</h1>",
        f"<p class='m'>{label} · {nrows} value row(s) · "
        f"{len(selected)} of {len(all_names)} device(s) shown · "
        f"graphs: {len(kinds)} of {len(KINDS)} · times in {zone} · "
        f"temperatures in {degrees}</p>",
        graph_links(params),
        shot_links(params),
        "<div class='wrap'><div class='side'>",
        side_html(selected, all_names, keep),
        "</div><div class='main'>",
    ]
    for kind, unit, title in kinds:
        got = series.get(kind, {})
        if not got:
            continue
        body.append(f"<h2 class='m'>{esc(title)} ({esc(unit_of(kind))})</h2>")
        body.append(key_html([n for n in selected if n in got]))
        body.append("<p class='m'>a break in a line is a period with no data, "
                    "not a flat reading</p>")
        body.append(auto_img(kind, unit, title, params)
                    if auto_view(params) else
                    graph(kind, unit, title, got, selected))
    if not nrows:
        body.append("<p class='m'>nothing in this range - widen it or pick "
                    "another date</p>")
    body += ["</div><div class='aside'>", aside_html(params, all_names),
             "</div></div>",
             PICK_JS,
             live_js(LIVE_INFO.get("refresh", 0.0))]
    return ("<!doctype html><meta charset='utf-8'>"
            f"<title>thermview</title><style>{CSS}</style>"
            + "".join(body))


def config_raw() -> dict:
    """devices.json as it is on disk (name -> address), so a write can keep
    the keys it was not asked about - including the `_note` one.
    """
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(raw: dict) -> None:
    CONFIG.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def historical_files() -> list:
    """The CSV exports waiting in historical/, newest name first."""
    if not HIST.is_dir():
        return []
    return sorted((f.name for f in HIST.iterdir()
                   if f.suffix.lower() == ".csv" and f.stat().st_size),
                  reverse=True)


def set_name(address: str, name: str) -> str:
    """Give one address a name in devices.json. The address is never touched.

    A name is only a label: the identity is the address, so renaming rewrites
    one key and the history follows it. An empty name would delete the device,
    which is not what a form box left alone means, so it writes nothing. A
    name another device already uses is refused rather than silently shared.
    """
    raw = config_raw()
    addr, name = norm_address(address), name.strip()
    if not addr or not name:
        return "an empty name would drop the device - nothing was written"
    if name.startswith("_"):
        return ("a name starting with '_' is read as a note, not a device "
                "- nothing was written")
    hit = next((n for n, a in raw.items()
                if not n.startswith("_") and norm_address(a) == addr), None)
    if hit is not None and hit == name:
        return f"{addr} is already named {name}"
    clash = raw.get(name)
    if clash is not None and norm_address(clash) != addr:
        return (f"'{name}' already names {norm_address(clash)} - one name, "
                "one device, nothing was written")
    out = {}
    for n, a in raw.items():
        out[name if n == hit else n] = a
    if hit is None:
        out[name] = address
    write_config(out)
    return (f"renamed {addr} to {name}" if hit is not None else
            f"added {name} at {addr}") + " in devices.json"


def import_history(name: str, file_name: str) -> str:
    """Store one CSV from historical/, through thermwatch's own importer.

    The same function `--import` calls, so the rows, the header map and the
    "already in the store" answer are exactly what the command line gives.
    """
    path = HIST / file_name
    if path.parent != HIST or not path.is_file():
        return f"{file_name} is not in historical/ - nothing was imported"
    import thermwatch  # sits in the same folder
    got = thermwatch.import_rows(
        path, thermwatch.load_config(thermwatch.CONFIG), name)
    return (f"{file_name} for {name}: {got['read']} data row(s) read, "
            f"{got['stored']} value row(s) stored, {got['skipped']} skipped")


def seen_by_scan(seconds: float) -> tuple[list, str]:
    """(addresses that answered, what the scan says) - how a device that is
    paired with Windows but not in devices.json is found.

    A Govee keeps no reading in an attribute value, so the only way to see one
    is an active scan it answers. This is thermwatch's own scan; it takes
    seconds and competes with the live poll, so the page runs it only when the
    find link is clicked.
    """
    try:
        import thermwatch  # sits in the same folder
        found = asyncio.run(thermwatch.scan(max(seconds, 1.0)))
    except Exception as e:            # no adapter, no winrt, a dead scan
        return [], f"the scan did not run: {e}"
    named = config_names()
    return ([(a, (d.name or "")) for a, (d, _adv) in found.items()
             if a not in named]), f"{len(found)} device(s) answered"


# How long the settings screen's scan runs when its link is clicked. A Govee
# answers an active scan only after a while; 8 s found the ones in range in
# earlier runs, and the page says so rather than pretending it is instant.
SCAN_S = 8.0


def value_counts(imported_only: bool = False) -> dict:
    """{address: value rows} - what a removal or a delete would take back.

    `imported_only` counts the rows an import wrote (`model = imported`), which
    is what the file removal can affect; without it the count is everything the
    store holds for that address, which is what a delete would lose.
    """
    Store.refresh()
    try:
        return {r[0]: r[1] for r in Store.con.execute(
            "SELECT address, COUNT(*) FROM readings "
            + ("WHERE model = 'imported' " if imported_only else "")
            + "GROUP BY address")}
    except sqlite3.Error:
        return {}


def wipe_device(address: str) -> str:
    """Delete every row of one device - only ever reached through a confirm.

    This is the blunt one: live samples and imported history alike go, and
    nothing about the device does - the name and the address stay in devices.json
    and the next sample starts a clean history. Because it cannot be put back
    from here, the settings screen does not run it: its button opens a page that
    says what would be lost and asks, and only that page's link calls this.
    """
    Store.refresh()
    if Store.con is None:
        return "no store - nothing to delete"
    try:
        cur = Store.con.execute("DELETE FROM readings WHERE address = ?",
                                (address,))
        Store.con.commit()
    except sqlite3.Error as e:
        return f"the store refused the delete: {e}"
    n = cur.rowcount
    Store.stamp = (0, 0)     # the file shrank; make the next read re-open it
    return (f"deleted {n} value row(s) for {address} - the name and the "
            "address are kept, and the history is gone")


def confirm_page(address: str, keep: str = "") -> str:
    """The step before a delete: what it would remove, and two ways out.

    A delete cannot be undone from here, so the settings screen never runs one
    on a button press: it shows this page instead. The count is in it, so the
    person deciding knows what they are agreeing to lose, and the answer is a
    link that carries the address - the decision is in the address, like every
    other link here.
    """
    counts = value_counts()
    name = config_names().get(address, "(no name)")
    n = counts.get(address, 0)
    return "".join(["<!doctype html><meta charset='utf-8'>",
            f"<title>delete all data?</title><style>{CSS}</style>",
            "<h1>delete all data for this device?</h1>",
            f"<p class='m'>{esc(name)} <code>{address}</code></p>",
            f"<p class='rpt'><b>{n} value row(s) would be deleted</b> - every "
            "reading this store holds for that address, live samples and "
            "imported history alike. The name and the address stay. "
            "<b>It cannot be put back from here.</b></p>",
            f"<p class='m'><a class='yes' href='/devices/confirm?"
            f"address={esc(address)}'>yes, delete all {n} rows</a> · "
            f"<a class='d' href='/devices{f'?{keep}' if keep else ''}'>"
            "no, go back to settings</a></p>"])
    Store.refresh()
    try:
        return {r[0]: r[1] for r in Store.con.execute(
            "SELECT address, COUNT(*) FROM readings GROUP BY address")}
    except sqlite3.Error:
        return {}


def remove_history(name: str, file_name: str) -> str:
    """Take back the rows one historical file added - the inverse of an import.

    This is for the mistake the upload makes: the wrong export chosen for a
    device. The file is read exactly as the import reads it, and the rows it
    would write are deleted instead, so undoing needs the file, not a memory
    of which file it was. Only rows an import wrote (`model = imported`) are
    touched, so a live sample that shares a timestamp survives, and the count
    is reported because this is the one place the store loses data.
    """
    path = HIST / file_name
    if path.parent != HIST or not path.is_file():
        return f"{file_name} is not in historical/ - nothing was removed"
    import thermwatch  # sits in the same folder
    got = thermwatch.import_rows(
        path, thermwatch.load_config(thermwatch.CONFIG), name, "remove")
    Store.stamp = (0, 0)   # the file shrank; make the next read re-open it
    return (f"{file_name} for {name}: {got['read']} data row(s) read, "
            f"{got['removed']} value row(s) taken back, "
            f"{got['skipped']} skipped")


def apply_device(fields: dict) -> list:
    """Do what one form asked for, and say what happened.

    A delete is not done here: `post` sends it to `confirm_page` first, so the
    only path to `wipe_device` is the link on that page.
    """
    addr = norm_address(fields.get("address", ""))
    name = (fields.get("name") or "").strip()
    file_name = (fields.get("file") or "").strip()
    if not addr:
        return ["no address in the form - nothing was written"]
    notes = []
    if name:
        notes.append(set_name(addr, name))
    chosen = name or config_names().get(addr, addr)
    remove = any(v.strip() for v in fields.get("remove", []))
    if file_name:
        notes.append(remove_history(chosen, file_name) if remove else
                     import_history(chosen, file_name))
    elif remove:
        notes.append("no file chosen - a removal takes back one file's rows, "
                     "so it needs the file")
    if not notes:
        notes.append("nothing was asked for - the name is unchanged and no "
                     "CSV was chosen")
    return notes


def unit_form() -> str:
    """The dropdown the settings screen offers for the temperature unit.

    Same shape as the window and the zone: choosing applies straight away, and
    the choice is saved in display.json. Only temperature converts - humidity,
    battery and signal are not temperatures.
    """
    cur = display_doc().get("unit", "C")
    cur = "F" if str(cur).upper().startswith("F") else "C"
    opts = "".join(f'<option value="{u}">'
                   f"{'°C' if u == 'C' else '°F'}"
                   f'{" · in use" if u == cur else ""}</option>'
                   for u in put_first(cur, ["C", "F"]))
    return (f"<p class='m'>temperatures are drawn in - pick one, it applies "
            "straight away</p><form>"
            "<select class='u' data-base='/devices?unit=' "
            "onchange='pick(this)'>" + opts + "</select></form>")


def zone_form() -> str:
    """The dropdown the settings screen offers for the display zone.

    Same shape as the window dropdown: choosing applies straight away, through
    `/devices?tz=<name>`, which saves the choice in display.json and redraws
    this screen - so the setting is not one click behind a Save button.
    """
    cur = str(display_doc().get("tz", "UTC"))
    opts = "".join(f'<option value="{z}">{z}'
                   f'{" · in use" if z == cur else ""}</option>'
                   for z in put_first(cur, list(ZONES)))
    return (f"<p class='m'>the graphs and the live readings are shown in - "
            "pick one, it applies straight away</p><form>"
            f"<select class='tz' data-base='/devices?tz=' "
            f"onchange='pick(this)'>{opts}</select></form>")


def devices_page(params: dict, report: list = ()) -> str:
    """The devices screen.

    Every device known anywhere is listed: named in devices.json, present in
    the store without a name, or answered a scan when the page was asked for
    one (`/devices?scan=8`). The address is shown in a `<code>` and is never
    editable - it is the identity, and the history follows it. Under each one
    is a form: a name box, a dropdown of the CSVs in historical/ and a Save
    button. Pressing it writes devices.json and, when a file was chosen,
    stores that file for that device; the page comes back with what happened,
    so the next look shows the new name.

    The same form has a second button, **remove this file's data**: it takes
    back exactly the rows the file in the dropdown would add, which is what
    "we uploaded the wrong export for this device" needs. Only rows an import
    wrote are removed, so live samples are never caught, and the count beside
    the device says how many of its rows came from a file. Nothing else about
    the device is touched - the name and the address stay.

    A third button, **delete all data for this device**, is the blunt one, so
    it does not delete: it opens a page that says how many rows would go and
    asks. Only that page's "yes, delete all N rows" link removes anything.
    """
    cfg = config_names()
    stored = Store.addresses()
    files = historical_files()
    if params.get("tz"):
        name = (params["tz"][0] or "").strip()
        got = save_zone(name)
        report = list(report) + [
            (f"the display zone is now {got} - every page shows times in it"
             if got else
             f"'{name}' has no zone data here - the choice was not saved")]
    if params.get("unit"):
        got = save_unit(params["unit"][0])
        report = list(report) + [
            (f"temperatures are now drawn in {got} - the store still keeps "
             "°C, nothing was converted in it"
             if got else
             f"'{params['unit'][0]}' is not a unit - the choice was not saved")]
    pick_display(params)
    # The view you came from is part of this screen: the range, the date and
    # the devices chosen are carried so "back to the graphs" lands where you
    # were, and a form answer shows the same screen with the same filters.
    keep = keep_of(params)
    back = f"/{f'?{keep}' if keep else ''}"
    seen, note = ([], "no scan run - click find one for that")
    try:
        seconds = float(params.get("scan", ["0"])[0])
    except ValueError:
        seconds = 0.0
    if seconds > 0:
        seen, note = seen_by_scan(seconds)
    order = list(dict.fromkeys(
        list(cfg) + [a for a in stored if a not in cfg]
        + [a for a, _n in seen if a not in cfg]))
    # The value is what the browser sends back, so it is HTML-quoted only:
    # a URL-quoted `Greenhouse%20thermometer…` would be submitted as that
    # literal, and the real file is `Greenhouse thermometer…`. The browser
    # form-encodes the raw name itself, and parse_qs undoes that.
    options = "".join(f"<option value='{esc(f)}'>{esc(f)}</option>"
                      for f in files)
    counts = value_counts(True)
    out = ["<!doctype html><meta charset='utf-8'>",
           f"<title>settings</title><style>{CSS}</style>",
           "<h1>settings</h1>",
           "<p class='m'>the <b>address is the identity</b> - it is shown and "
           "never changed here. A name is a label in devices.json, so a "
           "rename moves the history, it does not create a device. "
           f"{len(cfg)} named, {len(stored)} in the store, "
           f"{len(files)} CSV(s) in historical/</p>",
           f"<p class='m'>the graphs come back to: <b>{esc(span(params)[2])}"
           "</b> - the range you had, kept.</p>",
           "<p class='m'><b>removing takes back the rows one historical file "
           "added</b> - choose that file in the dropdown and press remove. "
           "Live samples are never touched, the name and the address stay, and "
           "what comes back is only what the file did not have. "
           "<b>Deleting all data</b> is the blunt one: it asks for "
           "confirmation first, because it cannot be put back.</p>",
           f"<p class='m'>{esc(note)}</p>",
           f"<p class='m'><a class='d' href='/devices?scan={SCAN_S:.0f}"
           f"{f'&{keep}' if keep else ''}'>scan "
           f"{SCAN_S:.0f}s for a device that is paired but not listed</a></p>",
           zone_form(), unit_form()]
    for r in report:
        out.append(f"<p class='rpt'>{esc(r)}</p>")
    for addr in order:
        name = cfg.get(addr, "")
        where = ("named in devices.json" if name else
                 "in the store, not named" if addr in stored else
                 "answered the scan, not named")
        windows = dict(seen).get(addr, "")
        found = (f"<p class='m'>Windows calls it "
                 f"<code>{esc(windows)}</code></p>" if windows else "")
        held = counts.get(addr, 0)
        out.append(
            f"<div class='dev'><p class='m'>{esc(name or '(no name)')} "
            f"<code>{addr}</code> · {where} · "
            f"{held} value row(s) came from a historical file</p>{found}"
            f"<form method='post' action='/devices'>"
            f"<input type='hidden' name='address' value='{esc(addr)}'>"
            f"<input type='hidden' name='keep' value='{esc(keep)}'>"
            f"<label>name <input type='text' name='name' "
            f"value='{esc(name)}' style='width:12rem'></label>"
            f"<label>historical CSV <select name='file'>"
            f"<option value=''></option>{options}</select></label>"
            f"<input type='submit' name='save' value='save this device'>"
            f"<input type='submit' name='remove' "
            f"value='remove this file's data'>"
            f"<input type='submit' name='delete' "
            f"value='delete all data for this device'>"
            "</form></div>")
    if not order:
        out.append("<p class='m'>no device is named anywhere - add one by "
                   "name and address in devices.json, or click find one</p>")
    out.append(f"<p class='m'><a class='d' href='/devices{f'?{keep}' if keep else ''}'>this screen "
               f"without a scan</a> · <a class='d' href='{back}'>back to the "
               "graphs</a></p>")
    return "".join(out) + PICK_JS


class _Handler:
    """What a request shows. Kept separate from the HTTP layer so the tests
    can exercise it without a server.
    """

    def route(self, path: str) -> str:
        Store.refresh()
        route, params = parse(path)
        if route.rstrip("/") == "/devices/confirm":
            addr = norm_address(params.get("address", [""])[0])
            if not addr:
                return confirm_page("")
            return devices_page({}, [wipe_device(addr)])
        if route.rstrip("/") == "/devices":
            return devices_page(params)
        return page(params)

    def post(self, body: str) -> str:
        """Apply one device form and answer with the screen as it now is.

        Except a delete: that one goes to the confirm page, which asks before
        anything is lost.
        """
        fields = {k: v[0] for k, v in parse_qs(body).items()}
        keep = fields.get("keep", "")
        _base, params = parse("/" + (f"?{keep}" if keep else ""))
        if any(v.strip() for v in fields.get("delete", [])):
            return confirm_page(norm_address(fields.get("address", "")), keep)
        notes = apply_device(fields)
        Store.refresh()
        return devices_page(params, notes)

    def raw(self, path: str) -> tuple[str, bytes]:
        """(content type, bytes) for the shot routes and one graph image."""
        route, params = parse(path)
        route = route.lstrip("/")
        if route == "graph":
            return "image/svg+xml", graph_svg(params, params.get("k", [""])[0])
        if route == "shot":
            fmt = params.get("fmt", ["svg"])[0]
            name, data, ctype = shot_file(params, fmt)
            note = (f"<!doctype html><meta charset='utf-8'>"
                    f"<title>saved</title><p>Wrote "
                    f"<code>{esc(str(SHOTS / name))}</code> "
                    f"({len(data)} bytes) into this project.</p>"
                    f"<p><a href='/shots/{esc(name)}'>view it</a> · "
                    f"<a href='/shots'>all saved shots</a></p>")
            return "text/html; charset=utf-8", note.encode("utf-8")
        if route == "shots":
            files = sorted(SHOTS.iterdir()) if SHOTS.exists() else []
            items = "".join(
                f"<li><a href='/shots/{esc(f.name)}'>{esc(f.name)}</a> "
                f"({f.stat().st_size} bytes)</li>" for f in files)
            note = ("<!doctype html><meta charset='utf-8'><title>saved "
                    "shots</title><p class='m'>shots saved by thermview</p>"
                    f"<ul>{items}</ul>")
            return "text/html; charset=utf-8", note.encode("utf-8")
        if route.startswith("shots/"):
            name = path.split("?", 1)[0].split("/", 2)[2]
            f = SHOTS / name
            if f.parent == SHOTS and f.exists():
                ctype = ("image/svg+xml" if f.suffix == ".svg"
                         else "image/jpeg" if f.suffix in (".jpg", ".jpeg")
                         else "text/plain")
                return ctype, f.read_bytes()
        return "text/plain", b"not a shot"


Handler = _Handler()


class Request(BaseHTTPRequestHandler):

    def log_message(self, format, *args) -> None:
        """The access line without httpd's ` - - `.

        Those two dashes are placeholders for the remote ident and the response
        size that httpd never fills. The address, the time and the request are
        what says which server and which page, so the line is written here
        without them - same control-character escaping as the default.
        """
        message = format % args
        sys.stderr.write("%s [%s] %s\n"
                         % (self.address_string(),
                            self.log_date_time_string(),
                            message.translate(self._control_char_table)))
    """The HTTP layer. Everything it decides is which page `Handler` renders.

    The server must stay single-threaded (`HTTPServer`, not
    `ThreadingHTTPServer`): one sqlite connection may only be used from the
    thread that created it (MEM-0026), and a threaded server hands every
    request to a fresh thread, which makes the store reads come back empty.
    """

    def do_GET(self):
        route, _ = parse(self.path)
        route = route.lstrip("/")
        if route == "favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if route == "live":
            ctype = "application/json"
            data = json.dumps(live_state()).encode("utf-8")
        elif route in ("shot", "shots", "graph") or route.startswith("shots/"):
            ctype, data = Handler.raw(self.path)
        else:
            ctype = "text/html; charset=utf-8"
            data = Handler.route(self.path).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError):
            pass    # the browser hung up before reading the body - not an error

    def do_POST(self):
        """One device form: a name, a CSV from historical/, a Save button.

        Only `/devices` takes a form, so only `/devices` posts here. Writing
        devices.json and storing a CSV are the two things the button may do,
        and the answer is the screen again with what happened above it.
        """
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace")
            data = Handler.post(body).encode("utf-8")
        except (ValueError, OSError) as e:
            data = f"<p class='rpt'>{e}</p>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (ConnectionAbortedError, BrokenPipeError):
            pass


class Prefixed:
    """Put the same tag in front of every line this tool writes.

    The streams are wrapped rather than every `print` rewritten: the access log
    the request handler produces goes to stderr, and the scan and connect lines
    come from `thermwatch`, which this imports - covering both without touching
    a single print in either is what keeps the two tools' logs readable.
    """

    def __init__(self, stream, tag: str) -> None:
        self.stream, self.tag, self.pending = stream, f"[{tag}] ", ""

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.stream.write(self.tag + line + "\n")
        return len(text)

    def flush(self) -> None:
        if self.pending:
            self.stream.write(self.tag + self.pending + "\n")
            self.pending = ""

    def isatty(self) -> bool:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="serve the thermwatch store")
    ap.add_argument("--port", type=int, default=8957)
    ap.add_argument("--live-interval", type=float, default=60.0,
                    help="seconds between polls, one device per turn "
                         "(0 keeps the viewer offline)")
    ap.add_argument("--turn-scan", type=float, default=3.0,
                    help="seconds of active scan per device")
    ap.add_argument("--connect-wait", type=float, default=45.0,
                    help="seconds to wait for each GATT session to reach ACTIVE")
    ap.add_argument("--live-refresh", type=float, default=5.0,
                    help="seconds the open page waits before it re-reads the "
                         "live panel in place (0 = only on a manual reload)")
    ap.add_argument("--tag", default=None,
                    help="put this label in front of every log line "
                         "(default: the folder this tool sits in)")
    args = ap.parse_args()
    tag = args.tag or Path(__file__).resolve().parent.name
    sys.stdout = Prefixed(sys.stdout, tag)
    sys.stderr = Prefixed(sys.stderr, tag)
    devices = config_names()
    LIVE_INFO["refresh"] = args.live_refresh
    if args.live_interval > 0 and devices:
        LIVE_INFO["interval"] = args.live_interval
        start_live(devices, args.live_interval, args.turn_scan,
                   args.connect_wait)
    else:
        LIVE_INFO["note"] = "off (--live-interval 0)"
    srv = HTTPServer(("127.0.0.1", args.port), Request)
    print(f"thermview on http://127.0.0.1:{args.port}/ - Ctrl+C stops it",
          flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

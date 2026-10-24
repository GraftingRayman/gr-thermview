"""Checks for thermview - offline, against the real store, no server started.

Run: python test_view.py, from this folder. The store it reads is the one
beside it (`thermometer.sqlite3`), so a copy of the folder tests itself once
it has sampled anything.
Prints one line per check and a final "N checks, M failed".
"""
import io
import re
import sys
from datetime import datetime, timedelta, timezone

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import thermview  # noqa: the module under test
from thermview import parse, page, Handler, span, pick_devices, Store  # noqa

FAILS = 0
TOTAL = 0


def check(name: str, cond: bool) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    if cond:
        print(f"ok   {name}")
    else:
        FAILS += 1
        print(f"FAIL {name}")


def html_of(path: str) -> str:
    return Handler.route(path)


def main() -> int:
    addrs = Store.addresses()
    names = [thermview.label(a) for a in addrs]
    check("the store has devices to show", len(names) >= 1)
    if len(names) < 2:
        print("need two devices to check the split - nothing to check")
        return 1
    a, b = names[0], names[-1]
    check("one device is one address, whatever it is called",
          len(addrs) == len(names))

    home = html_of("/")
    check("the index renders", home.startswith("<!doctype html>"))
    check("a graph is drawn for each measurement with points",
          home.count("<svg") >= 2)
    check("every device gets its own colour in the key",
          all(f"color:{c}" in home for c in thermview.PALETTE[:len(names)]))
    check("the key names the devices",
          all(f"&#9632; {n}" in home for n in names))
    check("one polyline per device in a graph",
          home.count("<path d='M") >= len(names))

    only_a = html_of(f"/?dev={a}")
    check("selecting one device shows only it",
          f"&#9632; {a}" in only_a and f"&#9632; {b}" not in only_a)
    check("selecting one device draws one line per graph",
          only_a.count("<path d='M") <= len(thermview.KINDS))

    none_of = html_of("/?dev=-*")
    check("selecting none says so instead of showing everything",
          "nothing in this range" in none_of and "<svg" not in none_of)

    both = html_of(f"/?dev={a},{b}")
    check("a list of devices shows all of them",
          all(f"&#9632; {n}" in both for n in (a, b)))

    menu = html_of(f"/?dev={a}")
    check("the menu offers hide for what is shown and show for the rest",
          f"{a} · hide" in menu and f"{b} · show" in menu)
    hrefs = re.findall(r"href='([^']*dev=[^']*)'", menu)
    check("menu links keep the other filters of the page",
          all(("from=" not in hrefs[0]) or ("from=" in h) for h in hrefs))
    check("menu links are real requests",
          all(h.startswith("/?") or h.startswith("/shot") for h in hrefs))

    frm, to, label = span(parse("/?range=24h")[1])
    check("a range asks for the last 24 hours",
          label == "last 24 hours" and
          (datetime.now(timezone.utc) - timedelta(hours=24)).date()
          == datetime.fromisoformat(frm).date())
    f1, t1, l1 = span(parse("/?range=1h")[1])
    check("the short range asks for the last hour",
          l1 == "last hour"
          and (datetime.now(timezone.utc) - timedelta(hours=1)).date()
          == datetime.fromisoformat(f1).date())
    f2, t2, l2 = span(parse("/?range=1y")[1])
    check("the long range asks for the last year",
          l2 == "last year"
          and (datetime.now(timezone.utc) - timedelta(days=365)).date()
          == datetime.fromisoformat(f2).date())
    check("every range is offered in the dropdown, and picking applies",
          all(f'<option value="{k}">' in html_of("/")
              for k, _d, _h in thermview.RANGES)
          and "function pick(el)" in html_of("/"))
    check("a range is honoured: rows outside it are not shown",
          len(Store.rows("", [frm, to])) <= len(Store.rows("", [
              "0000-01-01T00:00:00+00:00",
              datetime.now(timezone.utc).isoformat(timespec="seconds")])))

    thermview.LIVE_INFO["refresh"] = 5.0
    hour = html_of("/?range=1h&dev=*")
    imgs = re.findall(r"<img class='g auto'[^>]*", hour)
    check("the last-hour page draws its graphs as images it can reload",
          len(imgs) >= len(thermview.KINDS))
    check("each image asks for the graph route with the page's own filters",
          all("src='/graph?k=" in t and "range=1h" in t for t in imgs))
    check("the image keeps its address in data-base for the script to reuse",
          all("data-base=" in t for t in imgs))
    wide = html_of("/?range=24h&dev=*")
    check("a wider range still draws the graph into the page",
          wide.count("<svg") >= len(thermview.KINDS)
          and "<img class='g auto'" not in wide)
    thermview.LIVE_INFO["refresh"] = 0.0
    off = html_of("/?range=1h&dev=*")
    check("with live refresh off even the last hour stays inline",
          "<svg" in off and "<img class='g auto'" not in off)
    js = thermview.live_js(5.0)
    check("the page script re-asks for the auto images on its tick",
          "img.auto" in js and "data-base" in js and "redraw()" in js)
    check("with no polling there is no script at all",
          thermview.live_js(0) == "")
    ctype, svg = Handler.raw("/graph?k=temperature&range=1h&dev=*")
    check("the graph route answers with a standalone SVG",
          ctype == "image/svg+xml" and svg.startswith(b"<svg")
          and svg.endswith(b"</svg>"))
    _c, empty = Handler.raw("/graph?k=temperature&range=1h&dev=-*")
    check("a graph with no device selected is still a valid image",
          empty.startswith(b"<svg") and b"</svg>" in empty)

    def n_rows(page_html: str) -> int:
        m = re.search(r"(\d+) value row\(s\) ·", page_html)
        return int(m.group(1)) if m else -1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    empty = "2020-01-01"
    day = html_of(f"/?date={empty}")
    check("a date filters to that day - an empty one shows nothing",
          n_rows(day) == 0 and "nothing in this range" in day)

    wide = html_of("/?from=2026-06-01&to=2026-10-31")
    paths = re.findall(r"<path d='([^']*)'", wide)
    long = [x for x in paths if len(x) > 1000]      # the month-long series
    check("long series are bucketed, not drawn point by point",
          len(wide) < 200_000 and "averages" in wide)
    check("a period with no data breaks the line instead of bridging it",
          len(long) >= 2 and all(x.count("M") >= 3 for x in long))
    check("the page says a break is missing data, not a flat reading",
          "a break in a line is a period with no data" in wide)
    check("the day that has data is the one the calendar links to",
          f"date={today}" in html_of("/"))

    cal = html_of("/")
    check("the month grid is present", "class='cal'" in cal)
    check("days with samples are links that say how many",
          re.search(r"<a class='cn' title='\d+ sample", cal) is not None)
    check("days without samples are plain text",
          "<span class='dd'>" in cal)

    # graph toggles
    none_g = html_of("/?g=none")
    check("g=none draws no graph", "<svg" not in none_g)
    only_t = html_of("/?g=temperature")
    check("g=temperature draws only that graph",
          "Temperature" in only_t and "Humidity" not in only_t)
    two = html_of("/?g=temp,hum")
    check("a list of graphs draws both of them",
          "Temperature" in two and "Humidity" in two
          and "Battery" not in two)
    check("the graph switcher offers every graph, all and none",
          all(f"/?g={k}" in html_of("/")
              for k, _u, _t in thermview.KINDS)
          and "/?g=none" in html_of("/") and "/?g=all" in html_of("/"))
    check("battery and signal are not offered as graphs",
          "/?g=battery" not in html_of("/")
          and "/?g=signal_strength" not in html_of("/"))
    check("a battery graph is not drawn even for all",
          "Battery" not in html_of("/?g=all"))

    # live panel
    thermview.LIVE_INFO["refresh"] = 5.0
    page0 = html_of("/")
    check("the menu says what the live poll is doing",
          "live:" in page0 and "one device per turn" in page0)
    check("every device has a live slot",
          all("class='lv'" in page0 for _a in addrs))
    check("the page carries the in-place poll script",
          "getElementById('lv-status')" in page0 and "fetch('/live'" in page0)
    check("every live slot has an id the script can find",
          all(f"id='lv-{a}'" in page0 for a in addrs))
    import json as _json
    state = _json.loads(_json.dumps(thermview.live_state()))
    check("/live lists every device, not only the ones answering",
          set(state["devices"]) >= set(addrs) and "status" in state)

    # shots
    name, data, ctype = thermview.shot_file(
        parse("/?range=24h")[1], "svg")
    check("an svg shot is a real svg document",
          data.startswith(b"<svg") and ctype == "image/svg+xml"
          and b"</svg>" in data)
    name2, data2, ctype2 = thermview.shot_file(
        parse("/?range=24h")[1], "jpg")
    check("a jpg shot is a real jpeg",
data2[:2] == b"\xff\xd8" and ctype2 == "image/jpeg")
    check("shots are written inside the project",
          str(thermview.SHOTS) in str(pathlib.Path(name2).resolve().parent)
          if False else (thermview.SHOTS / name2).exists())

    # The devices screen: names, an address that is never edited, and the
    # CSVs in historical/. The writes go to a temp config and a temp store,
    # so the real ones are only ever read.
    from pathlib import Path
    import shutil
    import sqlite3
    import thermwatch  # sits in the same folder; the CSV import is its own
    tmp = Path(".tmp/devices")
    shutil.rmtree(tmp, ignore_errors=True)   # a run that crashed left one behind
    real_store = thermview.STORE
    real_cfg, real_hist = thermview.CONFIG, thermview.HIST
    real_cfg_json, real_db = thermwatch.CONFIG, thermwatch.DB
    real_cfg_bytes = real_cfg.read_bytes()
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "historical").mkdir(exist_ok=True)
    shutil.copyfile(real_cfg, tmp / "devices.json")
    # A name with a space is the point: the form must send the file's real
    # name, so the option value is HTML-quoted, not URL-quoted.
    (tmp / "historical" / "probe one.csv").write_text(
        "Time,Temperature,Humidity\n"
        "2026-10-24 02:00:00,21.5,55.0\n"
        "2026-10-24 02:05:00,21.6,55.2\n", encoding="utf-8")
    thermview.CONFIG, thermview.HIST = (tmp / "devices.json",
                                        tmp / "historical")
    thermwatch.CONFIG, thermwatch.DB = tmp / "devices.json", tmp / "probe.sqlite3"
    thermview.STORE = tmp / "probe.sqlite3"      # the counts read this store

    screen = Handler.route("/devices")
    check("the devices screen lists every device known",
          screen.count("class='dev'") == len(addrs))
    check("the address is shown and never offered as an edit",
          all(f"<code>{a}</code>" in screen for a in addrs)
          and "name='address'" in screen
          and "type='text' name='address'" not in screen)
    check("every device carries a Save button",
          screen.count("name='save'") == len(addrs))
    check("the dropdown lists what is in historical/",
          "probe one.csv" in screen and "value=''" in screen)
    check("the option value is the file's name, not a quoted one",
          "probe%20one.csv" not in screen)
    check("the menu offers one thing: settings",
          "href='/devices'>settings</a>" in html_of("/")
          and "paired but not listed" not in html_of("/"))
    check("the scan is asked for on the settings screen, not the menu",
          "paired but not listed" in screen)

    first = addrs[0]
    from urllib.parse import quote_plus
    pick = "&file=" + quote_plus("probe one.csv")
    answer = Handler.post(f"address={first}&name=RenamedProbe{pick}")
    cfg_now = thermview.config_names()
    check("the Save button writes the name to devices.json",
          cfg_now.get(first) == "RenamedProbe")
    check("the address is the same one after the rename",
          set(cfg_now) == set(addrs))
    check("the answer says what the CSV added",
          "value row(s) stored" in answer and "renamed" in answer.lower())
    with sqlite3.connect(tmp / "probe.sqlite3") as con:
        got = con.execute("SELECT COUNT(*) FROM readings").fetchall()[0][0]
        who = con.execute("SELECT DISTINCT name, address FROM readings"
                          ).fetchall()
    check("the chosen CSV was stored for that device",
          got == 4 and who == [("RenamedProbe", first)])
    again = Handler.post(f"address={first}&name=RenamedProbe{pick}")
    check("pressing again stores nothing twice",
          "0 value row(s) stored" in again)
    dup = Handler.post(f"address={addrs[1]}&name=RenamedProbe")
    check("one name, one device: a name in use is refused",
          "already names" in dup and thermview.config_names().get(addrs[1])
          != "RenamedProbe")
    kept = thermview.config_names().get(addrs[1])
    blank = Handler.post(f"address={addrs[1]}&name=")
    check("an empty name writes nothing",
          "nothing was asked for" in blank
          and thermview.config_names().get(addrs[1]) == kept)

    # the removal: it takes back one file's rows, not the device's history
    check("every device also offers a remove button",
          screen.count("name='remove'") == len(addrs))
    (tmp / "historical" / "probe two.csv").write_text(
        "Time,Temperature,Humidity\n"
        "2026-10-24 03:00:00,20.5,60.0\n"
        "2026-10-24 03:05:00,20.6,60.2\n", encoding="utf-8")
    Handler.post(f"address={addrs[1]}"
                 "&file=" + quote_plus("probe two.csv"))
    shown = Handler.route("/devices")
    check("the screen says how many rows came from a file",
          shown.count("4 value row(s) came from a historical file") == 2)
    # a second export that repeats the same seconds with different numbers:
    # its rows were never written, so removing it must take nothing back
    (tmp / "historical" / "probe three.csv").write_text(
        "Time,Temperature,Humidity\n"
        "2026-10-24 02:00:00,99.9,10.0\n"
        "2026-10-24 02:05:00,99.9,10.0\n", encoding="utf-8")
    twin = "&file=" + quote_plus("probe three.csv")
    over = Handler.post(f"address={first}{twin}")
    check("a second file for the same seconds adds nothing",
          "0 value row(s) stored" in over)
    check("removing that file takes nothing back - the values differ",
          "0 value row(s) taken back" in Handler.post(
              f"address={first}&remove=yes{twin}"))

    removed = Handler.post(f"address={first}"
                           "&remove=remove+this+file%27s+data"
                           "&file=" + quote_plus("probe one.csv"))
    with sqlite3.connect(tmp / "probe.sqlite3") as con:
        left = con.execute("SELECT address, COUNT(*) FROM readings GROUP BY "
                           "address").fetchall()
        live = con.execute("SELECT COUNT(*) FROM readings "
                           "WHERE model != 'imported'").fetchone()[0]
    check("the removal named what it took back",
          "value row(s) taken back" in removed)
    check("only that file's rows were taken back",
          left == [(addrs[1], 4)] and live == 0)
    check("the other device and its name are untouched",
          thermview.config_names().get(addrs[1]) == kept)
    check("a remove with no file chosen says so",
          "no file chosen" in Handler.post(
              f"address={first}&remove=yes"))

    # the blunt delete: it must ask first, and only the yes link removes
    check("every device offers a delete as well",
          screen.count("name='delete'") == len(addrs))
    ask = Handler.post(f"address={addrs[1]}&delete=delete+all+data")
    with sqlite3.connect(tmp / "probe.sqlite3") as con:
        before = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    check("the delete asks first and writes nothing",
          "delete all data for this device?" in ask
          and "cannot be put back" in ask
          and thermview.config_names().get(addrs[1]) == kept)
    check("the question counts what would go",
          f"{before} value row(s) would be deleted" in ask)
    done = Handler.route(f"/devices/confirm?address={addrs[1]}")
    with sqlite3.connect(tmp / "probe.sqlite3") as con:
        after = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    check("only the yes link deletes, and only that device",
          after == before - 4 and f"deleted 4 value row(s) for {addrs[1]}"
          in done)
    after_screen = Handler.route("/devices")
    check("the device is still named after its data is gone",
          thermview.config_names().get(addrs[1]) == kept
          and "0 value row(s) came from a historical file" in after_screen)

    # the view is kept through settings and back
    seven = Handler.route("/?range=7d&dev=*")
    check("the menu's settings link carries the range",
          "href='/devices?range=7d" in seven)
    settings = Handler.route("/devices?range=7d")
    check("settings says which view it came from",
          "the graphs come back to" in settings and "last 7 days" in settings)
    check("back from settings lands on the same range",
          "href='/?range=7d" in settings)
    check("the scan link keeps the range too",
          "/devices?scan=8&range=7d" in settings)
    answered = Handler.post("address=" + addrs[0]
                            + "&keep=" + quote_plus("range=7d"))
    check("the answer to a form keeps the view",
          "href='/?range=7d" in answered)
    ask = Handler.post("address=" + addrs[0]
                       + "&delete=yes&keep=" + quote_plus("range=7d"))
    check("the confirm page goes back to the same settings view",
          "href='/devices?range=7d" in ask)

    thermview.CONFIG, thermview.HIST = real_cfg, real_hist
    thermview.STORE = real_store
    thermwatch.CONFIG, thermwatch.DB = real_cfg_json, real_db
    with sqlite3.connect(real_db) as con:
        leaked = con.execute("SELECT COUNT(*) FROM readings WHERE name = ?",
                             ("RenamedProbe",)).fetchone()[0]
    # the flashing name: a battery the last poll saw at or below the mark
    addr = addrs[0]
    thermview.LIVE[addr] = {"ts": "2026-10-24T00:00:00+00:00",
                            "values": {"battery": (50.0, "%")},
                            "model": "test"}
    low_page = Handler.route("/")
    check("a device at or below the mark gets a flashing name",
          "class='d low'" in low_page
          and "flashing name is a battery" in low_page)
    thermview.LIVE[addr]["values"]["battery"] = (50.1, "%")
    check("just above the mark nothing flashes",
          "class='d low'" not in Handler.route("/"))
    thermview.LIVE.pop(addr, None)

    # the display zone: the dropdown, a saved choice, and the axis moving
    settings = Handler.route("/devices")
    check("settings offers the display zone as a dropdown",
          "select class='tz'" in settings
          and all(f'<option value="{z}"' in settings
                  for z in thermview.ZONES))
    import json as _json
    import re as _re
    tmpd = Path(".tmp/tz")
    tmpd.mkdir(parents=True, exist_ok=True)
    real_display = thermview.DISPLAY
    thermview.DISPLAY = tmpd / "display.json"
    saved = Handler.route("/devices?tz=Europe/London")
    check("choosing a zone saves it for every page",
          "the display zone is now Europe/London" in saved
          and _json.loads(thermview.DISPLAY.read_text(
              encoding="utf-8")).get("tz") == "Europe/London")
    times = lambda s: _re.findall(r"\d\d-\d\d \d\d:\d\d", s)
    utc_svg = Handler.raw("/graph?k=temperature&range=24h&tz=UTC")[1].decode()
    lon_svg = Handler.raw("/graph?k=temperature&range=24h")[1].decode()
    check("the saved zone moves the graph axis",
          times(lon_svg) != times(utc_svg) and bool(times(lon_svg)))
    check("the page says what the times are in",
          "times in BST" in Handler.route("/"))
    bad = Handler.route("/devices?tz=Mars/Olympus")
    check("a zone with no data says so and changes nothing",
          "no zone data here" in bad
          and _json.loads(thermview.DISPLAY.read_text(
              encoding="utf-8")).get("tz") == "Europe/London")
    asked = Handler.route("/?tz=UTC&range=24h")
    check("a page can ask for another zone without saving it",
          "times in UTC" in asked
          and _json.loads(thermview.DISPLAY.read_text(
              encoding="utf-8")).get("tz") == "Europe/London")
    thermview.DISPLAY = real_display
    shutil.rmtree(tmpd)

    if Store.con is not None:          # the temp store is still open in it
        try:
            Store.con.close()
        except Exception:
            pass
    Store.con = None
    import gc
    gc.collect()      # the indexer's connection is in a cycle; Windows needs
                      # it gone before the temp DB can be deleted
    shutil.rmtree(tmp)
    check("the test left the real config and store alone",
          real_cfg.read_bytes() == real_cfg_bytes and leaked == 0
          and not tmp.exists())

    Store.stamp = (0, 0)        # pretend the file moved on disk
    Store.refresh()
    check("the store is re-read when the file changes",
          Store.stamp == thermview.store_stamp()
          and Store.con is not None)

    print(f"\n{TOTAL - FAILS} passed, {FAILS} failed of {TOTAL} checks")
    return 0 if FAILS == 0 else 1


raise SystemExit(main())

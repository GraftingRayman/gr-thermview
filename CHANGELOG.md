# Changelog

Everything here was verified against the store or the page before it was
written down. Dates are the day the change was made on this machine.

## 2026-10-24 — a window dropdown, a flashing low battery, a display zone

- **The right column's ranges became a dropdown** — last hour, 24 h, 7 d, 30 d,
  last year, all data — and picking one applies straight away: the page carries a
  three-line script that turns the choice into the address it would have produced
  (`/?dev=…&range=…`) and goes there. No submit button, and the option in use is
  marked `· shown now`, so the page says what it is showing.
- **A device whose battery the last poll saw at or below 50 % has a name that
  flashes** (amber, with a line saying why). The reading is still shown normally;
  the flashing is the attention, because a flat thermometer is about to stop
  answering. With `prefers-reduced-motion` the colour stays and the animation is
  off. Only a device with a live reading can flash — nothing is inferred from
  stored history.
- **Times can be shown in a zone you choose.** The settings screen offers a
  dropdown of common ones; picking it saves `display.json` next to the scripts
  (gitignored — a fresh clone starts on UTC) and applies immediately, so the
  **graph axis labels** and the **live readings'** timestamps move to it and the
  page header says what it is doing (`times in BST`). `?tz=<name>` on any page
  asks for one zone for that view without saving it, so a shared link shows the
  same times. A name the system has no data for is refused and said so rather
  than shown wrong.
- **The store still keeps UTC, and the calendar days and range windows are still
  UTC.** A zone only changes how a time is written — no data moves and no filter
  breaks. Making "a day" mean a local day would be a different, bigger change.

## 2026-10-24 — the settings screen: name a device, undo a wrong upload

The tool grew a screen of its own after the repo copy was made; this folder was
refreshed with it.

- **`/devices`** lists every device known anywhere — named in `devices.json`, in
  the store without a name, or answered an active scan when the page is asked for
  one. The Bluetooth address is shown and never editable, because it is the
  identity; the name is a label and the history follows the address.
- **Save this device** writes `devices.json` (one key, matched by address, every
  other key kept) and, when a CSV from `historical/` was chosen in the dropdown,
  stores it through the same importer `--import` uses. An empty name writes
  nothing; a name another device uses is refused.
- **Remove this file's data** is the inverse of an import: the file is read the
  same way and the rows it would have written are deleted — but only the ones that
  match **identically** (timestamp, address, key, value and unit) and that an
  import wrote (`model = imported`). A real reading at the same second, or a
  second export repeating a timestamp with different numbers, is never caught.
- **Delete all data for this device** does not delete on the press: a page says
  how many value rows would go and asks; only its yes link removes them. The
  name and the address always stay.
- The left column now offers one thing — **settings** — and the scan is asked for
  on that screen, so the menu is not a list of half-finished sentences.
- `historical/` is a folder in the repo (kept by its `README.md`); the CSVs you
  put in it are ignored, since they are data.

## 2026-10-24 — the repo copy

- The tool was lifted out of the project it grew in and put in its own folder,
  so it can be a repository of its own. Nothing in the two scripts changed
  behaviour; the paths they use are all relative to the script's own folder
  (`Path(__file__).resolve().parent`), so the copy runs where it stands.
- `requirements.txt`, `.gitignore`, `.gitattributes` and this file were added;
  the store, the reports and the saved shots are now ignored rather than
  committed, since they are generated on every run.

## 2026-10-23 — the last-hour view redraws its own graphs

- A `/graph` route answers one graph as a standalone SVG document, built from
  the same primitives the page draws.
- On the **last hour** view only, each graph is drawn as `<img class='g auto'
  src='/graph?…'>` and the page's own poll script gives its address a fresh
  `t=` on every tick, so the browser re-asks for the picture and the server
  re-reads the store. Every other range keeps inline SVG — a wider window does
  not move on by itself. With `--live-refresh 0` even the last hour goes back
  to inline SVG.
- Honest limit, still true: the viewer's own live polls are **not stored**, so
  the graph moves when a sample is written, not on every poll.

## 2026-10-23 — two more thermometers, from the phone app's CSV exports

- `--import <csv> --device <name>` stores a CSV the Govee app exported, so a
  thermometer that never came within range of this PC still has history.
- A device's identity is its **Bluetooth address**; a device with no address
  (its data came from a CSV) is keyed by a stable `csv_<name>` instead, and
  `--sync-names` keeps stored names in line with `devices.json`.

## 2026-10-23 — live polling in the viewer, at one minute

- `--live-interval` default is 60 s; one device per turn, so two thermometers
  never compete for the adapter at once.

## 2026-10-22 — the viewer

- `thermview.py` serves the store in the shape of the other viewers: devices
  left (with their live readings), graphs middle (temperature and humidity,
  switchable with `g=`), dates right (ranges and a month grid).
- The open page updates itself: a small script re-fetches `/live` and writes
  the new text into the live spans by element id — not a meta refresh.
- `/shot?fmt=svg|jpg` saves the current view into `shots/`; both formats draw
  the same primitives, so the JPEG is the graph, not a browser screenshot.
- The server is deliberately **single-threaded**: one sqlite connection may
  only be used from the thread that created it.

## 2026-10-22 — the sampler

- `thermwatch.py` connects the paired Govee H5075s over Windows' Bluetooth
  stack, active-scans for their encrypted advertisements, decodes them with
  `govee-ble` and stores one row per sample and value in `thermometer.sqlite3`.
- `--list`, `--range`, `--history`, `--config` and the connect/scan timings
  exist because the device keeps no reading in an attribute value and a short
  scan misses it.

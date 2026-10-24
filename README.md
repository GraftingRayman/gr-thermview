# gr-thermview

Reads the Govee thermometers paired to a Windows PC over Bluetooth, stores the
readings in a SQLite file, and serves them as graphs you can browse by device
and by date. One sampler run = one sample, so it is meant to be scheduled
(hourly, so); the viewer can poll on its own while it runs.

Windows-only: the Bluetooth stack it talks to is the Windows one (via `bleak`'s
WinRT backend).

```
thermwatch.bat                      sample every device in devices.json
thermwatch.bat --list               show what the scan sees, store nothing
thermwatch.bat --scan-seconds 120   a longer scan
thermwatch.bat --no-connect         scan only, for a device already connected
thermwatch.bat --range              rank what is in range by signal strength
thermwatch.bat --range-seconds 60   a longer range probe
thermview.bat                       serve the store at http://127.0.0.1:8957/
```

## Start here

1. **Install the dependencies** — see `requirements.txt`, pinned to what this
   was built with:
   ```
   python -m pip install -r requirements.txt
   ```
2. **Name your thermometers** in `devices.json` — `name -> Bluetooth address`.
   `--list` prints the addresses a scan sees, so a new device can be spotted
   before it is given a name.
3. **Sample once** (`thermwatch.bat`). Expect it to take a couple of minutes per
   device: the connect and the active scan are the bulk of it, and a device that
   is idle or out of range stores nothing — that is a normal exit, not an error.
4. **Look at it** (`thermview.bat`, then open the address it prints).
5. **Schedule it.** The tool is one sample per run on purpose; a task scheduler
   or a loop calling `thermwatch.bat` every hour is what fills the archive.

## What is in this folder

| Path | In git? | What |
| --- | --- | --- |
| `thermwatch.py` | yes | the sampler: connect, active-scan, decode, store |
| `thermview.py` | yes | the viewer: devices left, graphs middle, dates right, settings |
| `test_view.py` | yes | offline checks for the viewer, no server and no browser |
| `devices.json` | yes | `name -> Bluetooth address` — **your** config, edit it |
| `thermwatch.bat`, `thermview.bat` | yes | Windows launchers for the two scripts |
| `historical/README.md` | yes | the folder where the phone app's CSV exports go |
| `README.md`, `CHANGELOG.md` | yes | this document and its history |
| `requirements.txt`, `.gitattributes` | yes | pinned dependencies, line endings |
| `thermometer.sqlite3` | **no** | the store — generated, ~190 MB here, grows every run |
| `last_run.md`, `range.md` | **no** | the report of the most recent run |
| `shots/` | **no** | views the viewer saved |

Nothing here is a package: the three scripts are run as files, and every path
they use is relative to the folder they sit in (`Path(__file__).resolve().parent`),
so the folder works wherever you clone it.

## Tests

```
python test_view.py        # from this folder; needs a store beside it
```

It prints one line per check and a final total. It reads the store next to it and
starts no server, so it needs no browser and no Bluetooth. With **no** store
beside it it says so and stops - one `FAIL` line, then `need two devices to
check the split - nothing to check` - rather than pretending a fresh clone
passes. Running it writes two shots into `shots/` (gitignored).

**79 checks, 0 failed** — verified 2026-10-24 against a 1.15 M-row store.

## `--range`: does a spot work, and how solidly

It connects the configured devices, runs one active scan, and reports per device:

- **answered N times** - the device advertised during the scan, so it is in range
  and reachable from that spot;
- **connected** - whether a GATT session reached ACTIVE. This is what *wakes an
  idle device* so it starts advertising; it is **not** what the reading needs.
  Readings come from advertisements, so a device can report "did NOT connect"
  and still store data - verified with OUTSIDE just outside the house, which
  never reached ACTIVE in 45 s yet stored 12.3 °C / 80.6 %.
- the reported signal in dBm, **informational only**.

Why not metres: the signal Windows reports for these devices swings enormously.
Home, at a fixed 4 m, read −44, −51, −56, −66 and −71 dBm across five runs - a
27 dB spread, which at any plausible dB-per-doubling is a factor of twenty in
 distance. So the honest test for placing one of your other thermometers is
 binary: run `--range` from where you're considering, and if it appears and
 connects, that spot works. If it doesn't appear, move it closer or move the
 dongle (a short USB extension off the back of the tower buys more than any
 amount of tuning here).

## What it does, in order

1. **Connect** each configured device. A paired Bluetooth LE device is idle and
   unreachable, and Windows' Settings has no "Connect" for it - the connect is
   opening a GATT session with `maintain_connection`, which completes
   asynchronously, so the run waits for the session to reach ACTIVE (it took
   ~8-9 s for this device).
2. **Active-scan** for advertisements. The H5075 does not keep its reading in
   an attribute value; it sends it as an encrypted advertisement, which only an
   active scan picks up. A 30 s scan missed the device, 75 s found it, so the
   default is 90 s.
3. **Decode** with `govee-ble` into temperature, humidity, battery and signal
   strength. Units come from the decoder.

## Files

| Path | What |
| --- | --- |
| `devices.json` | `name -> Bluetooth address`. Add a device by adding its address; `--list` prints the addresses the scan sees. |
| `thermometer.sqlite3` | `readings` table: one row per sample and value (`ts_utc, name, address, key, value, unit, model`). |
| `last_run.md` | The report for the most recent sample run. |
| `range.md` | The report for the most recent `--range` probe. |

Devices that are idle or out of range are reported as "Not seen" and store
nothing; that is a normal exit (code 0), not an error.

## Naming

A device's **identity is its Bluetooth address**; the name in `devices.json` is only a label. Renaming
one does not create a second device:

- `thermview` reads `devices.json` on every request, so a new name shows up without restarting it;
- `thermwatch.bat --sync-names` rewrites the names already stored in the SQLite file to match the
  config, matched by address (it reports how many rows it changed and is safe to run again);
- the graphs, the key and the left menu all use the current label, so history follows the rename.

Each device still gets its own name in `devices.json` - `"Home"` is the one inside the house, `"Outside"`
the one outside. Anything the scan finds that is not in the config is shown as `unmapped <address>` and
is not stored, so a new thermometer can be spotted before it is given a name.

## Viewing the data

`thermview.bat` serves the store at `http://127.0.0.1:8957/` - a page with the
devices on the left, the graphs in the middle and the dates on the right - plus
one live thing: while it runs it keeps the GATT sessions open
and polls the thermometers, so the current reading is shown next to each device and
the new samples go into the same store the graphs read.

| Column | What |
| --- | --- |
| Left | the devices. Clicking one **adds or drops it** from the selection while keeping the others, so any subset from none to all is reachable; `all` and `none` links do the obvious thing. Next to each name is its **live reading** - temperature, humidity, battery and signal strength with the time it was taken - or why there is none (`not reachable (no active session)` / `connected, waiting for a reading`). Above the list, one line says how the poll is running. |
| Middle | the graphs - **temperature and humidity only** - and links that switch which of them are drawn: each on its own, both, or `none`. Below them, `save: SVG · JPG · saved shots`. Battery and signal strength are read, stored and shown in the live panel, but they are not drawn as graphs. |
| Right | a **dropdown for the window** the graphs cover — last hour, 24 h, 7 d, 30 d, last year, all data — and a month grid whose days link to that day; days with samples say how many, empty days are plain text. Picking a window applies straight away, no submit button: the page's own script turns the choice into the address it would have produced (`/?dev=…&range=…`) and goes there. The option in use is marked `· shown now`, so the page says what it is showing. |

The store is re-read when the file changes on disk, so a scheduled `thermwatch`
run shows up without restarting the viewer - and so does the viewer's own polling.

URL parameters, if you want to link straight to a view:

```
/                       all devices, all data
/?dev=Home              one device          /?dev=Home,OUTSIDE   a list
/?dev=-*                none                /?dev=-OUTSIDE       all but one
/?range=1h | 24h | 7d | 30d | 1y          fixed ranges (the dropdown picks these)
/?tz=Europe/London                       show times in that zone (any IANA name)
/?unit=F                                 draw temperatures in °F (or °C)
/?date=YYYY-MM-DD                         one day
/?from=2026-10-01&to=2026-10-31           a span
/?month=YYYY-MM                          which month the grid shows
/?g=none                                   draw no graph
/?g=temperature | temp,hum | all           only these graphs (temperature/humidity only)
/live                                      the live panel as JSON
/devices                                   settings: names, CSVs, remove, delete
/devices?scan=8                            settings after an 8 s active scan
/devices/confirm?address=<address>         yes, delete everything for that device
/graph?k=temperature&range=1h&dev=*        one graph as an SVG document
/shot?fmt=svg&range=24h&g=temp,hum         save this view as a file
/shot?fmt=jpg                              ... as a real JPEG
/shots                                     what has been saved, /shots/<name> views it
```

A shot is written to `shots/` (ignored by git) with a name like
`2026-10-22T221811Z-temperature-humidity.jpg`: a header block naming the range,
the devices and the graphs, then one block per graph. Both formats draw the same
primitives, so the JPEG is the graph, not a screenshot of a browser.

Live polling settings (defaults in brackets):

```
thermview.bat --live-interval 60    # seconds between polls [60]; 0 turns it off
thermview.bat --turn-scan 3         # seconds of active scan per poll [3]
thermview.bat --connect-wait 45     # seconds to wait for each session [45]
thermview.bat --live-refresh 5      # seconds the open page waits before it
                                    # re-reads the live panel in place [5]; 0 = manual
```

**Every line in the console carries a tag** — `[GR-ThermView]`, because the two
`.bat` launchers in this folder pass `--tag GR-ThermView`. Run the scripts
straight and the default is the folder they sit in. The access log, the sampler's
`[scan]`/`[connect]` lines and the store-reload notices all get it: the two
streams are wrapped, not the messages rewritten, which is what covers a line the
request handler writes to stderr and a line that comes from the other file.

The page you have open **updates itself**: a small script re-fetches `/live` (a few
hundred bytes of JSON) and writes the new text into the live spans by element id.
It is not a meta refresh — the menu, the date grid and the graphs stay as they
are, and nothing is re-rendered. `/live` lists every device, configured or in the
store, so a span that went stale is rewritten too rather than left showing an old
reading.

**On the last-hour view the graphs update themselves too.** That page draws each
graph as an image (`<img class='g auto' src='/graph?k=…&range=1h'>`) instead of
inline SVG, and the same script gives its address a fresh `t=` on every tick, so
the browser asks for the picture again and the server re-reads the store. Only
that range does it: a wider window does not move on by itself, so re-drawing it
every few seconds would ask for the same picture over and over. `/graph` answers
with a standalone SVG document — the same primitives the page draws, no title
inside, since the heading is already above it. With `--live-refresh 0` even the
last-hour page goes back to inline SVG.

What the graph can show is what is **in the store**: the viewer's own live polls
are not stored, so the last-hour graph moves when `thermwatch.bat` writes a
sample, not on every poll. Plotting the live readings as the newest point is not
done.

## The settings screen (`/devices`)

The left column ends with one link: **settings**. The scan is asked for on that
screen, not in the menu — clicking it runs an 8 s active scan, so it is slow on
purpose and only happens when you choose it.

The screen lists every device known **anywhere**: named in `devices.json`, present
in the store without a name, or answered a scan. Each one is a block with its
address in a `<code>` — **shown, never editable**, because the address is the
identity and the history follows it — and under it a form: a name box, a dropdown
of the CSVs in `historical/`, and **three buttons**.

- **Save this device** writes `devices.json` and, when a file was chosen, stores
  that file for that device through the same importer `--import` uses. An empty
  name box writes nothing, a name another device already uses is refused, and a
  name starting with `_` is kept as a note rather than a device.
- **Remove this file's data** takes back exactly the rows that file would add —
  the inverse of an import, for the mistake it makes. A row is only taken back
  when it matches **identically**: timestamp, address, key, value and unit must
  all agree, and only rows an import wrote (`model = imported`) are eligible. So
  a real reading at the same second, or a second export that repeats a timestamp
  with different numbers, is never caught. The count beside each device says how
  many of its rows came from a file.
- **Delete all data for this device** is the blunt one, and it does not delete on
  the press: it opens a page that names the device, says **how many value rows
  would go**, and says they cannot be put back. Only that page's
  `yes, delete all N rows` link removes anything; `no, go back to settings`
  leaves the store alone. The name and the address always stay.

`historical/` holds the CSV exports the phone app made, one file per thermometer;
its `README.md` says the same thing from the folder. The files are data, not part
of the tool, and `historical/*.csv` is ignored — the folder is kept in the repo by
its README, so the feature has somewhere to put files.

Each poll cycle takes the next device in the rotation and runs one short active
scan, so two thermometers never compete for the adapter at once; whatever answers
gets stored, so a scan that catches the other one is not thrown away. A device
whose session never reached ACTIVE is reported as not reachable rather than
silently missing.

The server is deliberately **single-threaded**: one sqlite connection may only be
used from the thread that created it, and a threaded server hands every request to
a fresh thread, which made the store read back empty. The live poller runs in its
own thread and opens its own connections, which is allowed; the HTTP thread never
uses them.

`test_view.py` runs the checks offline against the store beside it - 79 checks,
0 failed as of 2026-10-24 (it also writes two shots into `shots/`, and its
settings checks run against a temp `devices.json` and a temp store, so a real
one is never rewritten by the test).

## Units and the display zone

**Temperatures can be drawn in °C or °F.** The settings screen offers a
**dropdown** for it, and picking applies straight away: the **graph axis, the
graph heading, the live readings and the saved shots** all show the chosen unit,
and the page header says what it is doing (`temperatures in °F`). Humidity, the
battery and the signal are not temperatures, so they are never converted. **The
store keeps °C and always will** — a unit only decides how a number is written,
so changing it converts nothing in the dump and breaks no filter. The choice is
saved in `display.json` next to the scripts (gitignored — a fresh clone starts on
°C); `?unit=F` on any page asks for one view without saving it, so a shared link
shows the same degrees whoever opens it. A unit that is not C or F is refused and
left at what it was.

Every dropdown on the page — window, unit, zone — lists **the option in use first**,
marked `· in use` / `· shown now`. A browser leaves a `<select>` on its first
option, so an untouched dropdown would otherwise sit on a different setting than
the page is showing, and clicking what it already shows fires nothing — that is
why a setting that was already in use looked unselectable.

The same screen picks the **time zone** for the graph axis and the live
readings' timestamps (`?tz=<name>` for one view). The store keeps UTC, and the
calendar days and the range windows are still UTC — only the display moves. A
zone the system has no data for is refused and said so rather than shown wrong.

## A battery that needs attention

When the last poll saw a device's battery at or below **50 %**, its name in the
left menu **flashes** (amber, and a legend line says why). The reading is still
shown normally — the flashing is the attention, and a flat thermometer is about to
stop answering. With `prefers-reduced-motion` the colour stays and the animation
is off.

## Dependencies

Pinned in `requirements.txt` to the versions this was built and verified with:
`bleak` 3.0.2, `govee-ble` 1.5.2, `home-assistant-bluetooth` 2.0.0, and
`Pillow` 12.0.0 - which only the JPEG shots need, everything else is standard
library. `bleak` pulls the `winrt-*` Windows Bluetooth projections in by itself
(they are declared dependencies of it); 3.2.1 is what is installed here.

## What this cannot do

- **A device's own history is not reachable.** `--history` asks the device for
  its past (up to 20 days) and the call is accepted, but the H5075 answers with
  nothing on this PC - verified, not assumed. The archive is filled by the
  scheduled samples and by `--import <csv> --device <name>`, which stores a CSV
  the phone app exported.
- **The viewer's live polls are not stored**, so the last-hour graph moves when
  a sample is written, not on every poll. Plotting the live reading as the
  newest point is not done.
- **Battery and signal strength are stored and shown live, but never graphed.**
  Only temperature and humidity are drawn.

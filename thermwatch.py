"""thermwatch - read the Govee thermometers paired to this PC and store the data.

One run = one sample, so it is meant to be scheduled (hourly, so). A run that
finds nothing exits 0: an idle or out-of-range thermometer is normal, not a
failure.

What each step is for (all verified on this machine, 2026-10-22):

1. Connect. A paired Bluetooth LE device is unreachable while it is idle, and
   Windows' Settings has no "Connect" for it - the connect is opening a GATT
   session with `maintain_connection`, which completes asynchronously, so we
   wait for the session to reach ACTIVE. Observed times were 4-28 s, so the
   default wait is 45 s; a device that never reaches ACTIVE is out of reach.
2. Scan. The reading is not a stored attribute value - the H5075 sends it as an
   encrypted BLE advertisement, which only an **active** scan picks up
   (`govee-ble` marks this model `requires_active_scan`).
3. Decode. `govee-ble` turns that payload into temperature, humidity, battery.

Files, all in this folder:
    devices.json          name -> Bluetooth address; edit this to add devices
    thermometer.sqlite3   one row per sample and value
    last_run.md           the report for the last run

Usage:
    thermwatch.py                     sample everything in devices.json
    thermwatch.py --list              show what the scan sees, store nothing
    thermwatch.py --scan-seconds 90   a longer scan (the default)
    thermwatch.py --no-connect        scan only (device already connected)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakScanner
from home_assistant_bluetooth import BluetoothServiceInfoBleak
from govee_ble import GoveeBluetoothDeviceData

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "devices.json"
DB = HERE / "thermometer.sqlite3"
REPORT = HERE / "last_run.md"
RANGE = HERE / "range.md"

# --range measures how far each device is by the signal strength Windows reports
# with its advertisements. The signal-strength FILTER does not gate the events
# (verified: a device below the threshold is still reported, with -127 dBm as an
# "unknown" sentinel), so the probe collects raw RSSI samples and keeps the best
# one per device. RSSI is noisy for the same device, so best-of-N is the fair
# comparison; every ~6 dB below the best is roughly double the distance.
RANGE_SAMPLES_S = 30.0

# What we keep from a decoded device; units come from the decoder, not from us.
KEEP = ("temperature", "humidity", "battery", "signal_strength")


def norm_address(value: str) -> str:
    """`A4:C1:38:77:92:8A`, `a4c13877928a` and `0xa4c1...` are one address."""
    return value.removeprefix("0x").replace(":", "").replace("-", "").lower()


def load_config(path: Path) -> dict:
    """devices.json -> {address: name}. Keys starting with "_" are not devices."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {norm_address(a): n for n, a in raw.items()
            if not n.startswith("_")}


async def open_sessions(addresses: list[str], wait_s: float) -> tuple[list, dict]:
    """Try to connect each address. Returns (live sessions, {address: connected}).

    The connection only lives while this process holds the session object, so the
    sessions must stay referenced for the whole run.
    """
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattSession,
            GattSessionStatus,
        )
    except ImportError:
        print("[connect] winrt is not installed - scanning without connecting")
        return [], {}

    async def one(addr: str):
        try:
            number = int(addr, 16)
        except ValueError:
            # A CSV-only entry (a device whose data came from an exported file,
            # not from this radio) has no Bluetooth route; saying so keeps the
            # scheduled runs working instead of crashing over it.
            print(f"[connect] {addr} is a CSV-only entry - no Bluetooth route")
            return None, False
        dev = await BluetoothLEDevice.from_bluetooth_address_async(number)
        if dev is None:
            print(f"[connect] {addr} is not paired - nothing to connect")
            return None, False
        print(f"[connect] {dev.name!r} connection_status={dev.connection_status}")
        session = await GattSession.from_device_id_async(dev.bluetooth_device_id)
        session.maintain_connection = True
        waited = 0.0
        while waited < wait_s:
            await asyncio.sleep(1.0)
            waited += 1.0
            if session.session_status == GattSessionStatus.ACTIVE:
                print(f"[connect] session ACTIVE after {waited:.0f}s")
                return session, True
        print(f"[connect] session never reached ACTIVE in {waited:.0f}s")
        return session, False

    sessions, connected = [], {}
    for a in addresses:
        got = await one(a)
        sessions.append(got[0])
        connected[a] = got[1]
    return [x for x in sessions if x], connected


async def scan(seconds: float) -> dict[str, tuple]:
    """Active scan; return {address: (device, advertisement_data)}."""
    print(f"[scan] active scan for {seconds:.0f}s")
    found = await BleakScanner.discover(
        timeout=seconds, scanning_mode="active", return_adv=True)
    out = {}
    for dev, adv in found.values():
        out[norm_address(dev.address)] = (dev, adv)
    print(f"[scan] {len(out)} device(s) answered")
    return out


def decode(dev, adv) -> dict:
    """One advertisement -> {'values': {key: (value, unit)}, 'model': ...}."""
    info = BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        device=dev,
        advertisement_data=adv,
        source="thermwatch",
        time=asyncio.get_running_loop().time(),
        connectable=True,
    )
    upd = GoveeBluetoothDeviceData().update(info)
    values = {}
    for device_key, sensor in upd.entity_values.items():
        if device_key.key not in KEEP:
            continue
        desc = upd.entity_descriptions.get(device_key)
        unit = desc.native_unit_of_measurement.value if desc else ""
        values[device_key.key] = (sensor.native_value, unit)
    model = next((d.model for d in upd.devices.values()), None)
    return {"values": values, "model": model}


def store(ts_utc: str, name: str, address: str, decoded: dict) -> int:
    """Add one row per value. A repeat within the same second is skipped, not
    duplicated - the unique index is what makes that possible.
    """
    with sqlite3.connect(DB) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS readings "
            "(ts_utc, name, address, key, value, unit, model)"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS sample "
            "ON readings (ts_utc, name, address, key)"
        )
        added = 0
        for key, (value, unit) in decoded["values"].items():
            try:
                con.execute(
                    "INSERT INTO readings (ts_utc, name, address, key, value, unit,"
                    "model) VALUES (?,?,?,?,?,?,?)",
                    (ts_utc, name, address, key, value, unit, decoded["model"]),
                )
                added += 1
            except sqlite3.IntegrityError:  # same second, same device, same key
                continue
        return added


def clean_header(value: str) -> str:
    """`Start Time (oC)` -> `starttimeoc`, so column names match loosely."""
    return "".join(c for c in value.lower() if c.isalnum())


def to_float(value: str):
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def parse_time(value: str):
    """An ISO-ish or common app timestamp -> UTC ISO string, or None."""
    text = str(value).strip().replace("–", "-")
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M",
                "%Y-%m-%d"):
        try:
            when = datetime.strptime(text, fmt)
            return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        except ValueError:
            continue
    if text.isdigit() and len(text) >= 9:                      # epoch seconds
        return datetime.fromtimestamp(int(text), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00")
    return None


def import_rows(path: Path, devices: dict, device_name: str,
                mode: str = "store") -> dict:
    """Store, or take back, a CSV the phone app exported.

    The header is matched loosely, values are sanity-checked, and anything that
    does not fit is counted and reported rather than guessed.

    `mode="remove"` is the inverse of an import: the file is read exactly the
    same way and the rows it would write are deleted instead - and only the ones
    that match **identically** (timestamp, address, key, value and unit) and that
    an import wrote (`model = imported`). A device may hold a real reading for the
    same second, and a second export may repeat a timestamp the first one already
    stored with a different number; neither is this file's row, so neither is
    removed. This is what "we uploaded the wrong historical file for this device"
    needs: undoing needs the file, not a memory of which file it was.
    """
    address = next((a for a, n in devices.items()
                    if n.lower() == device_name.lower()), None)
    if address is None and len(norm_address(device_name)) == 12:
        address = norm_address(device_name)
        device_name = next((n for a, n in devices.items()
                            if norm_address(a) == address), device_name)
    if address is None:
        print(f"[import] {device_name!r} is neither a configured name nor a "
              "12-hex address - refusing to invent one")
        return {"read": 0, "stored": 0, "skipped": 0}

    aliases = {"time": ("time", "date", "timestamp"),
               "temperature": ("temp", "temperature"),
               "humidity": ("humidity", "hum", "rh"),
               "battery": ("battery", "bat")}
    want = {k: None for k in aliases}
    read = stored = skipped = 0
    pending = []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            print("[import] empty file")
            return {"read": 0, "stored": 0, "skipped": 0}
        norm = [clean_header(h) for h in header]
        for i, h in enumerate(norm):
            for key, words in aliases.items():
                if want[key] is None and any(w in h for w in words):
                    want[key] = i
                    break
        print("[import] header map: " +
              ", ".join(f"{k}={v}" for k, v in want.items() if v is not None))
        if want["time"] is None or want["temperature"] is None:
            print("[import] no time/temperature column - refusing; send the "
                  "first lines if the names differ")
            return {"read": 0, "stored": 0, "skipped": 0}

        limits = {"temperature": (-60, 70), "humidity": (0, 100),
                  "battery": (0, 100)}
        units = {"temperature": "C", "humidity": "%", "battery": "%"}
        for row in reader:
            if not row or all(not c.strip() for c in row):
                continue
            read += 1
            ts = parse_time(row[want["time"]]) if want["time"] < len(row) else None
            if ts is None:
                skipped += 1
                continue
            values = {}
            for key, idx in want.items():
                if idx is None or key == "time" or idx >= len(row):
                    continue
                v = to_float(row[idx])
                if v is None:
                    continue
                lo, hi = limits[key]
                if not lo <= v <= hi:
                    continue
                values[key] = (v, units[key])
            if not values:
                skipped += 1
                continue
            pending.extend((ts, device_name, address, key, v, u, "imported")
                           for key, (v, u) in values.items())
    if pending:
        with sqlite3.connect(DB) as con:
            con.execute("CREATE TABLE IF NOT EXISTS readings "
                        "(ts_utc, name, address, key, value, unit, model)")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS sample "
                        "ON readings (ts_utc, name, address, key)")
            if mode == "remove":
                # The file is read the same way, so the rows it would have
                # written are exactly the ones taken back - **identically**: the
                # timestamp, the address, the key, the value and the unit all
                # have to agree. A device may hold a real reading for the same
                # second, and a second export may repeat a timestamp the first
                # one already stored with a different number; neither is this
                # file's row, so neither is removed. `model = imported` is the
                # second guard: only rows an import wrote are eligible.
                gone = 0
                for ts, _name, addr, key, val, unit, _m in pending:
                    gone += con.execute(
                        "DELETE FROM readings WHERE model = 'imported' "
                        "AND ts_utc = ? AND address = ? AND key = ? "
                        "AND value = ? AND unit = ?",
                        (ts, addr, key, val, unit)).rowcount
                con.commit()
                print(f"[remove] {read} data row(s) read, {gone} value row(s) "
                      f"deleted ({len(pending) - gone} were not in the store), "
                      f"{skipped} skipped")
                return {"read": read, "stored": 0, "skipped": skipped,
                        "removed": gone}
            have = {r for r in con.execute(
                "SELECT ts_utc, name, address, key FROM readings")}
            fresh = [row for row in pending
                     if row[:4] not in have]
            con.executemany(
                "INSERT INTO readings (ts_utc, name, address, key, value, unit,"
                "model) VALUES (?,?,?,?,?,?,?)", fresh)
            con.commit()
            stored = len(fresh)
    print(f"[import] {read} data row(s) read, {stored} value rows stored "
          f"({len(pending) - stored} already in the store), {skipped} skipped")
    return {"read": read, "stored": stored, "skipped": skipped}


UUID_CMD = "494e5445-4c4c-495f-524f-434b535f2012"   # the control slot
UUID_TX = "494e5445-4c4c-495f-524f-434b535f2013"    # records arrive here
MAX_HISTORY_MIN = 28800                              # the device answers 20 days


def spp_packet(command: bytes, params: bytes = b"") -> bytes:
    """A 20-byte SPP-over-GATT request: command, params, zero pad, XOR check."""
    body = bytearray(command + params)
    body.extend(bytes(19 - len(body)))
    check = 0
    for b in body:
        check ^= b
    return bytes(body) + bytes([check])


def decode_record(three: bytes) -> tuple[float, float] | None:
    """Three bytes -> (°C, %RH). High bit marks a negative temperature."""
    raw = int.from_bytes(three, "big")
    if raw == 0xFFFFFF:
        return None
    negative = bool(raw & 0x800000)
    if negative:
        raw ^= 0x800000
    temp = int(raw / 1000) / 10.0
    if negative:
        temp = -temp
    return temp, (raw % 1000) / 10.0


async def history(address: str, minutes: int) -> list:
    """Ask the device for its own history: (utc_iso, temp, humidity) records.

    The device keeps 20 days at 1-minute resolution. Records arrive as
    notifications on the transmit slot, six per message, each stamped with how
    many minutes back the first one is.
    """
    from bleak import BleakClient, BleakError
    from bleak.backends.device import BLEDevice

    out: list = []
    state = {"started": False, "done": False, "count": 0}

    def on_records(_sender, data):
        data = bytes(data)
        if len(data) < 3:
            return
        back = int.from_bytes(data[0:2], "big")
        for i in range(6):
            rec = decode_record(data[2 + 3 * i:5 + 3 * i])
            if rec is None:
                continue
            when = datetime.now(timezone.utc) - timedelta(minutes=back - i)
            out.append((when.strftime("%Y-%m-%dT%H:%M:%S+00:00"), *rec))
        state["count"] += 1

    def on_control(_sender, data):
        data = bytes(data)
        if data[:2] == b"\x33\x01":
            state["started"] = True
        elif data[:2] == b"\xee\x01":
            state["done"] = True
            if len(data) >= 4:
                state["expect"] = int.from_bytes(data[2:4], "big")

    client = BleakClient(BLEDevice(address=address, name="", details={}),
                         timeout=40.0)
    async with client:
        await client.start_notify(UUID_TX, on_records)
        await client.start_notify(UUID_CMD, on_control)
        start = min(max(minutes, 1), MAX_HISTORY_MIN)
        await client.write_gatt_char(UUID_CMD,
                                     spp_packet(b"3",
                                                start.to_bytes(2, "big")
                                                + bytes(2)),
                                     response=True)
        waited = 0.0
        while waited < 120.0:
            await asyncio.sleep(0.5)
            waited += 0.5
            if state["done"]:
                break
            if state["count"] and state.get("expect") and                state["count"] >= state["expect"]:
                break
        try:
            await client.stop_notify(UUID_TX)
            await client.stop_notify(UUID_CMD)
        except BleakError:
            pass    # the device had already dropped the connection
    return out


async def store_history(devices: dict, minutes: int) -> None:
    """Pull each configured device's own history and store it."""
    for addr in devices:
        recs = await history(addr, minutes)
        print(f"[history] {addr}: {len(recs)} records from the device")
        for ts, t, h in recs:
            store(ts, devices[addr], addr,
                  {"values": {"temperature": (t, "C"),
                              "humidity": (h, "%")},
                   "model": "device-history"})


def sync_names(devices: dict) -> int:
    """Bring the store in line with the config, matched by identity.

    The address is the device's identity and a name is only a label, so a rename
    in devices.json would otherwise split its history in two. The reverse also
    happens: a device whose data came from an exported CSV has no Bluetooth
    address, so its rows were stored under a placeholder like `csv_greenhouse`,
    and when the config later names a real address for that device the history
    belongs to it. Both directions are one row update each. Returns the number
    of rows changed.
    """
    if not DB.exists():
        return 0

    def is_address(value: str) -> bool:
        return len(value) == 12 and all(c in "0123456789abcdef"
                                       for c in value.lower())

    changed = 0
    with sqlite3.connect(DB) as con:
        pairs = con.execute(
            "SELECT name, address FROM readings GROUP BY name, address"
        ).fetchall()
        by_name = {n: a for a, n in devices.items()}

        def drop_clash(stored: str, addr: str, want: str) -> int:
            """Rows that already exist under the name the config uses now.

            The same sample can be stored twice - once under the old name, from
            before a rename, and once under the current one - and the unique
            index is on (ts_utc, name, address, key), so a plain rename would
            collide. The duplicate is dropped first, then the rest are renamed;
            the count is reported so a merge is not silent.
            """
            return con.execute(
                "DELETE FROM readings WHERE name = :n AND address = :a AND "
                "EXISTS (SELECT 1 FROM readings AS other WHERE other.name = :w "
                "AND other.ts_utc = readings.ts_utc AND other.key = readings.key)",
                {"n": stored, "a": addr, "w": want}).rowcount

        for stored, addr in pairs:
            want = devices.get(addr)
            if want and want != stored:
                gone = drop_clash(stored, addr, want)
                n = con.execute("SELECT COUNT(*) FROM readings WHERE "
                                "name = :n AND address = :a",
                                {"n": stored, "a": addr}).fetchone()[0]
                con.execute("UPDATE readings SET name = :w WHERE "
                            "name = :n AND address = :a",
                            {"w": want, "n": stored, "a": addr})
                changed += n
                print(f"[sync] {addr}: {stored} -> {want} "
                      f"({n} row(s) renamed, {gone} duplicate(s) dropped)")
            real = by_name.get(stored)
            if real and real != addr and not is_address(addr) and is_address(real):
                gone = drop_clash(stored, addr, stored)
                n = con.execute("SELECT COUNT(*) FROM readings WHERE "
                                "name = :n AND address = :a",
                                {"n": stored, "a": addr}).fetchone()[0]
                con.execute("UPDATE readings SET address = :r WHERE "
                            "name = :n AND address = :a",
                            {"r": real, "n": stored, "a": addr})
                changed += n
                print(f"[sync] {stored}: address {addr} -> {real} "
                      f"({n} row(s) moved, {gone} duplicate(s) dropped)")
    return changed


def coverage() -> list:
    if not DB.exists():
        return []
    with sqlite3.connect(DB) as con:
        try:
            return con.execute(
                "SELECT name, MIN(ts_utc), MAX(ts_utc), COUNT(ts_utc), "
                "COUNT(DISTINCT ts_utc) FROM readings GROUP BY name ORDER BY name"
            ).fetchall()
        except sqlite3.OperationalError:  # no readings yet
            return []


def write_report(ts_utc: str, seen: list, wanted: dict, rows: int,
                 scan_s: float, wait_s: float) -> None:
    lines = [
        "# Last thermwatch run",
        "",
        f"Run at {ts_utc} (UTC) - scan {scan_s:.0f}s, connect wait {wait_s:.0f}s.",
        "",
        "## Readings",
    ]
    for name, addr, decoded in seen:
        lines.append(f"- **{name}** (`{addr}`, model {decoded['model']}):")
        for key, (value, unit) in decoded["values"].items():
            lines.append(f"  - {key}: {value}{unit}")
    lines.append("")
    missing = [n for a, n in wanted.items()
               if not any(a == s[1] for s in seen)]
    lines.append("## Not seen")
    if missing:
        lines += [f"- {n} - idle or out of range; nothing stored for it"
                  for n in missing]
    else:
        lines.append("- none")
    lines += ["", f"{rows} value row(s) added to `thermometer.sqlite3`.",
              "", "## Coverage in the store", ""]
    for name, first, last, rows_n, samples in coverage():
        lines.append(f"- {name}: {samples} sample(s), {rows_n} value row(s), "
                    f"{first} .. {last}")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> int:
    wanted = load_config(args.config)

    sessions = []
    if not args.no_connect:
        sessions, _connected = await open_sessions(list(wanted), args.connect_wait)

    found = await scan(args.scan_seconds)
    ts_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seen, rows = [], 0
    for addr, (dev, adv) in found.items():
        name = wanted.get(addr) or f"unmapped {addr} ({dev.name})"
        decoded = decode(dev, adv)
        if not decoded["values"]:
            print(f"[read] {name}: no reading in this advertisement")
            continue
        print(f"[read] {name}: "
              + ", ".join(f"{k}={v}{u}" for k, (v, u) in decoded["values"].items()))
        seen.append((name, addr, decoded))
        if not args.list:
            rows += store(ts_utc, name, addr, decoded)

    if not args.list:
        write_report(ts_utc, seen, wanted, rows, args.scan_seconds,
                     args.connect_wait)
    if not seen:
        print("[done] nothing answered the scan - idle or out of range")
    for s in sessions:
        s.maintain_connection = False
    return 0


async def collect_rssi(seconds: float) -> dict:
    """One active scan; return {address: [(rssi, local_name), ...]}."""
    from winrt.windows.devices.bluetooth.advertisement import (
        BluetoothLEAdvertisementWatcher,
        BluetoothLEScanningMode,
    )

    out: dict[str, list] = {}

    def on_adv(_sender, args):
        out.setdefault(f"{args.bluetooth_address:012x}", []).append(
            (args.raw_signal_strength_in_dbm, args.advertisement.local_name))

    async def run_it():
        watcher = BluetoothLEAdvertisementWatcher()
        watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE
        watcher.allow_extended_advertisements = True
        token = watcher.add_received(on_adv)
        watcher.start()
        await asyncio.sleep(seconds)
        watcher.stop()
        watcher.remove_received(token)

    await run_it()
    return out


async def range_probe(addresses: list[str], wait_s: float, scan_s: float) -> dict:
    """Connect the configured devices, then rank everything in range by signal."""
    sessions, connected = await open_sessions(addresses, wait_s)
    samples = await collect_rssi(scan_s)
    best = {}
    for addr, vals in samples.items():
        known = [r for r, _n in vals if r is not None and r > -127]
        if not known:
            continue
        name = next((n for _r, n in vals if n), "")
        best[addr] = (max(known), min(known), len(known), name)
    for s in sessions:
        s.maintain_connection = False
    return best, connected


def write_range_report(best: dict, wanted: dict, connected: dict,
                       scan_s: float) -> None:
    """What the probe proved. Signal strength is informational only.

    Measured on this dongle: the same device at the same 4 m gave -51, -56 and
    -66 dBm in three runs - a 15 dB spread, which at any plausible dB-per-doubling
    is a factor of ten in distance. So the honest outputs are "it answered",
    "it connected" and how many samples there were; metres are not derivable.
    """
    lines = ["# Last thermwatch range probe", "",
             f"Run at {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
             f"(UTC) - one active scan of {scan_s:.0f}s.", "",
             "## In range", ""]
    for addr, (hi, lo, n, name) in sorted(best.items(),
                                          key=lambda kv: -kv[1][0]):
        label = wanted.get(addr) or f"unmapped {addr} ({name})"
        lines.append(f"- {label}: answered {n} time(s); signal reported "
                     f"{hi} .. {lo} dBm (informational - the spread at a fixed "
                     "spot is larger than any distance model)")
    lines += ["", "## Configured devices that connected", ""]
    if connected:
        for addr, ok in connected.items():
            label = wanted.get(addr, addr)
            lines.append(f"- {label}: {'connected (GATT session reached ACTIVE)' if ok else 'did NOT connect'}")
    else:
        lines.append("- none were probed (--no-connect)")
    missing = [n for a, n in wanted.items() if a not in best]
    lines += ["", "## Not seen", ""]
    if missing:
        lines += [f"- {n} - no advertisement at all: out of range, idle, or not "
                  "paired" for n in missing]
    else:
        lines.append("- none")
    lines += ["", "## What this can and cannot tell you", "",
              "Reliable: whether a device answers at all - that is what a reading needs,",
              "and whether a GATT session reaches ACTIVE - that is what wakes an idle device.",
              "A device that answers but does not connect still gives readings; one that",
              "answers rarely just samples slowly. Not reliable: distance in metres - RSSI",
              "from this stack swings by 15 dB at a fixed spot, so it cannot be turned",
              "into a range."]
    RANGE.write_text("\n".join(lines) + "\n", encoding="utf-8")



class Prefixed:
    """Put the same tag in front of every line this tool writes.

    The streams are wrapped rather than every `print` rewritten: the same
    helper lets `thermview` cover the access log it does not produce and the
    lines it does, so one small class explains both tools' consoles.
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
    ap = argparse.ArgumentParser(description="sample the paired Govee thermometers")
    ap.add_argument("--list", action="store_true",
                    help="show what the scan sees; store nothing")
    ap.add_argument("--scan-seconds", type=float, default=90.0,
                    help="active-scan length (default 90; a short scan can miss "
                         "the device - 30s did, 75s found it)")
    ap.add_argument("--connect-wait", type=float, default=45.0,
                    help="max seconds to wait for a session to become ACTIVE "
                         "(observed 4-28 s for this device, so 20 s was too tight)")
    ap.add_argument("--no-connect", action="store_true",
                    help="scan only, for devices that are already connected")
    ap.add_argument("--config", type=Path, default=CONFIG,
                    help="name -> address json (default devices.json)")
    ap.add_argument("--range", action="store_true",
                    help="rank devices by signal strength instead of storing "
                         "readings")
    ap.add_argument("--range-seconds", type=float, default=RANGE_SAMPLES_S,
                    help="scan length for --range (default 30)")
    ap.add_argument("--import", dest="csv_in", type=Path, metavar="CSV",
                    help="store a CSV the phone app exported")
    ap.add_argument("--device", metavar="NAME",
                    help="which device that CSV belongs to (a configured name "
                         "or a 12-hex address)")
    ap.add_argument("--sync-names", action="store_true",
                    help="rewrite stored names to match devices.json "
                         "(matched by address), then stop")
    ap.add_argument("--history", type=int, metavar="MINUTES",
                    help="ask the device for its own history (up to 20 days "
                         "= 28800 minutes) and store it")
    ap.add_argument("--tag", default=None,
                    help="put this label in front of every log line "
                         "(default: the folder this tool sits in)")
    args = ap.parse_args()
    tag = args.tag or HERE.name
    sys.stdout = Prefixed(sys.stdout, tag)
    sys.stderr = Prefixed(sys.stderr, tag)
    if args.csv_in:
        if not args.device:
            print("[import] --device is required with --import")
            return 1
        devices = load_config(args.config)
        stats = import_rows(args.csv_in, devices, args.device)
        write_report(datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     [], devices, stats["stored"], 0, 0)
        return 0
    if args.history:
        devices = load_config(args.config)
        asyncio.run(store_history(devices, args.history))
        return 0

    if args.sync_names:
        devices = load_config(args.config)
        n = sync_names(devices)
        print(f"[sync] {n} row(s) renamed" if n else "[sync] already in line")
        return 0
    if args.range:
        devices = load_config(args.config)
        best, connected = asyncio.run(
            range_probe(list(devices), args.connect_wait, args.range_seconds))
        write_range_report(best, devices, connected, args.range_seconds)
        return 0
    return asyncio.run(run(args)) or 0


if __name__ == "__main__":
    raise SystemExit(main())

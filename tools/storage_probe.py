#!/usr/bin/env python3
"""A snapshot of every storage of the connected MTP device (its internal storage, a memory
card), and the difference between two snapshots: for finding where another program (Garmin
Express) puts the files of a map (docs/design/maps-on-memory-card.md). It ONLY READS. Not
part of a release; run by hand from the folder of the project, with openExpress for Mac and
Garmin Express both quit (each holds the device while it runs):

    python3 tools/storage_probe.py snapshot OUT.json [--depth N] [--device-xml OUT.xml]
    python3 tools/storage_probe.py diff BEFORE.json AFTER.json

snapshot: for each storage, what the device says of it (its name, StorageType, filesystem,
access, capacity and free space) and every folder and file the ordinary MTP listing shows,
with its size, down to --depth folders (default 4). --device-xml also saves the device's
GarminDevice.xml with its unit ID and unlock codes masked (masked_device_xml), e.g. for a
test fixture.

diff: the files added, removed and changed in size on each storage, and the change of its
free space.

Nothing is written to the device; the snapshot holds names and sizes, never a serial number
or a unit ID. Exit code: 0, or 2 when no device can be opened."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openexpress.device import ptp  # noqa: E402

DEVICE_XML = "GARMIN/GarminDevice.xml"
DEFAULT_DEPTH = 4
STORAGE_TYPES = {ptp.STORAGE_FIXED_ROM: "FixedROM", ptp.STORAGE_REMOVABLE_ROM: "RemovableROM",
                 ptp.STORAGE_FIXED_RAM: "FixedRAM", ptp.STORAGE_REMOVABLE_RAM: "RemovableRAM"}


# --- the snapshot ---------------------------------------------------------------------------

def snapshot(session: ptp.Session, depth: int = DEFAULT_DEPTH) -> dict:
    """Every storage of the device on `session`, read only: {"storages": [...]}, each with what
    GetStorageInfo says, its folders and its files (path -> size), and whether it has
    GARMIN/GarminDevice.xml."""
    out = []
    for store in session.storages():
        info = session.storage_info(store)
        folders: list[str] = []
        files: dict[str, int] = {}
        _walk(session, store, ptp.ROOT, "", depth, folders, files)
        out.append({
            "id": f"{store:08x}",
            "description": info.description,
            "type": STORAGE_TYPES.get(info.storage_type, f"0x{info.storage_type:04x}"),
            "removable": info.removable,
            "filesystem": f"0x{info.filesystem:04x}",
            "read_only": info.read_only,
            "capacity": info.capacity,
            "free": info.free,
            "has_device_xml": any(p.casefold() == DEVICE_XML.casefold() for p in files),
            "folders": folders,
            "files": files,
        })
    return {"storages": out}


def _walk(session, store: int, parent: int, path: str, depth: int, folders: list[str],
          files: dict[str, int]) -> None:
    for obj in sorted(session.children(store, parent), key=lambda o: o.name.casefold()):
        full = f"{path}/{obj.name}" if path else obj.name
        if obj.is_dir:
            folders.append(full)
            if depth > 1:
                _walk(session, store, obj.handle, full, depth - 1, folders, files)
        else:
            files[full] = obj.size


def device_xml(session: ptp.Session) -> bytes | None:
    """GARMIN/GarminDevice.xml of the storage that has it (any case), read only; None."""
    for store in session.storages():
        handle = ptp.ROOT
        for part in DEVICE_XML.split("/"):
            found = next((o for o in session.children(store, handle)
                          if o.name.casefold() == part.casefold()), None)
            if found is None:
                break
            handle = found.handle
        else:
            return session.read_file(handle)
    return None


_ID = re.compile(rb"(<Device\b[^>]*>.*?<Id>)\d+(</Id>)", re.DOTALL)
# What may tell one unit from another besides its ID: its unlock codes, the number of its
# device extension (Ifix) and the public key of its Connect IQ extension (Modulus).
_PER_UNIT = re.compile(rb"(<(?:\w+:)?(?:Code|Ifix|Modulus)>)([^<]*)(</(?:\w+:)?(?:Code|Ifix|"
                       rb"Modulus)>)")


def masked_device_xml(data: bytes) -> bytes:
    """GarminDevice.xml with the unit ID (the device's first <Id>) set to 0, and each unlock
    code, Ifix and Modulus replaced by X's of its length; everything else as the device wrote
    it."""
    data = _ID.sub(rb"\g<1>0\g<2>", data, count=1)
    return _PER_UNIT.sub(lambda m: m.group(1) + b"X" * len(m.group(2)) + m.group(3), data)


# --- the difference -------------------------------------------------------------------------

def diff(before: dict, after: dict) -> list[str]:
    """Lines saying what changed on each storage between two snapshots, by its name."""
    lines = []
    old = {s["description"] or s["id"]: s for s in before["storages"]}
    new = {s["description"] or s["id"]: s for s in after["storages"]}
    for name in [*old, *(n for n in new if n not in old)]:
        a, b = old.get(name), new.get(name)
        if a is None or b is None:
            lines.append(f"## {name}: {'added' if a is None else 'gone'}")
            continue
        changes = []
        for path in sorted(set(a["files"]) | set(b["files"]), key=str.casefold):
            was, now = a["files"].get(path), b["files"].get(path)
            if was is None:
                changes.append(f"  + {path}  {now}")
            elif now is None:
                changes.append(f"  - {path}  {was}")
            elif was != now:
                changes.append(f"  ~ {path}  {was} -> {now}")
        freed = b["free"] - a["free"]
        lines.append(f"## {name}: {len(changes)} file(s) changed, free space {freed:+d} bytes")
        lines.extend(changes)
    return lines


# --- the command ----------------------------------------------------------------------------

def _open_session():
    """A context manager that gives the PTP session of the connected device (macOS: its
    storage opened by mac_storage, then closed); None when there is none."""
    from openexpress.device import mac_storage

    found = mac_storage.find_devices()
    if not found:
        return None
    storage = mac_storage.open_found(found[0])

    class _Opened:
        def __enter__(self):
            self._lent = storage.exclusive_ptp()
            return self._lent.__enter__()

        def __exit__(self, *exc):
            try:
                return self._lent.__exit__(*exc)
            finally:
                storage.close()

    return _Opened()


def main(argv=None, opener=_open_session) -> int:
    p = argparse.ArgumentParser(description="Snapshots of every storage of the connected "
                                            "device (read only), and their difference.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="list every storage of the device into a JSON file")
    s.add_argument("out", type=Path)
    s.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                   help=f"how many folders deep to list (default {DEFAULT_DEPTH})")
    s.add_argument("--device-xml", type=Path, metavar="OUT",
                   help="also save GarminDevice.xml, its unit ID and unlock codes masked")
    d = sub.add_parser("diff", help="what changed between two snapshots")
    d.add_argument("before", type=Path)
    d.add_argument("after", type=Path)
    args = p.parse_args(argv)

    if args.cmd == "diff":
        before, after = (json.loads(f.read_text(encoding="utf-8"))
                         for f in (args.before, args.after))
        print("\n".join(diff(before, after)))
        return 0
    opened = opener()
    if opened is None:
        print("No device over MTP: connect it, and quit openExpress for Mac and Garmin "
              "Express.", file=sys.stderr)
        return 2
    with opened as session:
        snap = snapshot(session, args.depth)
        xml = device_xml(session) if args.device_xml else None
    args.out.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    for st in snap["storages"]:
        print(f"{st['description'] or st['id']}: {st['type']}, {len(st['files'])} files, "
              f"{st['free']} of {st['capacity']} bytes free"
              + (", GarminDevice.xml" if st["has_device_xml"] else ""))
    if args.device_xml:
        if xml is None:
            print("GarminDevice.xml not found", file=sys.stderr)
        else:
            args.device_xml.write_bytes(masked_device_xml(xml))
            print(f"GarminDevice.xml saved, masked: {args.device_xml}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""A device's memory card: which of its storages it is, and where map files are on it.

A handheld such as the GPSMAP H1i Plus has two storages over MTP: its internal storage, which
has GARMIN/GarminDevice.xml, and a memory card. GarminDevice.xml declares the card as a place
for maps (StorageLocationsExtension: `MTPId="Memory Card"`), and for each data type whose
files may go there, their folder on it (ExternalPath). Garmin Express puts a map on the card by
that rule (docs/design/maps-on-memory-card.md): a file whose data type has an ExternalPath goes
to the card under that path; any other (its unlock, its search files) stays on internal.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import subprocess
import sys
from pathlib import Path

from openexpress.device.storage import LocalStorage, Storage
from openexpress.i18n import _

log = logging.getLogger(__name__)

# Where a map is: its own .img files on internal storage, on the memory card, or on both (an
# interrupted move, or what another program did).
INTERNAL, CARD, BOTH = "internal", "card", "both"
# Installed, its own .img files not on internal storage, while the device's card is not there
# (taken out: the device declares a card but has none) or cannot be read: presumably on it.
CARD_OUT = "card_out"


def card_of(storage: Storage, device=None) -> Storage | None:
    """The memory card among the device's other storages (`storage.others`): the one named as
    GarminDevice.xml names its card (`device.storage_locations`, by MTPId), else the only one
    the device says can be taken out. None for a device without one, or when it cannot be
    told."""
    if (volume := getattr(storage, "card_volume", None)) is not None:
        return volume  # the card in the computer's reader (reader_card)
    others = tuple(getattr(storage, "others", ()) or ())
    if not others:
        return None
    named = {loc.mtp_id.casefold() for loc in getattr(device, "storage_locations", ()) or ()
             if loc.mtp_id}
    if named:
        for other in others:
            if (other.description or "").casefold() in named:
                return other
    removable = [o for o in others if o.removable]
    return removable[0] if len(removable) == 1 else None


def declares_card(device) -> bool:
    """Whether the device's GarminDevice.xml declares a memory card (StorageLocationsExtension),
    whether or not one is in it now."""
    return any(loc.mtp_id for loc in getattr(device, "storage_locations", ()) or ())


def card_paths(path: str, device=None) -> list[str]:
    """Where a file the server names `path` (from the root of the internal storage, e.g.
    "Garmin/D9213050A.img") may be on the memory card: for each data type of the device's
    GarminDevice.xml it belongs to that has an ExternalPath, the path with that data type's
    folder for its ExternalPath (in the order of GarminDevice.xml, without repeats). Without a
    device, the same path, as every handheld seen so far names the card's folders as its own.
    Empty when no data type of it may be on the card."""
    if device is None:
        return [path]
    out: list[str] = []
    for f in device.files:
        if f.external_path is None or not f.matches(path):
            continue
        base = f.path.strip("/")
        rest = path.strip("/")[len(base):].lstrip("/") if base else path.strip("/")
        candidate = posixpath.join(f.external_path, rest) if f.external_path else rest
        if candidate.casefold() not in (c.casefold() for c in out):
            out.append(candidate)
    return out


# --- where a file of a map goes when the map goes to the card --------------------------------

# The kind a .sid file names in its header ("GARMIN SID", then a 16-bit kind at byte 14), and
# the data type of GarminDevice.xml it is: the path and extension alone (Garmin/SID/*.sid)
# match three data types, of which only Etyp has an ExternalPath on the H1i Plus.
SID_MAGIC = b"GARMIN SID"
SID_KINDS = {13: "Etyp", 2: "TopoActiveInstaSearch", 9: "TopoActivePhoneticInstaSearch"}
SID_HEADER = 16  # bytes to read of a .sid for its kind


def sid_kind(head: bytes | None) -> int | None:
    """The kind a .sid file names in its first bytes; None when they are no such header."""
    if not head or len(head) < SID_HEADER or not head.startswith(SID_MAGIC):
        return None
    return int.from_bytes(head[14:16], "little")


def _types_of(path: str, device, head: bytes | None) -> list:
    """The data types of GarminDevice.xml a file written to the device belongs to: those that
    take files (InputToUnit, InputOutput) matching its path, the most specific ones (with a
    BaseName, e.g. BaseMaps' gmapbmap) alone when there are any, and of a .sid matching
    several, the one its header names (none when it names none known)."""
    matches = [f for f in device.files if f.writable and f.matches(path)]
    specific = [f for f in matches if f.basename]
    if specific:
        matches = specific
    if len({f.datatype for f in matches}) > 1 and path.casefold().endswith(".sid"):
        named = SID_KINDS.get(sid_kind(head))
        matches = [f for f in matches if f.datatype == named]
    return matches


def _mapped(f, path: str) -> str:
    base = f.path.strip("/")
    rest = path.strip("/")[len(base):].lstrip("/") if base else path.strip("/")
    return posixpath.join(f.external_path, rest) if f.external_path else rest


def card_path_of(path: str, device, head: bytes | None = None) -> str | None:
    """Where a file of a map going to the memory card is written on the card, as Garmin
    Express does it: under the ExternalPath of its data type, when every data type it may be
    has one and they agree. None: it stays on internal storage, as the unlock (.gma, which no
    data type declares), search files (InstaSearch .sid) and anything else (a data type
    without an ExternalPath, or none known). `head`: the first bytes of the file, which tell a
    .sid's kind (sid_kind); without them such a .sid stays on internal."""
    if device is None:
        return None
    types = _types_of(path, device, head)
    if not types or any(f.external_path is None for f in types):
        return None
    places = {_mapped(f, path) for f in types}
    return places.pop() if len(places) == 1 else None


def may_go_to_card(path: str, device) -> bool:
    """Whether a file may go to the card when its map does, its kind not read yet (a .sid of
    the kind ETYP does, one of another kind does not): for the space a plan counts on each
    storage before anything is downloaded."""
    if device is None:
        return False
    if path.casefold().endswith(".sid"):
        return any(card_path_of(path, device, SID_MAGIC + bytes(4) + kind.to_bytes(2, "little"))
                   is not None for kind in SID_KINDS)
    return card_path_of(path, device) is not None


# The largest file FAT32 (the card's format, as the device declares it) can hold.
FAT32_MAX_FILE = 2**32 - 1


# --- the card in a computer's card reader -----------------------------------------------------
#
# Writing a map's files to the card in the computer's card reader instead of through the device,
# which writes its card at a few MB/s over USB (docs/design/maps-on-memory-card.md measures it).
# The device stays connected meanwhile, its card out: the unlock and search files still go to its
# internal storage over USB. What tells the device's card apart in a reader is kept whenever it
# is seen over MTP: its capacity and the map files on it.

CARD_FILE = "memory-card.json"
# The volume of a card in a reader is the card's capacity over MTP less about one allocation
# block (the H1i Plus's exFAT card: 131,072 bytes less); a megabyte apart is still the card.
CAPACITY_SLACK = 1 << 20


def _card_file(unit_id: int) -> Path:
    from openexpress import paths

    return paths.device_dir(unit_id) / CARD_FILE


def remember(device, card: Storage, files: dict[str, int]) -> None:
    """Keeps what tells the device's card apart in a card reader: its capacity and name over
    MTP and the map files found on it (`files`: its path on the card -> size). Only the log hears
    of a failure: a card not remembered is not offered in a reader."""
    unit_id = getattr(device, "unit_id", None)
    capacity = card.capacity()
    if not unit_id or not capacity:
        return
    record = {"capacity": capacity, "name": card.description or "",
              "files": dict(sorted(files.items()))}
    target = _card_file(unit_id)
    try:
        if target.exists() and json.loads(target.read_text(encoding="utf-8")) == record:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        temporary.replace(target)
    except (OSError, ValueError) as e:
        log.info("card: not remembered (%s)", type(e).__name__)


def remembered(unit_id: int | None) -> dict | None:
    """What was kept of the device's card (remember); None when it was never seen."""
    if not unit_id:
        return None
    try:
        record = json.loads(_card_file(unit_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("capacity") else None


def _named(folder: Path, name: str) -> Path | None:
    try:
        return next((p for p in folder.iterdir() if p.name.casefold() == name.casefold()), None)
    except OSError:
        return None


def _at(root: Path, path: str) -> Path | None:
    """`path` under `root`, each part in any letter case (as a device's paths are)."""
    here = root
    for part in [p for p in path.split("/") if p]:
        found = _named(here, part)
        if found is None:
            return None
        here = found
    return here


def not_the_card(unit_id: int | None, volume: Path) -> str | None:
    """Why the mounted `volume` is not this device's memory card; None when it is: its capacity
    the card's (within CAPACITY_SLACK), a Garmin folder, and every map file the device last had
    on its card there at its size."""
    record = remembered(unit_id)
    if record is None:
        # TRANSLATORS: why a card in the computer's card reader is not used
        return _("openExpress has not seen this device's memory card yet: connect the device "
                 "with its card in it once, then try again.")
    try:
        st = os.statvfs(volume)
    except OSError:
        return _("The card cannot be read.")
    if abs(st.f_blocks * st.f_frsize - int(record["capacity"])) > max(CAPACITY_SLACK,
                                                                       st.f_frsize):
        # TRANSLATORS: why a card in the computer's card reader is not used
        return _("Its size is not that of the device's memory card.")
    if _named(volume, "Garmin") is None:
        return _("It has no Garmin folder.")
    for path, size in (record.get("files") or {}).items():
        found = _at(volume, path)
        if found is None or not found.is_file() or found.stat().st_size != size:
            # TRANSLATORS: why a card in the computer's card reader is not used
            return _("The maps on it are not those the device last had on its card.")
    return None


def mount_roots() -> list[Path]:
    """Where a card in the computer's reader is mounted: /Volumes on macOS; on Linux
    /run/media/<user> (udisks) and /media/<user>."""
    if sys.platform == "darwin":
        return [Path("/Volumes")]
    user = os.environ.get("USER", "")
    return [Path("/run/media") / user, Path("/media") / user] if user else []


def reader_volumes(unit_id: int | None, root: Path | None = None) -> list[tuple[Path, str]]:
    """The mounted volumes with a Garmin folder (a memory card in the computer's reader), each
    with why it is not this device's card ("" when it is); under `root`, else mount_roots."""
    out = []
    volumes = []
    for folder in [root] if root is not None else mount_roots():
        try:
            volumes += sorted(folder.iterdir())
        except OSError:
            continue
    for volume in volumes:
        if volume.is_symlink() or not volume.is_dir() or _named(volume, "Garmin") is None:
            continue
        out.append((volume, not_the_card(unit_id, volume) or ""))
    return out


def reader_card(device, volume: Path) -> Storage:
    """The volume of the device's card in the computer's reader, as its card storage: the
    storage of a map's card files for an install or a move (card_of gives it, set as
    `storage.card_volume`). DeviceError when it is not the device's card (not_the_card)."""
    from openexpress.device.storage import DeviceError

    volume = Path(volume)
    if (why := not_the_card(getattr(device, "unit_id", None), volume)) is not None:
        # TRANSLATORS: error: a volume given as the device's card in the card reader is not;
        # {volume}: its name, {reason}: why
        raise DeviceError(_("{volume} is not this device's memory card: {reason}").format(
            volume=volume.name, reason=why))
    st = ReaderCard(volume)
    record = remembered(device.unit_id) or {}
    st.description = record.get("name") or volume.name
    st.removable = True
    return st


def _apple_double(file: Path) -> Path:
    return file.with_name("._" + file.name)


class ReaderCard(LocalStorage):
    """The device's card in the computer's reader. macOS keeps a file's extended attributes on
    a FAT or exFAT card in a hidden "._" file beside it (AppleDouble): `Garmin/._DB961020A.img`
    beside a map, which the device might take for a map of its own. Each one beside a file
    written here is removed at once."""

    in_reader = True

    def _write(self, real_path: str, data: bytes) -> None:
        super()._write(real_path, data)
        self._no_apple_double(real_path)

    def _write_file(self, real_path: str, local: Path, progress=None) -> None:
        super()._write_file(real_path, local, progress)
        self._no_apple_double(real_path)

    def _no_apple_double(self, real_path: str) -> None:
        side = _apple_double(self._p(real_path))
        try:
            side.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.info("card: a macOS ._ file not removed (%s)", type(e).__name__)


def _no_apple_doubles(root: Path) -> int:
    """The macOS "._" files beside files in the card's Garmin folder removed (before it is
    ejected); how many."""
    garmin = _named(root, "Garmin")
    gone = 0
    for folder, _dirs, files in os.walk(garmin) if garmin else ():
        for name in files:
            if name.startswith("._") and name[2:] in files:
                try:
                    (Path(folder) / name).unlink()
                    gone += 1
                except OSError as e:
                    log.info("card: a macOS ._ file not removed (%s)", type(e).__name__)
    return gone


def eject_command(root: Path) -> list[str]:
    """The command that ejects the volume at `root`: diskutil on macOS, gio elsewhere."""
    if sys.platform == "darwin":
        return ["diskutil", "eject", str(root)]
    return ["gio", "mount", "--eject", str(root)]


def eject(card: Storage) -> bool:
    """A card in the computer's reader ejected (eject_command), so that it may be put back in
    the device; whether it was."""
    root = getattr(card, "root", None)
    if root is None or not getattr(card, "in_reader", False):
        return False
    if gone := _no_apple_doubles(Path(root)):
        log.info("card: %d macOS ._ files removed before ejecting", gone)
    try:
        done = subprocess.run(eject_command(Path(root)), capture_output=True, text=True,
                              check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("card: not ejected (%s)", type(e).__name__)
        return False
    if done.returncode != 0:
        log.info("card: not ejected (%s %d)", eject_command(Path(root))[0], done.returncode)
    return done.returncode == 0

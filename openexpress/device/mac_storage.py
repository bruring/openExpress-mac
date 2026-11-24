"""Access to an MTP device on macOS, where there is no gvfs: the program has the device to
itself through its own PTP client (ptp.Session, over libusb) for as long as the storage is
open, and reads, writes, deletes and makes folders with it. Found and opened as gio_storage
finds and opens devices on Linux, under the same names (platform.devices), so that the
command line goes the same way on both.

What a listing shows is what ordinary MTP shows (GetObjectHandles), as through gvfs: the
folders a device hides are read through exclusive_ptp, as on Linux, which here lends the
session the storage has open. A storage keeps what it listed (names, sizes and handles), so
that a path is looked up without asking the device again; refresh() drops it, and what is
done through the lent session drops it too.

A device is addressed as mtp://[usb:BUS,DEVICE]/ (its place on USB, new at each plugging in),
the older form of gvfs's addresses, which --device-uri takes as well.

A Garmin device in mass storage mode (a drive) is mounted by macOS itself, under /Volumes: it
is found by its disks below its USB device (mac_usb.drive_devices), opened as a UmsStorage on
the folder where its volume is mounted (addressed as file:///Volumes/…), and ejected after a
write (eject: `diskutil eject`), as gio_storage does on Linux. A drive has no session: nothing
is held open of it between the engine's requests.
"""

from __future__ import annotations

import atexit
import logging
import os
import posixpath
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from openexpress.device import ptp
from openexpress.device.found import (  # noqa: F401 - the names of platform.devices()
    ABSENT,
    DRIVE_OFF,
    GARMIN_VID,
    MASS_STORAGE,
    NO_DEVICE_FILE,
    NOT_MOUNTED,
    PRESENT,
    STARTING,
    STARTING_PRODUCT,
    TEST_DRIVE_ENV,
    UNREADABLE,
    UNSUPPORTED,
    USB_GAP_S,
    WAIT_FOR_DEVICE_S,
    DeviceInUse,
    DeviceSummary,
    DriveNotMounted,
    FoundDevice,
    GarminUsb,
    MassStorageNotice,
    NoDevice,
    NotThereYet,
    SeveralDevices,
    UnsupportedDevice,
    device_uri,
    distinct_devices,
    ejected_text,
    files_not_offered_text,
    mass_storage_text,
    named_for_testing,
    no_device_text,
    one_device,
    place_of,
    test_drive_ids,
    unsupported_device_text,
    usb_place,
    wait_for_device,
)
from openexpress.device.found import probe as found_probe
from openexpress.device.storage import (
    CopyNotStarted,
    DeviceError,
    DeviceNotResponding,
    Entry,
    LocalStorage,
    Storage,
    UmsStorage,
    entry_named,
)
from openexpress.i18n import _, human_size, pgettext
from openexpress.platform import mac_usb

log = logging.getLogger(__name__)

def _entry(info: ptp.ObjectInfo) -> Entry:
    return Entry(info.name, info.is_dir, 0 if info.is_dir else info.size, info.modified)


class MacStorage(Storage):
    """One storage of an MTP device (its internal storage), through a PTP session of its own,
    open from the opening to close(). Over MTP the old file under a name is deleted before
    the new one is sent (replaces_atomically False), as through gvfs.

    The device's other storages (a memory card) are MacStorages of the same session, kept in
    `others` of the one with GARMIN/GarminDevice.xml (adopt): they share its lock, so that a
    transaction on one never starts in the middle of one on another, its stall and its
    closing; what they listed is dropped with its own."""

    def __init__(self, session: ptp.Session, store: int, usb: GarminUsb | None = None,
                 uri: str | None = None, description: str = "",
                 removable: bool | None = None):
        self._session = session
        self.store = store
        self.usb = usb
        self.device_uri = uri
        self.description = description
        self.removable = removable
        # As gvfs names the storage of a device: its address, then the storage's name.
        self.label = (uri or "mtp://") + (description or f"{store:08x}")
        self._lock = threading.RLock()
        self._owner: MacStorage | None = None  # the storage whose session this one shares
        # What was listed, by the real path of each folder ("": the top level), and the handle
        # of each object listed by its real path.
        self._folders: dict[str, list[ptp.ObjectInfo]] = {}
        # Each write that failed after the device made its object: the handle it left behind,
        # by the real path (_discard_partial).
        self._partial: dict[str, int] = {}
        # The storage's space as the device told it last (GetStorageInfo), until something is
        # written, deleted or made, or what was listed is dropped (refresh): the free space
        # and the capacity of one look at the device are one question.
        self._space_told: ptp.StorageInfo | None = None
        self._closed = False

    # --- the session --------------------------------------------------------------------

    def adopt(self, others: list[MacStorage]) -> None:
        """The device's other storages, of this storage's session: kept in `others`, sharing
        its lock, its stall and its closing."""
        for other in others:
            other._owner = self
            other._lock = self._lock
        self.others = tuple(others)

    @property
    def closed(self) -> bool:
        return self._closed or (self._owner is not None and self._owner.closed)

    def _responding(self) -> None:
        """As Storage._responding; DeviceError once it is closed. A storage of another one's
        session also fails as that one does."""
        if self._owner is not None:
            self._owner._responding()
        super()._responding()
        if self._closed:
            raise DeviceError(_("The connection to the device is closed."))

    def _forget_listed(self) -> None:
        """What was listed of every storage of this session dropped (a refresh, a session lent:
        anything may have changed on any of them)."""
        root = self._owner or self
        for st in (root, *root.others):
            st._folders.clear()
            st._space_told = None

    def _call(self, what: str, action):
        """action(session), its PtpError said as `what` failing (DeviceError). A session out
        of step after it (a transfer that failed or had no answer in time) is a device that
        stopped responding: DeviceNotResponding, now and for every later operation, and the
        device is let go."""
        self._responding()
        with self._lock:
            try:
                return action(self._session)
            except ptp.PtpError as e:
                if self._session.broken:
                    raise self._stall(what, e) from e
                raise DeviceError(f"{what}: {e}") from e

    def _stall(self, what: str, error: Exception) -> DeviceNotResponding:
        if self._owner is not None:  # the session is the owner's: it stalls, and this with it
            self._stalled = self._owner._stall(what, error)
            return self._stalled
        stalled = self._stalled_during(what)
        note_stalled(self.usb)
        log.warning("device: the PTP session broke off (%s: %s); the device is let go",
                    type(error).__name__, error)
        self._release()
        return stalled

    def close(self) -> None:
        """The session closed and the device let go (the camera service goes on); nothing can
        be done with the storage afterwards. Again: nothing. A storage of another one's session
        is only marked closed: the session is closed with the other one."""
        with self._lock:
            if self._closed:
                return
            if self._owner is not None:
                self._closed = True
                return
            self._release()
        log.info("device: closed (USB %s)", self.usb_id or "?")

    def _release(self) -> None:
        self._closed = True
        try:
            self._session.close()  # CloseSession unless broken; the transport released anyway
        finally:
            _forget_open(self)

    # --- what was listed ----------------------------------------------------------------

    def _objects(self, real_path: str, what: str) -> list[ptp.ObjectInfo]:
        """The objects ordinary MTP shows in the folder at this real path, listed once."""
        if real_path in self._folders:
            return self._folders[real_path]
        if real_path:
            folder = self._object(real_path, what)
            if folder is None:
                raise DeviceError(f"{what}: " + _("File not found on the device: {path}").format(
                    path=real_path))
            if not folder.is_dir:
                raise DeviceError(_("Not a directory: {path}").format(path=real_path))
            handle = folder.handle
        else:
            handle = ptp.ROOT
        found = self._call(what, lambda s: s.children(self.store, handle))
        self._folders[real_path] = found
        return found

    def _object(self, real_path: str, what: str) -> ptp.ObjectInfo | None:
        """The object at this real path, from the listing of its folder; None: not there."""
        parent, name = posixpath.split(real_path)
        return entry_named(self._objects(parent, what), name) if name else None

    def _known(self, real_path: str) -> ptp.ObjectInfo | None:
        """The object at this real path if its folder was listed already; nothing is asked."""
        parent, name = posixpath.split(real_path)
        listed = self._folders.get(parent)
        return entry_named(listed, name) if listed is not None and name else None

    def _folder_handle(self, real_path: str, what: str) -> int:
        if not real_path:
            return ptp.ROOT
        folder = self._object(real_path, what)
        if folder is None or not folder.is_dir:
            raise DeviceError(_("Not a directory: {path}").format(path=real_path))
        return folder.handle

    def _file(self, real_path: str, what: str) -> ptp.ObjectInfo:
        found = self._object(real_path, what)
        if found is None or found.is_dir:
            raise DeviceError(_("File not found on the device: {path}").format(path=real_path))
        return found

    def _note(self, real_path: str, info: ptp.ObjectInfo) -> None:
        """The object now at this real path (just made, or looked at again), in the listing
        of its folder when that is kept."""
        self._space_told = None
        parent, _name = posixpath.split(real_path)
        listed = self._folders.get(parent)
        if listed is not None:
            self._folders[parent] = [o for o in listed if o.handle != info.handle] + [info]
        if info.is_dir and real_path not in self._folders:
            self._folders[real_path] = []  # a folder just made is empty

    def _forget(self, real_path: str) -> None:
        """Nothing is kept any more of the object at this real path nor, for a folder, of
        what was in it."""
        self._space_told = None
        parent, name = posixpath.split(real_path)
        listed = self._folders.get(parent)
        if listed is not None:
            gone = entry_named(listed, name)
            self._folders[parent] = [o for o in listed if o is not gone]
        for path in [p for p in self._folders if p == real_path
                     or p.startswith(real_path + "/")]:
            del self._folders[path]

    # --- Storage ------------------------------------------------------------------------

    def resolve(self, path: str, strict: bool = False) -> str | None:
        """As Storage.resolve, once the storage is known to answer: a closed one, or one whose
        device stopped responding, is never taken for a missing file."""
        self._responding()
        return super().resolve(path, strict)

    def _list(self, real_path: str) -> list[Entry]:
        self._responding()
        what = _("Cannot list {path}").format(path=real_path or "/")
        return [_entry(o) for o in self._objects(real_path, what)]

    def info(self, real_path: str) -> Entry | None:
        """The object asked about again (GetObjectInfo) when its folder was listed already: its
        size now, also of a file the device wrote since; else as its folder lists it."""
        known = self._known(real_path)
        if known is None:
            return super().info(real_path)
        what = _("Cannot read {path}").format(path=real_path)
        try:
            fresh = self._call(what, lambda s: s.info(known.handle))
        except DeviceNotResponding:
            raise
        except DeviceError:
            # Gone since it was listed (its handle is not valid any more): its folder listed
            # anew.
            self._folders.pop(posixpath.dirname(real_path), None)
            return super().info(real_path)
        self._note(real_path, fresh)
        return _entry(fresh)

    def _read(self, real_path: str) -> bytes:
        what = _("Cannot read {path}").format(path=real_path)
        found = self._file(real_path, what)
        content = bytearray()
        in_parts = not real_path.upper().endswith(".PRG")  # GetObject gives a PRG encrypted
        self._call(what, lambda s: s.read_into(found.handle, content.extend, size=found.size,
                                               in_parts=in_parts))
        return bytes(content)

    def _copy_out(self, real_path: str, dest: Path, told=None) -> int:
        """Streamed into `dest` as the device sends it (GetObject, or parts where the device
        reads them), never held whole: a map file is gigabytes."""
        what = _("Cannot read {path}").format(path=real_path)
        found = self._file(real_path, what)
        done = 0
        with open(dest, "wb") as out:
            def piece(data: bytes) -> None:
                nonlocal done
                out.write(data)
                done += len(data)
                if told is not None:
                    told(done, found.size)

            self._call(what, lambda s: s.read_into(found.handle, piece, size=found.size,
                                                   in_parts=True))
        return done

    def _read_start(self, real_path: str, count: int) -> bytes:
        """The first bytes alone (GetPartialObject); the whole file cut, on a device that does
        not read parts."""
        what = _("Cannot read {path}").format(path=real_path)
        found = self._file(real_path, what)
        if count <= 0:
            return b""

        def part(session):
            try:
                return session.read_part(found.handle, 0, min(count, ptp.MAX_OBJECT_SIZE))
            except ptp.PtpRefused as e:
                if e.code != ptp.OPERATION_NOT_SUPPORTED:
                    raise
                return None

        data = self._call(what, part)
        return self._read(real_path)[:count] if data is None else data

    def _write(self, real_path: str, data: bytes) -> None:
        what = _("Cannot write {path}").format(path=real_path)
        self._send(real_path, data, what)

    def _write_file(self, real_path: str, local: Path, progress=None) -> None:
        """Sent as it is read from the file, never held whole; `progress(done, total)` after
        each piece (ptp.WRITE_CHUNK). The file that cannot be read, or is too large for MTP,
        is CopyNotStarted: the old file stays."""
        self._responding()
        what = _("Cannot write {path}").format(path=real_path)
        try:
            stream = open(local, "rb")  # noqa: SIM115 - closed below, after the send
        except OSError as e:
            raise CopyNotStarted(f"{what}: {e.strerror or e}") from e
        with stream:
            try:
                size = os.fstat(stream.fileno()).st_size
            except OSError as e:
                raise CopyNotStarted(f"{what}: {e.strerror or e}") from e
            if size > ptp.MAX_OBJECT_SIZE:
                raise CopyNotStarted(f"{what}: " + _("The file is too large to send."))
            sent = (lambda done: progress(done, size)) if progress else None
            self._send(real_path, ptp.Outgoing(stream, size, sent), what)

    def _send(self, real_path: str, data, what: str) -> None:
        """A new object under this real path (SendObjectInfo, SendObject), the old file of the
        name deleted first: MTP replaces none. Refused before anything was touched (no old
        file, and SendObjectInfo refused): CopyNotStarted. The object a failed SendObject left
        is kept for _discard_partial."""
        parent_path, name = posixpath.split(real_path)
        parent = self._folder_handle(parent_path, what)
        old = self._object(real_path, what)
        if old is not None and old.is_dir:
            raise DeviceError(_("Cannot write {path}: it is a directory").format(path=real_path))
        if old is not None:
            self._call(what, lambda s: s.delete(old.handle, old.size))
            self._forget(real_path)

        def send(session) -> int:
            try:
                return session.send_object(self.store, parent, name, data)
            except ptp.PtpRefused as e:
                if old is None and e.operation == ptp.SEND_OBJECT_INFO:
                    raise CopyNotStarted(f"{what}: {e}") from e
                raise
            except ptp.PtpWriteError as e:
                if e.handle is not None:
                    self._partial[real_path] = e.handle
                raise

        handle = self._call(what, send)
        self._note(real_path, self._call(what, lambda s: s.info(handle)))

    def _discard_partial(self, real_path: str) -> None:
        """The object a failed send left under the name, deleted; nothing when it left none.
        A failed delete is only logged."""
        handle = self._partial.pop(real_path, None)
        self._forget(real_path)
        if handle is None or self.stalled or self.closed:
            return
        try:
            self._call(_("Cannot delete {path}").format(path=real_path),
                       lambda s: s.delete(handle))
        except DeviceNotResponding:
            raise  # a stall stops what meets it: nothing more is asked of the device
        except DeviceError as e:
            log.warning("Cannot delete a partly copied file: %s", type(e).__name__)

    def _delete(self, real_path: str) -> None:
        # TRANSLATORS: error: a file on the device cannot be deleted ({path}: the file)
        what = _("Cannot delete {path}").format(path=real_path)
        found = self._object(real_path, what)
        if found is None:
            raise DeviceError(_("File not found on the device: {path}").format(path=real_path))
        self._call(what, lambda s: s.delete(found.handle, None if found.is_dir else found.size))
        self._forget(real_path)

    def _mkdir(self, real_path: str) -> None:
        what = _("Cannot create directory {path}").format(path=real_path)
        parent_path, name = posixpath.split(real_path)
        parent = self._folder_handle(parent_path, what)
        handle = self._call(what, lambda s: s.create_folder(self.store, parent, name))
        self._note(real_path, self._call(what, lambda s: s.info(handle)))

    def _space(self) -> ptp.StorageInfo | None:
        """The storage's space, asked once until something changes it (_space_told)."""
        if self._space_told is not None:
            return self._space_told
        try:
            told = self._call(_("Cannot read the space on the device"),
                              lambda s: s.storage_info(self.store))
        except DeviceNotResponding:
            raise
        except DeviceError:
            return None
        self._space_told = told
        return told

    def free_space(self) -> int | None:
        space = self._space()
        return space.free if space is not None else None

    def capacity(self) -> int | None:
        # A size of 0 means "unknown" (a real storage is never empty-sized).
        space = self._space()
        return (space.capacity or None) if space is not None else None

    def refresh(self, progress=None) -> bool:
        """What was listed is dropped: the next look asks the device (its sizes now); and the
        storages the device has are asked again (a memory card taken out or put back while the
        session stays open: the device lists it again at once)."""
        self._responding()
        with self._lock:
            self._forget_listed()
            if self._owner is None:
                self._rescan_storages()
        return True

    def _rescan_storages(self) -> None:
        """`others` as the device lists its storages now: one gone is closed and dropped, one
        new opened beside; nothing changes when the device would not say (it is asked again at
        the next refresh)."""
        try:
            stores = [s for s in self._session.storages() if s != self.store]
        except ptp.PtpError as e:
            log.info("device: storages not asked again (%s)", type(e).__name__)
            return
        kept = {o.store: o for o in self.others}
        if list(kept) == stores:
            return
        now = []
        for store in stores:
            if store in kept:
                now.append(kept[store])
                continue
            try:
                info = self._session.storage_info(store)
            except ptp.PtpError:
                info = None
            now.append(MacStorage(self._session, store, self.usb, self.device_uri,
                                  info.description if info else "",
                                  removable=info.removable if info else None))
        for store, gone in kept.items():
            if store not in stores:
                gone._closed = True  # e.g. a memory card taken out
        self.adopt(now)
        self.other_storages = tuple(o.description or f"{o.store:08x}" for o in now)
        log.info("device: its storages now: %s", ", ".join(
            [self.description or "this one", *self.other_storages]))

    def exclusive_ptp(self):
        """The session of the storage, lent for the block (no unmount: nothing else holds the
        device); what was listed is dropped after it, as anything may have changed."""
        self._responding()
        return self._lent()

    @contextmanager
    def _lent(self):
        with self._lock:
            try:
                yield self._session
            finally:
                self._forget_listed()
                if self._session.broken and not self.stalled:
                    self._stall(_("Cannot read the device"), ptp.PtpError("out of step"))


# --- the storages open, the devices that stopped responding ---------------------------------
#
# A device is opened once in a process: the storage of a device found again (a probe, then a
# command) is the one already open. Each is closed at exit, so that the camera service goes on
# and the device ends its session.

_open: dict[tuple[int, int], MacStorage] = {}
_open_lock = threading.Lock()
# The places on USB of devices that stopped responding, and when. Such a device is left out of
# what is found for STALL_RETRY_S, then tried again: opening it cancels the transaction it was
# left in (ptp.Session.open), which brings back a device that stays at its place (a fēnix 8,
# its USB connection reset as it started), where gvfs on Linux can only wait for a new place.
_stalled_at: dict[tuple[int, int], float] = {}
STALL_RETRY_S = 10.0
_clock = time.monotonic


def _forget_open(storage: MacStorage) -> None:
    with _open_lock:
        for place, open_storage in list(_open.items()):
            if open_storage is storage:
                del _open[place]


def close_all() -> None:
    """Every storage open closed (at exit)."""
    with _open_lock:
        storages = list(_open.values())
    for storage in storages:
        try:
            storage.close()
        except DeviceNotResponding:
            log.info("device: not closed at exit, it stopped responding")  # the others still are
        except Exception as e:  # noqa: BLE001 - at exit: the device goes when the process does
            log.info("device: not closed at exit (%s)", type(e).__name__)


atexit.register(close_all)


def note_stalled(usb: GarminUsb | None) -> None:
    """The device at `usb` stopped responding: left out of what is found for STALL_RETRY_S."""
    place = usb_place(usb)
    if place is not None:
        _stalled_at[place] = _clock()


def _still_stalled(places: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """The places of devices that stopped responding that are still taken and not tried again
    yet; the others are forgotten."""
    now = _clock()
    for place, since in list(_stalled_at.items()):
        if place not in places or now - since >= STALL_RETRY_S:
            del _stalled_at[place]
    return set(_stalled_at)


# --- finding the devices --------------------------------------------------------------------

def _usb_devices(reuse: bool = False) -> list[mac_usb.UsbDevice]:
    """The Garmin devices on USB but those that stopped responding (`reuse`: as
    mac_usb.garmin_devices)."""
    devices = mac_usb.garmin_devices(reuse=True) if reuse else mac_usb.garmin_devices()
    stalled = _still_stalled({(d.bus, d.address) for d in devices})
    return [d for d in devices if (d.bus, d.address) not in stalled]


def _offering_mtp(devices: list[mac_usb.UsbDevice]) -> list[mac_usb.UsbDevice]:
    return [d for d in devices if d.mode == PRESENT and d.file_transfer]


def _found(d: mac_usb.UsbDevice) -> FoundDevice:
    return FoundDevice(device_uri(d.bus, d.address),
                       d.name or pgettext("in a sentence", "Garmin device"), True,
                       usb=d.garmin_usb())


def find_devices() -> list[FoundDevice]:
    """The Garmin devices on USB that offer MTP (a file transfer interface, not in their Garmin
    USB mode). Nothing is opened."""
    return [_found(d) for d in _offering_mtp(_usb_devices())]


def poll() -> tuple[list[FoundDevice], dict[tuple[int, int], str]]:
    """For the engine's polling, from one look at USB: what find_all_devices finds, and the
    holders() of them. The IORegistry (a run of `ioreg`) is read again only when the Garmin
    devices on USB changed, or a while after another program was seen holding one
    (mac_usb.garmin_devices, `reuse`); the name of a holder is asked once (mac_usb)."""
    on_usb = _usb_devices(reuse=True)
    devices = _offering_mtp(on_usb)
    held = {(d.bus, d.address): d.holder.name for d in devices
            if d.holder is not None and not d.holder.this_process
            and not d.holder.camera_daemon}
    # The drives only while one is on USB (or one is named for testing): their disks are
    # asked of the IORegistry and diskutil at each look.
    _look_at_usb([*on_usb, *_test_drives_on_usb()], _clock())
    drives = (find_ums_devices()
              if any(d.mode == MASS_STORAGE for d in on_usb) or _test_drive() is not None
              else [])
    return [*(_found(d) for d in devices), *drives], held


def look_again() -> None:
    """The next poll reads the IORegistry: a request found the device held."""
    mac_usb.look_again()


def wanted_elsewhere(found: FoundDevice) -> bool:
    """Whether another process of the program waits for this device, which this one holds
    (mac_usb.check_free): the engine lets it go then."""
    place = usb_place(found.usb)
    return found.kind != "ums" and place is not None and mac_usb.wanted_elsewhere(*place)


def find_all_devices() -> list[FoundDevice]:
    """Every Garmin device found, MTP devices first, then Garmin drives (`kind` tells them
    apart), for a list to choose from. Nothing is opened or mounted, and of a drive only the
    folders on the way to its GarminDevice.xml are looked at."""
    return [*find_devices(), *find_ums_devices()]


def mount(dev: FoundDevice) -> None:
    """Nothing to mount: an MTP device is opened over USB as it is (a drive: mount_drive)."""


def usb_state(clock=None) -> str:
    """As gio_storage.usb_state, from libusb: ABSENT, STARTING (only 091e:0003 besides what
    cannot be used), PRESENT, MASS_STORAGE, UNSUPPORTED, or DRIVE_OFF: those that are not
    drives (or cannot be used) are 091e:0003 at a port where a Garmin drive was (_drive_ports),
    a watch in its Garmin USB mode after its drive was ejected."""
    devices = _usb_devices()
    _look_at_usb([*devices, *_test_drives_on_usb()], (clock or _clock)())
    if not devices:
        return ABSENT
    others = [d for d in devices if d.mode not in (MASS_STORAGE, UNSUPPORTED)]
    if not others:
        return MASS_STORAGE if any(d.mode == MASS_STORAGE for d in devices) else UNSUPPORTED
    if any(d.mode == PRESENT for d in others):
        return PRESENT
    with _drives_lock:
        drives = all(d.port and d.port in _drive_ports for d in others)
    return DRIVE_OFF if drives else STARTING


def garmin_products_on_usb() -> list[str]:
    return [d.product for d in mac_usb.garmin_devices()]


def garmin_usb_summary() -> list[tuple[str, str | None, list[str | None]]]:
    """For a report without a device: (USB ID, the name it gives USB if any, the classes of
    its interfaces) of each Garmin device on USB; never its serial number."""
    return [(d.usb_id, d.name, list(d.classes)) for d in mac_usb.garmin_devices()]


def holders() -> dict[tuple[int, int], str]:
    """The other programs that hold a Garmin device for themselves (the IORegistry), by its
    place on USB: not this process, nor the camera service, which is paused while the device
    is open."""
    return {place: r.holder.name for place, r in mac_usb.registered_devices().items()
            if r.holder is not None and not r.holder.this_process
            and not r.holder.camera_daemon}


def mtp_missing() -> bool:
    """MTP is the program's own here: never missing."""
    return False


def garmin_mode_text() -> str:
    """A Garmin device on USB only as 091e:0003, its Garmin USB mode."""
    # TRANSLATORS: macOS: the only Garmin device on USB is in its Garmin USB mode (USB ID
    # 091e:0003); "Garmin USB mode" as Garmin calls it
    return _("The device is on USB in its Garmin USB mode (091e:0003), which openExpress does "
             "not use. A watch is in it for about 20 seconds after it is plugged in: wait until "
             "it shows its usual screen. If it stays in it, unplug the cable and plug it in "
             "again.")


def not_ready_text(state: str) -> str:
    """What to tell the user when no device could be opened in this USB state."""
    if state == STARTING:
        return garmin_mode_text()
    if state == PRESENT:
        return files_not_offered_text()
    if state == MASS_STORAGE:
        return drive_notice_text()
    if state == DRIVE_OFF:
        return drive_off_text(ejected_update_waits())
    if state == UNSUPPORTED:
        found = next((d for d in mac_usb.garmin_devices() if d.mode == UNSUPPORTED), None)
        return unsupported_device_text(found.name if found else None,
                                       found.usb_id if found else f"{GARMIN_VID}:?")
    return no_device_text()


# --- opening one --------------------------------------------------------------------------

def open_device_storage(uri: str | None = None, wait: bool = False, notify=None,
                        sleep=time.sleep, clock=time.monotonic) -> MacStorage:
    """The storage of the device that has GARMIN/GarminDevice.xml, as gio_storage opens one
    (found.wait_for_device): raises DeviceError (NotThereYet, SeveralDevices,
    MassStorageNotice, UnsupportedDevice, NoDevice, DeviceInUse) when there is none to open.
    With `wait`, a Garmin device on USB that does not offer MTP yet (its Garmin USB mode) is
    waited for up to WAIT_FOR_DEVICE_S, and `notify` is called once with a line saying so. A
    device open in this process already (the engine keeps it between requests) is used as it
    is: nothing to wait for, and no line saying it was opened."""
    kept = _kept_open(place_of(uri)) if uri is not None else None
    if kept is not None:
        return kept
    return wait_for_device(
        lambda: _open_device_storage(uri), usb_state=usb_state, products=garmin_products_on_usb,
        not_ready_text=not_ready_text, starting_text=garmin_mode_text, log=log, over="USB",
        wait=wait, notify=notify, sleep=sleep, clock=clock)


def _open_device_storage(uri: str | None) -> MacStorage:
    found = find_devices()
    if uri is None:
        return open_found(one_device(found))
    place = place_of(uri)
    if place is None:
        # TRANSLATORS: macOS: --device-uri was given an address of another form; {example}
        # is one of the form it takes ("mtp://[usb:008,037]/")
        raise DeviceError(_("On macOS a device is named by its place on USB, as in {example} "
                            "(openexpress status lists them).").format(example=device_uri(8, 37)))
    chosen = next((d for d in found if usb_place(d.usb) == place), None)
    if chosen is None:
        raise NotThereYet(not_ready_text(ABSENT))
    return open_found(chosen)


def _kept_open(place: tuple[int, int] | None) -> MacStorage | None:
    """The storage open in this process of the device at this place on USB, if it can still be
    used (not closed, not stalled)."""
    with _open_lock:
        open_storage = _open.get(place) if place is not None else None
    if open_storage is not None and not open_storage.closed and not open_storage.stalled:
        return open_storage
    return None


def open_found(found: FoundDevice) -> MacStorage:
    """The storage of a device find_devices found: the one open already in this process, or
    one opened now (its session open, the storage that has GARMIN/GarminDevice.xml)."""
    place = usb_place(found.usb)
    open_storage = _kept_open(place)
    if open_storage is not None:
        return open_storage
    try:
        transport = mac_usb.open_transport(*place)
    except DeviceInUse:
        raise
    except ptp.PtpError as e:
        # TRANSLATORS: error: the USB device could not be opened ({error}: why)
        raise DeviceError(_("Cannot open the device: {error}").format(error=e)) from e
    session = ptp.Session(transport)
    try:
        session.open()
        log.info("PTP: %s", ptp.operations_line(session))
        st = _storage_with_device_file(session, found)
    except BaseException:
        session.close()  # the transport too: the camera service goes on
        raise
    with _open_lock:
        _open[place] = st
    return st


def hold(found: FoundDevice) -> MacStorage | None:
    """The device opened for a request of the engine, found by its poll: the storage kept open
    since the request before, what was listed of it dropped (the device may have written
    files meanwhile), or opened now (open_found). None for a device that stopped responding
    at its place: the request opens it its own way and says so; None for a drive, which has
    no session to keep (the request opens its folder)."""
    if found.kind == "ums":
        return None
    place = usb_place(found.usb)
    kept = _kept_open(place)
    if kept is not None:
        kept.refresh()
        return kept
    if place is None or place in _stalled_at:
        return None
    return open_found(found)


def close_found(found: FoundDevice) -> None:
    """The storage open of a device find_devices found, closed: the device let go (the engine
    lets it go between requests, so that another program may take it). Nothing when none is
    open, nor for a drive (nothing of it is held)."""
    if found.kind == "ums":
        return
    with _open_lock:
        open_storage = _open.get(usb_place(found.usb))
    if open_storage is not None:
        open_storage.close()


def _storage_with_device_file(session: ptp.Session, found: FoundDevice) -> MacStorage:
    try:
        stores = session.storages()
    except ptp.PtpError as e:
        raise DeviceError(_("Cannot open the device: {error}").format(error=e)) from e
    storages: list[tuple[MacStorage, ptp.StorageInfo | None]] = []
    chosen: MacStorage | None = None
    for store in stores:
        try:
            info = session.storage_info(store)
        except ptp.PtpRefused:
            info = None
        st = MacStorage(session, store, found.usb, found.uri,
                        info.description if info else "",
                        removable=info.removable if info else None)
        storages.append((st, info))
        if chosen is None and st.exists("GARMIN/GarminDevice.xml"):
            chosen = st
    _log_storages(storages, chosen)
    if chosen is None:
        raise NotThereYet(_("GARMIN/GarminDevice.xml not found on device {uri}").format(
            uri=found.uri))
    # A device with more than one storage (e.g. a Forerunner 645 Music's small Primary,
    # beside its Media one): music.py refuses to create its folder on the wrong one.
    chosen.other_storages = tuple(_storage_name(st, info) for st, info in storages
                                  if st is not chosen)
    # The others themselves, on the same session: a memory card among them
    # (openexpress.device.card).
    chosen.adopt([st for st, _info in storages if st is not chosen])
    return chosen


def _storage_name(st: MacStorage, info: ptp.StorageInfo | None) -> str:
    """A storage's name for the log and `Storage.other_storages`: its description, as the
    device gives it, or its store ID in hex when the device would not say it (PtpRefused) -
    never its serial number or unit ID."""
    return (info.description if info else "") or f"{st.store:08x}"


def _log_storages(storages: list[tuple[MacStorage, ptp.StorageInfo | None]],
                  chosen: MacStorage | None) -> None:
    """One line naming every storage the device has (never its serial number or unit ID),
    its capacity and free space where the device said them, and which one has
    GARMIN/GarminDevice.xml."""
    parts = []
    for st, info in storages:
        name = _storage_name(st, info)
        if info and info.capacity:
            parts.append(f"{name} ({human_size(info.capacity, localize=False)}, "
                         f"{human_size(info.free, localize=False)} free)")
        else:
            parts.append(name)
    which = _storage_name(chosen, next((i for s, i in storages if s is chosen), None)) \
        if chosen is not None else "none"
    log.info("device: %d storage%s: %s; GARMIN on %s", len(storages),
             "" if len(storages) == 1 else "s", ", ".join(parts), which)


# --- a short look at a device found, for a list to choose from -----------------------------

def probe(found: FoundDevice) -> DeviceSummary:
    """Read only GarminDevice.xml of a device from find_all_devices (found.probe). An MTP
    device is opened for it (a session of the program's own) and stays open, kept for what
    comes next (`storage`); of a drive only its folder is read."""
    return found_probe(found, _probe_storage, log)


def _probe_storage(found: FoundDevice) -> Storage | str:
    if found.kind != "ums":
        return open_found(found)
    path = _path_of(found.uri)
    if path is None:
        return UNREADABLE
    storage = UmsStorage(Path(path), found.uri, found.usb, found.name)
    return storage if storage.exists("GARMIN/GarminDevice.xml") else NO_DEVICE_FILE


# --- Garmin drives: found, mounted, opened --------------------------------------------------
#
# macOS mounts a drive by itself, under /Volumes. Its disks are those below its USB device in
# the IORegistry (mac_usb.drive_devices); where each is mounted, diskutil tells.

# The address of a Garmin drive that is not mounted, in the place of the folder it has not got
# yet: its disk.
UNMOUNTED_DRIVE = "ums-volume:/dev/"
_test_drive_said: set[str] = set()  # the warnings about the drive named for testing, told


def _test_drive() -> tuple[str, str] | None:
    """(vendor, product) of the USB drive named in TEST_DRIVE_ENV, for testing; None."""
    return test_drive_ids(_test_drive_said, log)


def _drives() -> list[mac_usb.UsbDevice]:
    """The Garmin devices in mass storage mode on USB, and the drive named for testing (said
    once in the log, as a warning), with their disks."""
    drives = mac_usb.drive_devices(_test_drive())
    for d in drives:
        if d.vendor != GARMIN_VID:
            named_for_testing(d.vendor, d.product, _test_drive_said, log)
    return drives


def _path_of(uri: str) -> str | None:
    """The folder of a file:// address; None for any other."""
    parsed = urlparse(uri)
    return unquote(parsed.path) if parsed.scheme == "file" and parsed.path else None


def find_ums_devices() -> list[FoundDevice]:
    """The Garmin devices in mass storage mode whose volume macOS mounted and that have
    GARMIN/GarminDevice.xml (in any letter case), then those whose disk is not mounted
    (`mounted` False: to be mounted when chosen, mount_drive). A drive ejected by openExpress
    is left out until it is unplugged; one whose disk has not come yet is only said in the
    log. Only the folders on the way to GarminDevice.xml are read."""
    drives = _drives()
    _look_at_usb(drives, _clock(), drives_only=True)
    found = []
    for d in drives:
        usb = d.garmin_usb()
        if ejected_by_us(usb):
            log.info("device: a Garmin drive (USB %s) ejected by openExpress, skipped until it "
                     "is unplugged", usb.usb_id)
            continue
        volumes = [v for v in (mac_usb.volume_info(m) for m in d.media) if v is not None]
        if not volumes:
            log.info("device: a Garmin drive (USB %s) has no disk mounted or to mount yet: "
                     "waiting for macOS", usb.usb_id)
            continue
        drive = _mounted_drive(d, usb, [v for v in volumes if v.mount_point is not None])
        if drive is None and not any(v.mount_point is not None for v in volumes):
            volume = volumes[0]
            log.info("device: a Garmin drive (USB %s) is not mounted, listed to be mounted",
                     usb.usb_id)
            drive = FoundDevice(UNMOUNTED_DRIVE + volume.bsd, volume.name or d.name or "", False,
                                volume, _with_volume(usb, volume), kind="ums")
        if drive is not None:
            found.append(drive)
    return found


def _with_volume(usb: GarminUsb, volume: mac_usb.Volume) -> GarminUsb:
    """`usb` with the UUID of its volume, hidden in the log at once."""
    from dataclasses import replace

    from openexpress import applog

    usb = replace(usb, volume_uuid=volume.uuid)
    applog.hide_usb(usb)
    return usb


def _mounted_drive(d: mac_usb.UsbDevice, usb: GarminUsb,
                   mounted: list[mac_usb.Volume]) -> FoundDevice | None:
    """The drive at the first of its mounted volumes that has GARMIN/GarminDevice.xml; None
    (said in the log, without the folder) when none has."""
    for volume in mounted:
        try:
            has_xml = LocalStorage(Path(volume.mount_point)).exists("GARMIN/GarminDevice.xml")
        except (DeviceError, OSError):
            has_xml = False
        if has_xml:
            return FoundDevice(Path(volume.mount_point).as_uri(),
                               volume.name or os.path.basename(volume.mount_point), True,
                               volume, _with_volume(usb, volume), kind="ums")
    if mounted:
        log.info("device: a Garmin drive (USB %s) without GARMIN/GarminDevice.xml, skipped",
                 usb.usb_id)
    return None


def mount_drive(dev: FoundDevice) -> FoundDevice:
    """A Garmin drive chosen while it is not mounted, mounted where macOS mounts it
    (`diskutil mount`): the drive as find_ums_devices finds a mounted one. DeviceError when it
    cannot be mounted."""
    what = _("Cannot mount the device")
    volume = dev.volume
    if not isinstance(volume, mac_usb.Volume):
        raise DeviceError(what)
    mac_usb.mount_volume(volume.bsd, what)
    mounted = mac_usb.volume_info(volume.bsd)
    if mounted is None or mounted.mount_point is None:
        raise DeviceError(what)
    log.info("device: mounted the Garmin drive (USB %s)",
             dev.usb.usb_id if dev.usb is not None else "?")
    return FoundDevice(Path(mounted.mount_point).as_uri(),
                       mounted.name or os.path.basename(mounted.mount_point), True, mounted,
                       dev.usb, kind="ums")


def open_ums_storage(dev: FoundDevice) -> UmsStorage:
    """The storage of a Garmin device in mass storage mode that find_ums_devices found."""
    path = _path_of(dev.uri)
    try:
        if path is None:
            raise DeviceError("not a local folder")
        st = UmsStorage(Path(path), dev.uri, dev.usb, dev.name)
    except DeviceError:
        # Unplugged since it was found.
        raise NotThereYet(_("The device is not mounted.")) from None
    log.info("device: opened in mass storage mode (%s:, USB %s)", dev.uri.split(":", 1)[0],
             st.usb_id or "?")
    return st


# --- a Garmin drive ejected by openExpress ---------------------------------------------------
#
# As on Linux (gio_storage): a Garmin device in mass storage mode may take a file written to it
# only once the computer lets go of its drive, so after a write the drive is ejected before
# the cable may go out. Ejected, it stays on USB without a disk (or comes back as 091e:0003, a
# watch in its Garmin USB mode): until it is unplugged openExpress does not open it again.
# Where a device is plugged in is its port (bus and hub ports: mac_usb.UsbDevice.port), which
# stays when it comes back under another device number.


@dataclass
class _Ejected:
    """A drive ejected by openExpress that is still plugged in, as far as is known: until its
    port is empty for USB_GAP_S, or another device than it (not in its Garmin USB mode) is
    there."""

    usb: GarminUsb
    port: tuple[int, ...]  # () when not known: it ends when its device number is gone
    update_waits: bool = False  # an update copied to it waits for the device to install it
    on_unplugged: Callable[[], None] | None = None  # told once, when it is seen unplugged
    empty_since: float | None = None
    garmin_usb_mode: bool = False  # 091e:0003 seen at its port since it was ejected


_ejected: list[_Ejected] = []
# The ports where a Garmin drive was seen in this run, with since when nothing is there (None:
# something is): a Garmin device 091e:0003 at such a port is that watch in its Garmin USB mode
# after its drive was ejected (DRIVE_OFF), not a device starting.
_drive_ports: dict[tuple[int, ...], float | None] = {}
_drives_lock = threading.Lock()


def _look_at_usb(devices: list[mac_usb.UsbDevice], now: float, drives_only: bool = False) -> None:
    """What is on USB now (`devices`; `drives_only`: the drives alone, so that nothing is
    taken for gone), for _drive_ports and the drives ejected by us; those seen unplugged are
    told (on_unplugged) and said in the log."""
    told = []
    with _drives_lock:
        here = {d.port for d in devices if d.port}
        for d in devices:
            if d.port and d.mode == MASS_STORAGE:
                _drive_ports[d.port] = None
            elif d.port and d.mode != STARTING:
                _drive_ports.pop(d.port, None)
        if not drives_only:
            for port, gone in list(_drive_ports.items()):
                if port in here:
                    _drive_ports[port] = None
                elif gone is None:
                    _drive_ports[port] = now
                elif now - gone >= USB_GAP_S:
                    del _drive_ports[port]
        for entry in list(_ejected):
            gone, unplugged, said = _look_at(entry, devices, now, drives_only)
            if gone:
                _ejected.remove(entry)
                log.info("device: the drive ejected by openExpress %s (USB %s)", said,
                         entry.usb.usb_id)
                if unplugged and entry.on_unplugged is not None:
                    told.append(entry.on_unplugged)
    for tell in told:  # outside the lock: it may write a file
        try:
            tell()
        except Exception as e:  # noqa: BLE001 - optional; only for what is said later
            log.warning("device: an unplugging not kept (%s)", type(e).__name__)


def _look_at(entry: _Ejected, devices: list[mac_usb.UsbDevice], now: float,
             drives_only: bool) -> tuple[bool, bool, str]:
    """(whether the ejected drive is gone, whether it was unplugged, what to say of it)."""
    usb = entry.usb
    itself = next((d for d in devices if (d.bus, d.address) == (usb.bus, usb.address)
                   and d.product == usb.product and d.vendor == usb.vendor), None)
    if itself is not None:
        entry.empty_since = None
        return False, False, ""  # still there, ejected
    there = next((d for d in devices if entry.port and d.port == entry.port), None)
    if there is None:
        if drives_only:
            return False, False, ""  # only the drives were looked at: not known
        if not entry.port:
            return True, False, "is not at its place on USB any more"
        if entry.empty_since is None:
            entry.empty_since = now
        if now - entry.empty_since >= USB_GAP_S:
            return True, True, "was unplugged"
        return False, False, ""
    entry.empty_since = None
    if there.product == STARTING_PRODUCT and there.vendor == GARMIN_VID:
        entry.garmin_usb_mode = True  # on the cable, in its Garmin USB mode
        return False, False, ""
    if entry.garmin_usb_mode:
        return True, False, "is back on USB, not seen unplugged"
    return True, True, "was unplugged and plugged in again"


def _test_drives_on_usb() -> list[mac_usb.UsbDevice]:
    """The drive named for testing on USB, when one of ours was ejected: not a Garmin device,
    so that its unplugging is seen too."""
    with _drives_lock:
        testing = any(e.usb.for_testing for e in _ejected)
    return [d for d in mac_usb.drive_devices(_test_drive()) if d.vendor != GARMIN_VID] \
        if testing else []


def _forget_unplugged() -> None:
    _look_at_usb([*_usb_devices(), *_test_drives_on_usb()], _clock())


def note_ejected(usb: GarminUsb, port: tuple[int, ...] = (), update_waits: bool = False,
                 on_unplugged: Callable[[], None] | None = None) -> None:
    """Keep that openExpress ejected the drive of `usb`, plugged in at `port`."""
    with _drives_lock:
        _ejected.append(_Ejected(usb, port, update_waits, on_unplugged))


def ejected_by_us(usb: GarminUsb | None) -> bool:
    """Whether this drive was ejected by openExpress and is still on USB: then it is neither
    opened nor listed, also when macOS mounts it again, until it is unplugged."""
    if usb is None:
        return False
    with _drives_lock:
        return any(e.usb == usb and (e.usb.bus, e.usb.address) == (usb.bus, usb.address)
                   and e.usb.vendor == usb.vendor for e in _ejected)


def ejected_drive_waits() -> bool:
    """Whether a drive ejected by openExpress is still plugged in: the cable can go out."""
    _forget_unplugged()
    with _drives_lock:
        return bool(_ejected)


def ejected_update_waits() -> bool:
    """Whether a drive ejected by openExpress with an update copied to it is still plugged
    in: the device installs it only once it is unplugged."""
    with _drives_lock:
        return any(e.update_waits for e in _ejected)


def eject(storage, update_waits: bool = False,
          on_unplugged: Callable[[], None] | None = None) -> None:
    """Eject the drive of a Garmin device in mass storage mode (`storage`: a UmsStorage):
    macOS writes out what it still holds, unmounts every volume of its disk and tells the
    device that it may go (`diskutil eject`). The drive is then not opened again until it is
    unplugged (ejected_by_us). `update_waits`: an update copied to it waits for the device;
    `on_unplugged`: called once it is seen unplugged (while this program looks at USB).
    DriveNotMounted when it is not mounted any more; DeviceError when macOS refuses, e.g.
    while a program uses the drive."""
    usb = getattr(storage, "usb", None)
    # Its port before it goes: a watch may switch to its Garmin USB mode at once.
    place = usb_place(usb)
    port = next((d.port for d in _drives() if place is not None
                 and (d.bus, d.address) == place), ())
    if port:
        with _drives_lock:
            _drive_ports[port] = None
    mac_usb.eject_drive_at(
        str(storage.root), _("Cannot eject the device"),
        _("The drive is not mounted any more. If it was ejected, you can unplug the cable."))
    if usb is not None:
        note_ejected(usb, port, update_waits, on_unplugged)
    log.info("device: ejected (USB %s)", getattr(storage, "usb_id", None) or "?")


def not_ejected_text(window: bool = True, files: bool = False) -> str:
    """After a failed eject: the cable must stay in, and what to do (`files`: after a sync
    wrote the files of Garmin Connect's queue, not an update). The same words in the app and
    on the command line (`window` is for gio_storage's names)."""
    if files:
        # TRANSLATORS: macOS: the drive of a Garmin device could not be ejected after a sync
        return _("Do not unplug the cable yet: the device may not see the files from Garmin "
                 "Connect. Close any program that uses the drive and eject it in the Finder.")
    # TRANSLATORS: macOS: the drive of a Garmin device could not be ejected after an update
    # was copied to it
    return _("Do not unplug the cable yet: the device may not see the update. Close any "
             "program that uses the drive and eject it in the Finder.")


def drive_off_text(update_waits: bool = False) -> str:
    """A watch back on USB as 091e:0003 after its drive was ejected (DRIVE_OFF)."""
    if update_waits:
        return _("After it was ejected, the watch switched to its Garmin USB mode. Unplug the "
                 "cable and leave the device unplugged so it can install the update.")
    return _("After it was ejected, the watch switched to its Garmin USB mode, which "
             "openExpress does not use. Unplug the cable and plug it in again.")


def drive_notice_text() -> str:
    """A Garmin drive on USB that cannot be opened: ejected by openExpress, or its disk not
    mounted (yet)."""
    if ejected_drive_waits():
        return ejected_text()
    # TRANSLATORS: macOS: a Garmin device is on USB as a drive, but macOS has not mounted it
    return _("The Garmin device is connected as a USB drive, but macOS has not mounted it. "
             "Wait a moment; if it does not appear in the Finder, unplug the cable and plug "
             "it in again.")

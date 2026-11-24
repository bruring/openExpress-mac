"""A fake Garmin device at the other end of a PTP transport, answering as the fēnix 8 does
and listing the operations it lists:

* a folder the device took over, and its app files, are missing from the ordinary listing;
  only Garmin's 0x9002 lists them, with protection 1;
* a file just written is listed as usual, with protection 0;
* an app file (.PRG) reads back other bytes than were written;
* SendObjectInfo takes storage and parent and returns the new handle; a folder it makes only
  when `allows_folders` (the fēnix 8 refuses one in GARMIN/Apps), and only one described as
  libmtp describes it (an association of the type "generic folder");
* DeleteObject removes a protected app file too, as seen when Garmin Express removes an app
  (`delete_protected`: the fake refuses instead);
* an object of the top level names 0 as its parent in its description (`top_parent`), as the
  fēnix 8 and the Edge 1050 do in both listings, though the listings are asked with ROOT;
* 0x9002 lists only what the ordinary listing does not, every object in the folder asked, as
  on those two devices; `also_in_garmins` and `garmins_extra` make it answer otherwise.

Everything in the tree is made up.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from openexpress.device import ptp

STORAGE = 0x00010001
# What GetDeviceInfo lists: a fēnix 8's operations, and a Drive 53 EX's (an Edge 1050's
# without 0x900A).
FENIX_8_OPERATIONS = [
    0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1007, 0x1008, 0x1009, 0x100A, 0x100B, 0x100C, 0x100D,
    0x1010, 0x1014, 0x1015, 0x1016, 0x1019, 0x101B, 0x9000, 0x9001, 0x9002, 0x9003, 0x9004, 0x9005,
    0x9006, 0x9007, 0x9008, 0x9009, 0x900A, 0x900B, 0x9801, 0x9802, 0x9803, 0x9804, 0x9805, 0x9810,
    0x9811]
DRIVE_53_OPERATIONS = [
    0x1001, 0x1002, 0x1003, 0x1004, 0x1005, 0x1007, 0x1008, 0x1009, 0x100B, 0x100C, 0x100D, 0x1010,
    0x1014, 0x1015, 0x1016, 0x1019, 0x101B, 0x9000, 0x9001, 0x9002, 0x9003, 0x9004, 0x9005, 0x9006,
    0x9008, 0x9009, 0x9801, 0x9802, 0x9803, 0x9804, 0x9805]


@dataclass
class FakeObject:
    name: str
    parent: int
    data: bytes | None = None  # None: a folder
    hidden: bool = False
    listed_size: int | None = None  # listed at this size instead of its bytes' (never read)
    modified: str = ""  # its ModificationDate, as a PTP date ("20261117T093000")

    @property
    def protection(self) -> int:
        return 1 if self.hidden else 0


@dataclass
class _ExtraStorage:
    """A storage besides the fake's own (STORAGE, `self.free`/`self.objects`): empty of files,
    as a small Primary storage is beside a device's Media one (test_mac_storage, more than
    one storage)."""
    description: str
    capacity: int
    free: int
    kind: int = 3  # StorageType: 3 FixedRAM, 4 RemovableRAM (a memory card)


class FakeDevice:
    """Implements `ptp.Transport`. `transfers` records every transfer written to it."""

    def __init__(self, packet_size: int = 512, read_chunk: int | None = None):
        self.packet_size = packet_size
        self.read_chunk = read_chunk  # split answers into reads of this size
        self.objects: dict[int, FakeObject] = {}
        self.free = 500 * 1024 * 1024
        # Every storage ID the device lists (GetStorageIds): STORAGE (self.free, self.objects)
        # plus, for a test of a device with more than one, each added by add_storage (empty,
        # of its own capacity and free space).
        self.extra_storages: dict[int, _ExtraStorage] = {}
        self.refuse: dict[int, int] = {}  # operation -> response code
        self.refuse_names: dict[str, int] = {}  # file name -> code, at SendObjectInfo
        self.refuse_delete_names: dict[str, int] = {}  # file name -> code, at DeleteObject
        self.supports_hidden = True
        self.allows_folders = False  # whether SendObjectInfo of a folder makes one
        self.top_parent = 0  # the parent a top-level object names in its description
        self.also_in_garmins: set[int] = set()  # ordinary objects 0x9002 lists too
        # folder -> objects of other folders that 0x9002 lists in it too
        self.garmins_extra: dict[int, list[int]] = {}
        self.delete_protected = ptp.OK  # or a refusal such as ptp.OBJECT_WRITE_PROTECTED
        self.session_open = False
        self.wrong_transaction = False
        self.silent = False
        self.truncate_written = 0  # bytes cut from the next stored file
        # What GetDeviceInfo lists; the strings after it are made up, and must never be read.
        self.supported = list(FENIX_8_OPERATIONS)
        self.transfers: list[bytes] = []
        self.timeout_ms = ptp.TIMEOUT_MS  # set by the session, as on a real transport
        self.read_limits: list[int] = []  # the time limit in force at each read
        self.operations: list[int] = []
        self.closed = False
        self._next = 0x100
        self._out = b""
        self._pending: tuple[int, int, tuple[int, ...]] | None = None  # awaiting data
        self._announced: tuple[int, int] | None = None  # (handle, size) from SendObjectInfo
        self._answers: list[bytes] = []

    # --- building the tree ---------------------------------------------------------------

    def add(self, path: str, data: bytes | None = None, hidden: bool = False) -> int:
        """Add a folder (no data) or a file; missing parents become ordinary folders."""
        parent = ptp.ROOT
        parts = [p for p in path.split("/") if p]
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            found = next((h for h, o in self.objects.items()
                          if o.parent == parent and o.name == part), None)
            if found is None:
                found = self._next
                self._next += 1
                self.objects[found] = FakeObject(part, parent, data if last else None,
                                                 hidden if last else False)
            parent = found
        return parent

    def add_storage(self, store_id: int, description: str, capacity: int, free: int,
                    kind: int = 3) -> None:
        """A second storage the device has, besides STORAGE: empty of files (GetObjectHandles
        of it returns none), its own description, capacity, free space and StorageType
        (GetStorageInfo; 4: a memory card)."""
        self.extra_storages[store_id] = _ExtraStorage(description, capacity, free, kind)

    def add_twin(self, path: str, name: str | None = None, data: bytes | None = None) -> int:
        """A hidden object beside the one at `path`, in the same folder, called `name` (by
        default the same name): a second object under another handle."""
        parent = self.objects[self.handle(path)].parent
        handle = self._next
        self._next += 1
        self.objects[handle] = FakeObject(name or path.rsplit("/", 1)[-1], parent, data, True)
        return handle

    def handle(self, path: str) -> int | None:
        parent = ptp.ROOT
        for part in [p for p in path.split("/") if p]:
            parent = next((h for h, o in self.objects.items()
                           if o.parent == parent and o.name == part), None)
            if parent is None:
                return None
        return parent

    def names(self, path: str, hidden: bool | None = None) -> list[str]:
        parent = self.handle(path)
        return sorted(o.name for o in self.objects.values()
                      if o.parent == parent and hidden in (None, o.hidden))

    def take_over(self, folder: str, replaces: dict[str, str] | None = None) -> None:
        """What the device does after it is unplugged: the files in an app folder
        become its own. `replaces` maps a new file to the old file of the same app: the
        device keeps the old name with the new content (seen with an update)."""
        parent = self.handle(folder)
        for new, old in (replaces or {}).items():
            new_handle, old_handle = self.handle(f"{folder}/{new}"), self.handle(f"{folder}/{old}")
            self.objects[old_handle].data = self.objects.pop(new_handle).data
        for o in self.objects.values():
            if o.parent == parent:
                o.hidden = True

    def reconnect(self) -> None:
        """The cable was unplugged and plugged in again."""
        self.closed = self.session_open = False
        self._out, self._pending, self._announced, self._answers = b"", None, None, []

    def sent(self, operation: int) -> int:
        return self.operations.count(operation)

    # --- transport -----------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        assert not self.closed
        self.transfers.append(bytes(data))
        if self.silent:
            return
        self._out += data
        while len(self._out) >= ptp.HEADER.size:
            length, kind, code, transaction = ptp.HEADER.unpack_from(self._out)
            if len(self._out) < length:
                return
            payload, self._out = self._out[ptp.HEADER.size:length], self._out[length:]
            if kind == ptp.COMMAND:
                params = struct.unpack(f"<{len(payload) // 4}I", payload)
                self.operations.append(code)
                if code in (ptp.SEND_OBJECT_INFO, ptp.SEND_OBJECT):
                    self._pending = (code, transaction, params)
                else:
                    self._respond(code, transaction, *self._handle(code, params, None))
            else:
                assert kind == ptp.DATA and self._pending is not None
                operation, expected, params = self._pending
                assert (code, transaction) == (operation, expected)
                self._pending = None
                self._respond(operation, transaction, *self._handle(operation, params, payload))

    def read(self, size: int) -> bytes:
        assert not self.closed
        self.read_limits.append(self.timeout_ms)
        if not self._answers:
            return b""
        answer = self._answers.pop(0)
        limit = min(size, self.read_chunk or size)
        if len(answer) > limit:
            self._answers.insert(0, answer[limit:])
        return answer[:limit]

    def close(self) -> None:
        self.closed = True

    def _respond(self, operation: int, transaction: int, code: int, data: bytes | None = None,
                 params: tuple[int, ...] = ()) -> None:
        if self.wrong_transaction:
            transaction += 7
        if data is not None:
            self._answers.append(ptp.data_header(operation, transaction, len(data)) + data)
        self._answers.append(struct.pack(f"<IHHI{len(params)}I", 12 + 4 * len(params),
                                         ptp.RESPONSE, code, transaction, *params))

    # --- operations ----------------------------------------------------------------------

    def _handle(self, operation, params, data):
        if operation == ptp.OPEN_SESSION:
            if self.session_open:
                return (ptp.SESSION_ALREADY_OPEN,)
            if operation in self.refuse:
                return (self.refuse[operation],)
            self.session_open = True
            return (ptp.OK,)
        if not self.session_open:
            return (ptp.SESSION_NOT_OPEN,)
        if operation == ptp.CLOSE_SESSION:
            self.session_open = False
            return (ptp.OK,)
        if operation in self.refuse:
            return (self.refuse[operation],)
        if operation == ptp.GET_DEVICE_INFO:
            return ptp.OK, self.device_info()
        if operation == ptp.GET_STORAGE_IDS:
            ids = [STORAGE, *self.extra_storages]
            return ptp.OK, struct.pack(f"<I{len(ids)}I", len(ids), *ids)
        if operation == ptp.GET_STORAGE_INFO:
            extra = self.extra_storages.get(params[0]) if params else None
            if params and params[0] != STORAGE and extra is None:
                return (ptp.INVALID_STORAGE_ID,)
            capacity, free, description, kind = (
                (extra.capacity, extra.free, extra.description, extra.kind) if extra is not None
                else (2 * self.free, self.free, "Internal", 3))
            return ptp.OK, (struct.pack("<HHHQQI", kind, 2, 0, capacity, free, 0xFFFFFFFF)
                            + ptp.ptp_string(description) + ptp.ptp_string(""))
        if operation in (ptp.GET_OBJECT_HANDLES, ptp.GARMIN_GET_OBJECT_IDS):
            hidden = operation == ptp.GARMIN_GET_OBJECT_IDS
            if hidden and not self.supports_hidden:
                return (ptp.OPERATION_NOT_SUPPORTED,)
            parent = params[1] if hidden else params[2]
            if params[0] in self.extra_storages:
                return ptp.OK, struct.pack("<I", 0)  # an extra storage has no files of its own
            if params[0] != STORAGE:
                return (ptp.INVALID_STORAGE_ID,)
            if parent != ptp.ROOT and (parent not in self.objects
                                       or self.objects[parent].data is not None):
                return (ptp.INVALID_PARENT_OBJECT,)
            handles = [h for h, o in self.objects.items()
                       if o.parent == parent and (o.hidden == hidden
                                                  or (hidden and h in self.also_in_garmins))]
            if hidden:
                handles += self.garmins_extra.get(parent, [])
            return ptp.OK, struct.pack(f"<I{len(handles)}I", len(handles), *handles)
        if operation == ptp.SEND_OBJECT_INFO:
            return self._send_object_info(params, data)
        if operation == ptp.SEND_OBJECT:
            return self._send_object(data)
        obj = self.objects.get(params[0]) if params else None
        if obj is None:
            return (ptp.INVALID_OBJECT_HANDLE,)
        if operation == ptp.GET_OBJECT_INFO:
            return ptp.OK, self._object_info(obj)
        if operation == ptp.GET_OBJECT:
            if obj.data is None:
                return (ptp.INVALID_OBJECT_HANDLE,)
            if obj.name.upper().endswith(".PRG"):
                return ptp.OK, bytes(b ^ 0xA5 for b in obj.data)  # "encrypted"
            return ptp.OK, obj.data
        if operation == ptp.GET_PARTIAL_OBJECT:
            if obj.data is None:
                return (ptp.INVALID_OBJECT_HANDLE,)
            part = obj.data[params[1]:params[1] + params[2]]
            return ptp.OK, part, (len(part),)
        if operation == ptp.DELETE_OBJECT:
            if obj.name in self.refuse_delete_names:
                return (self.refuse_delete_names[obj.name],)
            if obj.protection and self.delete_protected != ptp.OK:
                return (self.delete_protected,)
            for h in [params[0]] + [h for h, o in self.objects.items() if o.parent == params[0]]:
                del self.objects[h]
            return (ptp.OK,)
        return (ptp.OPERATION_NOT_SUPPORTED,)

    def device_info(self) -> bytes:
        """A DeviceInfo dataset: versions, Garmin's extension, the operations supported, then
        events, properties, formats and the device's strings (all made up)."""
        def u16s(values) -> bytes:
            return struct.pack(f"<I{len(values)}H", len(values), *values)

        return (struct.pack("<HIH", 100, 6, 100) + ptp.ptp_string("microsoft.com: 1.0;")
                + struct.pack("<H", 0) + u16s(self.supported) + u16s([0x4002, 0x4003])
                + u16s([0xD402]) + u16s([]) + u16s([0x3000, 0x3001])
                + ptp.ptp_string("Made-up Maker") + ptp.ptp_string("Made-up Model 1")
                + ptp.ptp_string("9.99") + ptp.ptp_string("MADEUPSERIAL0000"))

    def _object_info(self, obj: FakeObject) -> bytes:
        fmt = ptp.FORMAT_FOLDER if obj.data is None else ptp.FORMAT_UNDEFINED
        size = obj.listed_size if obj.listed_size is not None else len(obj.data or b"")
        parent = self.top_parent if obj.parent == ptp.ROOT else obj.parent
        return (struct.pack("<IHHI", STORAGE, fmt, obj.protection, size)
                + struct.pack("<HIIIIIIIHII", 0, 0, 0, 0, 0, 0, 0, parent, 0, 0, 0)
                + ptp.ptp_string(obj.name) + ptp.ptp_string("") + ptp.ptp_string(obj.modified)
                + ptp.ptp_string(""))

    def _send_object_info(self, params, data):
        self._announced = None
        storage, parent = params[0], params[1]
        if storage != STORAGE:
            return (ptp.INVALID_STORAGE_ID,)
        if parent != ptp.ROOT and (parent not in self.objects
                                   or self.objects[parent].data is not None):
            return (ptp.INVALID_PARENT_OBJECT,)
        fmt, size = struct.unpack_from("<H", data, 4)[0], struct.unpack_from("<I", data, 8)[0]
        name, _end = ptp.read_string(data, 52)
        if fmt == ptp.FORMAT_FOLDER:
            if not self.allows_folders:
                return (ptp.ACCESS_DENIED,)  # the device does not let folders be created here
            if struct.unpack_from("<H", data, 42)[0] != ptp.GENERIC_FOLDER:
                return (ptp.INVALID_PARAMETER,)
            handle = self._next
            self._next += 1
            self.objects[handle] = FakeObject(name, parent)
            return ptp.OK, None, (storage, parent, handle)
        if name in self.refuse_names:
            return (self.refuse_names[name],)
        if size > self.free:
            return (ptp.STORE_FULL,)
        handle = self._next
        self._next += 1
        self.objects[handle] = FakeObject(name, parent, b"")
        self._announced = (handle, size)
        return ptp.OK, None, (storage, parent, handle)

    def _send_object(self, data):
        if self._announced is None:
            return (ptp.NO_VALID_OBJECT_INFO,)
        (handle, size), self._announced = self._announced, None
        if len(data) != size:
            return (ptp.GENERAL_ERROR,)
        if self.truncate_written:
            data, self.truncate_written = data[:-self.truncate_written], 0
        self.objects[handle].data = bytes(data)
        self.free -= len(data)
        return (ptp.OK,)

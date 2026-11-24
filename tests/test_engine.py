"""The engine (openexpress engine, docs/ENGINE.md): JSON Lines between the macOS app and the
program. Driven in-process with made-up devices (a folder each, as `--path` opens one) and a
Garmin server of the tests' own; the protocol's own stdout is kept apart from what the code
prints. All devices, accounts and tickets are made up."""

import datetime
import http.server
import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest
from test_ownmaps import img_header

import openexpress
from openexpress import __version__, activities, backup, engine
from openexpress import settings as settings_mod
from openexpress.device.found import DeviceSummary, FoundDevice
from openexpress.device.storage import LocalStorage
from openexpress.device.xmldevice import parse
from openexpress.i18n import caption
from openexpress.progress import Step, copying_text, downloading_text

UNIT = "1234567890"  # conftest.DEVICE_XML
KEY = "mtp://[usb:001,009]/"
# Every method of the contract (docs/ENGINE.md, Methods).
CONTRACT = {
    "hello", "account.status", "account.setup", "account.sign_in_url", "account.sign_in",
    "account.sign_out", "devices.list", "device.info", "device.backup", "device.sync",
    "updates.list", "updates.install", "maps.list", "maps.install", "maps.update",
    "maps.remove", "apps.list", "apps.install", "apps.remove", "activities.list",
    "settings.get", "settings.set", "strava.status", "strava.connect", "strava.disconnect",
    "cancel", "shutdown",
    "strava.upload",  # besides the contract: as `strava upload`
    "backups.list", "activities.track",  # what the app asked for after it
    "account.readings",
    "maps.move",  # a map between the device's own storage and its memory card
    "maps.card_reader"}  # the device's card in this computer's card reader


class FakeDevices:
    """The devices of the tests: a folder each, by its key (as a USB address); `held`: the
    program that holds one, by key."""

    def __init__(self, **roots):
        self.roots = dict(roots)
        self.held: dict[str, str] = {}
        self.released: list[str] = []
        self.holds: list[str] = []  # each device opened for a request (hold)
        self.waiting: set[str] = set()  # devices another process of the program waits for
        self.looks_again = 0

    def scan(self):
        return [engine.Seen(key, FoundDevice(key, "Test watch", True), self.held.get(key))
                for key in self.roots]

    def probe(self, found):
        st = LocalStorage(self.roots[found.uri])
        dev = parse(st.read("GARMIN/GarminDevice.xml"))
        return DeviceSummary(found, dev.description, dev.part_number, dev.software_version_str,
                             dev.unit_id)

    def options(self, found):
        return {"path": str(self.roots[found.uri])}

    def hold(self, found):
        self.holds.append(found.uri)

    def look_again(self):
        self.looks_again += 1

    def wanted(self, found):
        return found.uri in self.waiting

    comes_back = True  # a device that stopped responding answers again (back)

    def back(self, found):
        return self.comes_back

    def release(self, found):
        self.released.append(found.uri)


class Lines:
    """The engine's stdin: a line at a time, as the app writes them."""

    def __init__(self):
        self._q = queue.Queue()

    def readline(self):
        return self._q.get()

    def send(self, message) -> None:
        self._q.put((message if isinstance(message, str) else json.dumps(message)) + "\n")

    def close(self) -> None:
        self._q.put("")


class Out:
    """The engine's stdout: every write kept, each read as a line of JSON."""

    def __init__(self):
        self.written: list[str] = []
        self._cond = threading.Condition()

    def write(self, text):
        with self._cond:
            self.written.append(text)
            self._cond.notify_all()
        return len(text)

    def flush(self):
        pass

    def messages(self) -> list[dict]:
        with self._cond:
            text = "".join(self.written)
        return [json.loads(line) for line in text.splitlines()]

    def wait_for(self, test, timeout=10.0):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                found = [m for m in self.messages_unlocked() if test(m)]
                if found:
                    return found[0]
                left = deadline - time.monotonic()
                assert left > 0, f"not seen; the engine wrote: {self.written}"
                self._cond.wait(left)

    def messages_unlocked(self) -> list[dict]:
        return [json.loads(line) for line in "".join(self.written).splitlines()]


class Running:
    """An engine serving on a thread of its own: ask() sends a request and returns its
    response."""

    def __init__(self, devices, keep_open_s=engine.KEEP_OPEN_S):
        self.stdin, self.out = Lines(), Out()
        self.engine = engine.Engine(self.out, devices=devices, poll_s=None,
                                    keep_open_s=keep_open_s)
        self.code = None
        self._ids = iter(range(1, 10_000))
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.code = self.engine.serve(self.stdin)

    def send(self, method, params=None) -> int:
        request_id = next(self._ids)
        self.stdin.send({"id": request_id, "method": method, "params": params or {}})
        return request_id

    def response(self, request_id, timeout=10.0) -> dict:
        return self.out.wait_for(lambda m: m.get("id") == request_id
                                 and ("result" in m or "error" in m), timeout)

    def ask(self, method, params=None) -> dict:
        return self.response(self.send(method, params))

    def events(self, name, request=None) -> list[dict]:
        return [m["params"] for m in self.out.messages() if m.get("event") == name
                and (request is None or m["params"].get("request") == request)]

    def stop(self):
        self.stdin.close()
        self.thread.join(10)


@pytest.fixture
def devices(device_root):
    return FakeDevices(**{KEY: device_root})


@pytest.fixture
def running(devices):
    r = Running(devices)
    yield r
    r.stop()


@pytest.fixture
def ticking_clock(monkeypatch):
    """A distinct second for each backup (they are named by the second)."""
    import datetime

    class Clock(datetime.datetime):
        moments = iter(datetime.datetime(2026, 10, 20, 9, 0, n) for n in range(60))

        @classmethod
        def now(cls, tz=None):
            return next(cls.moments)

    monkeypatch.setattr(backup, "datetime", Clock)


# --- the protocol ------------------------------------------------------------------------

def test_hello_names_the_engine_its_version_platform_and_every_method_of_the_contract(running):
    result = running.ask("hello", {"client": "openExpress-mac/0.1"})["result"]
    assert result["engine"] == __version__
    assert result["platform"] == sys.platform
    assert set(result["methods"]) == CONTRACT


def test_a_line_that_is_not_json_is_answered_with_parse_error_and_the_engine_goes_on(running):
    running.stdin.send("{this is not json")
    error = running.out.wait_for(lambda m: "error" in m)
    assert error["id"] is None and error["error"]["code"] == "parse_error"
    assert "result" in running.ask("hello")


def test_an_unknown_method_and_a_request_without_id_are_errors_with_stable_codes(running):
    unknown = running.ask("maps.fly", {})
    assert unknown["error"]["code"] == "unknown_method"
    assert "maps.fly" in unknown["error"]["message"]
    running.stdin.send({"method": "hello"})
    invalid = running.out.wait_for(lambda m: m.get("error", {}).get("code") == "invalid_request")
    assert invalid["id"] is None
    running.stdin.send({"id": 99, "method": "hello", "params": [1, 2]})
    assert running.response(99)["error"]["code"] == "invalid_request"


def test_shutdown_answers_last_and_ends_the_engine(running):
    request_id = running.send("shutdown")
    running.thread.join(10)
    assert not running.thread.is_alive() and running.code == 0
    assert running.out.messages()[-1] == {"id": request_id, "result": {}}


# --- devices -------------------------------------------------------------------------------

def test_devices_list_tells_the_connected_device_by_its_unit_id_as_a_string_and_attaches_it(
        running):
    devices = running.ask("devices.list")["result"]["devices"]
    assert devices == [{
        "unit_id": UNIT, "name": "Test watch", "part_number": "006-B0000-00",
        "software": "23.39", "connected": True, "busy_by": None, "storage": None,
        "last_sync": None, "last_backup": None}]
    attached = running.events("device.attached")
    assert [d["unit_id"] for d in attached] == [UNIT]


def test_a_device_unplugged_is_told_detached_and_no_longer_listed(running, devices):
    running.ask("devices.list")
    devices.roots.clear()
    running.ask("devices.list")  # missing from one look: maybe another device's USB reset
    assert running.events("device.detached") == []
    assert running.ask("devices.list")["result"]["devices"] == []
    assert [d["unit_id"] for d in running.events("device.detached")] == [UNIT]


def test_a_device_missing_from_one_look_only_is_neither_detached_nor_attached_again(
        running, devices):
    """An Edge 1040 went missing for one look while a fēnix 8 beside it reset its USB
    connection; told detached and attached, the app would back up and sync it again."""
    running.ask("devices.list")
    root = devices.roots.pop(KEY)
    running.ask("devices.list")
    devices.roots[KEY] = root
    assert [d["unit_id"] for d in running.ask("devices.list")["result"]["devices"]] == [UNIT]
    assert running.events("device.detached") == []
    assert len(running.events("device.attached")) == 1


def test_device_info_gives_the_device_with_its_storage(running, devices):
    info = running.ask("device.info", {"device": UNIT})["result"]
    assert info["unit_id"] == UNIT and info["connected"] is True
    assert set(info["storage"]) == {"free", "capacity"} and info["storage"]["free"] > 0
    assert "FIT_TYPE_4" in info["datatypes"]
    assert devices.holds == [KEY]


def _wait_until(test, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not test():
        assert time.monotonic() < deadline, "not seen"
        time.sleep(0.01)


def test_a_device_stays_open_between_requests_and_is_let_go_a_while_after_the_last(devices):
    running = Running(devices, keep_open_s=0.3)
    try:
        running.ask("devices.list")  # attached: probed, then kept open as after a request
        for _n in range(3):
            assert "result" in running.ask("device.info", {"device": UNIT})
        assert devices.released == []
        assert devices.holds == [KEY] * 3  # each request opens it with what the poll found
        _wait_until(lambda: devices.released)
        time.sleep(0.4)
        assert devices.released == [KEY]  # once, after the last
        running.ask("device.info", {"device": UNIT})
        _wait_until(lambda: len(devices.released) == 2)
    finally:
        running.stop()


def test_without_a_while_to_stay_open_a_device_is_let_go_after_each_request(devices):
    running = Running(devices, keep_open_s=0)
    try:
        running.ask("devices.list")
        assert devices.released == [KEY]  # after the probe
        running.ask("device.info", {"device": UNIT})
        assert devices.released == [KEY, KEY]
    finally:
        running.stop()


def test_a_device_unplugged_or_taken_by_another_program_is_let_go_at_once(running, devices):
    running.ask("device.info", {"device": UNIT})
    devices.held[KEY] = "Garmin Express Service"
    running.ask("devices.list")
    assert devices.released == [KEY]
    devices.held.clear()
    running.ask("devices.list")  # free again: probed, kept open
    devices.roots.clear()
    running.ask("devices.list")
    running.ask("devices.list")  # missing from two looks: unplugged
    assert devices.released == [KEY, KEY]


def test_a_device_another_process_of_the_program_waits_for_is_let_go_at_once(running, devices):
    running.ask("device.info", {"device": UNIT})
    devices.waiting = {KEY}
    running.ask("devices.list")
    assert devices.released == [KEY]


def test_another_process_waiting_for_one_device_has_only_that_one_let_go(device_root, tmp_path):
    """An `openexpress` command for the fēnix 8 let go of the Edge 1040 beside it too, while
    the want lock said only that some device was wanted."""
    import shutil

    other_key = "mtp://[usb:001,010]/"
    shutil.copytree(device_root, tmp_path / "other")
    devices = FakeDevices(**{KEY: device_root, other_key: tmp_path / "other"})
    running = Running(devices)
    try:
        running.ask("devices.list")  # both probed, both kept open
        devices.waiting = {other_key}
        running.ask("devices.list")
        assert devices.released == [other_key]
    finally:
        running.stop()


def _stops_responding_once(running, method):
    """`method` of the engine, made to find the device not responding the first time."""
    from openexpress.device.storage import DeviceNotResponding

    kind, real = running.engine.methods[method]
    calls = []

    def once(req, a):
        calls.append(1)
        if len(calls) == 1:
            raise DeviceNotResponding("Cannot read GARMIN/Activity/x.fit: made up")
        return real(req, a)

    running.engine.methods[method] = (kind, once)
    return calls


def test_a_request_that_only_reads_is_run_once_more_when_the_device_comes_back(running):
    """A fēnix 8 on a USB hub broke off about one read in a hundred and answered again."""
    calls = _stops_responding_once(running, "device.info")
    info = running.ask("device.info", {"device": UNIT})
    assert info["result"]["unit_id"] == UNIT and calls == [1, 1]


def test_a_request_that_writes_is_not_run_again(running):
    calls = _stops_responding_once(running, "device.sync")
    error = running.ask("device.sync", {"device": UNIT})["error"]
    assert error["code"] == "device_not_responding" and calls == [1]


def test_a_device_that_does_not_come_back_is_said_not_responding(running, devices, monkeypatch):
    monkeypatch.setattr(engine, "COME_BACK_WAIT_S", 0.1)
    devices.comes_back = False
    calls = _stops_responding_once(running, "device.info")
    error = running.ask("device.info", {"device": UNIT})["error"]
    assert error["code"] == "device_not_responding" and calls == [1]


def test_the_device_is_let_go_when_the_engine_ends(devices):
    running = Running(devices)
    running.ask("device.info", {"device": UNIT})
    assert devices.released == []
    running.ask("shutdown")
    running.thread.join(10)
    assert devices.released == [KEY]


def test_a_device_wanted_during_a_request_is_let_go_once_it_ends(running, devices):
    started, go_on = threading.Event(), threading.Event()
    real = running.engine.device_info

    def slow_info(req, a):
        started.set()
        go_on.wait(10)
        return real(req, a)

    running.engine.methods["device.info"] = ("device", slow_info)
    request_id = running.send("device.info", {"device": UNIT})
    started.wait(10)
    devices.waiting = {KEY}
    running.ask("devices.list")
    assert devices.released == []  # not under the request
    go_on.set()
    running.response(request_id)
    _wait_until(lambda: devices.released == [KEY])


def test_a_request_that_finds_the_device_held_has_the_next_look_ask_who_holds_it(running,
                                                                                  devices):
    from openexpress.device.found import DeviceInUse
    from openexpress.platform.mac_usb import Holder

    def taken(found):
        raise DeviceInUse(Holder("Garmin Express Service", 4242))

    devices.hold = taken
    error = running.ask("device.info", {"device": UNIT})["error"]
    assert error["code"] == "device_busy"
    assert devices.looks_again == 1


def test_a_device_that_does_not_answer_holds_up_no_other(device_root, tmp_path):
    """Probed side by side, and the look waits PROBE_WAIT_S at most: the one that answers is
    listed at once, the other told attached once its probe is over (a fēnix 8 still starting,
    whose probe took 18 s)."""
    import shutil

    other = tmp_path / "other"
    shutil.copytree(device_root, other)
    slow_key = "mtp://[usb:001,010]/"
    devices = FakeDevices(**{slow_key: other, KEY: device_root})  # the one that waits first
    answer = threading.Event()
    probe = devices.probe

    def slow_probe(found):
        if found.uri == slow_key:
            assert answer.wait(10)
        return probe(found)

    devices.probe = slow_probe
    running = Running(devices)
    running.engine.probe_wait_s = 0.2
    try:
        listed = running.ask("devices.list")["result"]["devices"]
        assert [d["unit_id"] for d in listed if d["connected"]] == [UNIT]
        running.ask("devices.list")  # the probe still going on is not started again
        answer.set()
        _wait_until(lambda: len(running.events("device.attached")) == 2)
        assert len([d for d in running.ask("devices.list")["result"]["devices"]
                    if d["connected"]]) == 2
        assert len(running.events("device.attached")) == 2
    finally:
        answer.set()
        running.stop()


def test_a_unit_id_not_connected_is_a_no_device_error(running):
    error = running.ask("device.info", {"device": "42"})["error"]
    assert error["code"] == "no_device" and error["data"] == {"device": "42"}


def test_a_device_another_program_holds_is_told_busy_and_its_requests_refused_with_the_program(
        running, devices):
    devices.held[KEY] = "Garmin Express Service"
    listed = running.ask("devices.list")["result"]["devices"]
    assert listed[0]["busy_by"] == "Garmin Express Service"
    assert running.events("device.busy") == [
        {"device": None, "program": "Garmin Express Service", "name": "Test watch"}]
    error = running.ask("device.info")["error"]
    assert error["code"] == "device_busy"
    assert error["data"]["program"] == "Garmin Express Service"
    assert "Garmin Express Service is using the device" in error["message"]


def test_devices_list_reads_the_remembered_devices_and_the_settings_once_for_the_whole_list(
        running, monkeypatch):
    from openexpress import known_devices

    for n in range(3):
        known_devices.remember(1000 + n, f"Watch {n}", "006-B0000-00", "1.00", "mtp")
    running.ask("devices.list")  # attached first: its events read on their own
    loads = {"known": 0, "settings": 0}
    load_known, load_settings = known_devices.load, settings_mod.Settings.load.__func__

    def count_known():
        loads["known"] += 1
        return load_known()

    def count_settings(cls):
        loads["settings"] += 1
        return load_settings(cls)

    monkeypatch.setattr(known_devices, "load", count_known)
    monkeypatch.setattr(settings_mod.Settings, "load", classmethod(count_settings))
    listed = running.ask("devices.list")["result"]["devices"]
    assert [d["unit_id"] for d in listed] == [UNIT, "1002", "1001", "1000"]
    assert loads == {"known": 1, "settings": 1}


def test_a_busy_device_without_a_usb_name_is_named_as_it_was_remembered(running, devices,
                                                                          monkeypatch):
    """A fēnix 8 gives no USB product name: held by another program from the start, it is
    told by the name it had when it was last read, found by its serial."""
    from openexpress import known_devices
    from openexpress.device.found import GarminUsb

    known_devices.remember(1234567890, "fenix 8 - 47mm", "006-B4536-00", "24.16", "mtp")
    usb = GarminUsb("51b8", bus=1, address=9, serial="499602D2")
    monkeypatch.setattr(devices, "scan", lambda: [
        engine.Seen(KEY, FoundDevice(KEY, "", True, usb=usb), "Garmin Express Service")])
    running.ask("devices.list")
    assert running.events("device.busy") == [
        {"device": UNIT, "program": "Garmin Express Service", "name": "fenix 8 - 47mm"}]


def test_a_busy_device_is_named_by_the_unit_id_its_usb_serial_gives_when_it_is_remembered():
    assert engine.unit_id_by_serial("499602D2", {1234567890: None}) == 1234567890
    assert engine.unit_id_by_serial("1234567890", {1234567890: None}) == 1234567890
    assert engine.unit_id_by_serial("0000ABCDEF", {1234567890: None}) is None
    assert engine.unit_id_by_serial(None, {1234567890: None}) is None


# --- dry runs, applying, progress --------------------------------------------------------

def test_a_backup_without_apply_counts_the_files_and_writes_nothing(running, tmp_path):
    dest = tmp_path / "dest"
    result = running.ask("device.backup", {"device": UNIT, "dest": str(dest),
                                           "apply": False})["result"]
    assert result == {"dry_run": True, "dest": str(dest), "files": 5}
    assert not dest.exists()


def test_a_backup_with_apply_copies_the_files_and_tells_its_progress(running, tmp_path,
                                                                    ticking_clock):
    dest = tmp_path / "dest"
    request_id = running.send("device.backup", {"device": UNIT, "dest": str(dest),
                                                "apply": True})
    result = running.response(request_id)["result"]
    assert result["copied"] == 5 and result["linked"] == 0 and result["removed"] == []
    assert result["path"].startswith(str(dest / UNIT))
    steps = running.events("progress", request=request_id)
    assert steps and steps[-1]["done"] == steps[-1]["total"] == 5
    assert {s["phase"] for s in steps} == {"backup"}


def test_backups_list_gives_each_backup_of_the_device_newest_first_and_the_device_its_last(
        running, ticking_clock, tmp_path):
    running.ask("settings.set", {"key": "backup_dir", "value": str(tmp_path / "bk")})
    first = running.ask("device.backup", {"apply": True})["result"]
    second = running.ask("device.backup", {"apply": True})["result"]
    backups = running.ask("backups.list", {"device": UNIT})["result"]["backups"]
    assert [b["path"] for b in backups] == [second["path"], first["path"]]
    assert backups[0]["files"] == 5 and backups[0]["bytes"] == second["bytes"]
    assert backups[0]["date"].startswith("2026-10-20T09:00:01")  # with its offset
    device = running.ask("devices.list")["result"]["devices"][0]
    assert device["last_backup"] == backups[0]["date"]
    # Not connected: its unit ID alone names it.
    assert running.ask("backups.list", {"device": "42"})["result"] == {"backups": []}


def _a_map(name, file, *, installed=True, update=False):
    from openexpress import maps

    return maps.UniversalMap(
        name=name, map_type="TopoActive", state=1 if installed else 0,
        is_reinstall=not update, can_uninstall=True, version="2026.20", region="Europe",
        identifier={}, files=[maps.MapFile(f"Garmin/{file}", "https://example.org/m", None, 9)],
        files_to_remove=[], hosts=[])


def test_a_map_tells_the_version_the_device_has_from_its_header_when_garmin_offers_an_update(
        running, device_root, monkeypatch):
    from openexpress import cli

    garmin = device_root / "Garmin"  # the device's map folder, as Garmin names it
    garmin.mkdir(exist_ok=True)
    (garmin / "D1.img").write_bytes(img_header(b"TopoActive Europe, West 2026.11") + bytes(100))
    (garmin / "D2.img").write_bytes(img_header(b"CourseView Pacific") + bytes(100))
    offered = [_a_map("TopoActive Western Europe", "D1.img", update=True),
               _a_map("CourseView Pacific", "D2.img", update=True),
               _a_map("TopoActive Central Europe", "D3.img"),
               _a_map("TopoActive Alps", "D4.img", installed=False)]

    def load_maps(args, client, own=False):
        st = LocalStorage(device_root)
        return st, parse(st.read("GARMIN/GarminDevice.xml")), offered, len(offered)

    monkeypatch.setattr(cli, "_load_maps", load_maps)
    listed = running.ask("maps.list", {"all": True})["result"]["maps"]
    versions = {m["name"]: (m["status"], m["version"], m["installed_version"]) for m in listed}
    assert versions == {
        "TopoActive Western Europe": ("update", "2026.20", "2026.11"),
        "CourseView Pacific": ("update", "2026.20", None),  # its header names no version
        "TopoActive Central Europe": ("up to date", "2026.20", "2026.20"),
        "TopoActive Alps": ("available", "2026.20", None)}


def test_several_maps_installed_ask_garmin_for_the_maps_once_and_each_has_its_own_entry(
        devices, device_root, monkeypatch):
    from openexpress import cli
    from openexpress import maps as mp

    offered = [_a_map("TopoActive Alps", "D4.img", installed=False),
               _a_map("TopoActive Pyrenees", "D5.img", installed=False)]
    for m in offered:
        m.files = [mp.MapFile(m.files[0].device_path, "https://example.org/m", "0" * 32, 9)]
    loads = []

    def load_maps(args, client, own=False):
        loads.append(args.name)
        print("Read the maps of the device.")
        st = LocalStorage(device_root)
        return st, parse(st.read("GARMIN/GarminDevice.xml")), offered, len(offered)

    monkeypatch.setattr(cli, "_load_maps", load_maps)
    running = Running(devices)  # here: its lines routed from the stdout of the test
    result = running.ask("maps.install", {"maps": ["Alps", "Nowhere", "Pyrenees"]})["result"]
    running.stop()
    assert len(loads) == 1
    assert result["dry_run"] is True
    alps, nowhere, pyrenees = result["maps"]
    assert (alps["name"], alps["done"], alps["exit_code"]) == ("Alps", True, 0)
    assert alps["lines"][0] == "Read the maps of the device."
    assert any("Installing map TopoActive Alps" in line for line in alps["lines"])
    assert not any("Pyrenees" in line for line in alps["lines"])
    assert (nowhere["done"], nowhere["code"]) == (False, "failed")
    assert pyrenees["done"] is True and pyrenees["lines"][0] == "Read the maps of the device."
    assert any("Installing map TopoActive Pyrenees" in line for line in pyrenees["lines"])


def test_maps_not_read_are_the_error_of_every_map_named(running, monkeypatch):
    from openexpress import cli
    from openexpress import maps as mp

    def load_maps(args, client, own=False):
        raise mp.MapError("made up: no maps")

    monkeypatch.setattr(cli, "_load_maps", load_maps)
    result = running.ask("maps.remove", {"maps": ["Alps", "Pyrenees"]})["result"]
    assert [(m["name"], m["done"], m["reason"]) for m in result["maps"]] == [
        ("Alps", False, "made up: no maps"), ("Pyrenees", False, "made up: no maps")]


def test_apps_are_listed_as_data_from_what_the_command_line_reads(running, device_root,
                                                                  monkeypatch, xdg):
    import uuid

    from test_ciq_music import (
        IQ_XML_TYPES,
        PRELOADED,
        STORE_ID,
        CountingClient,
        FakeStore,
        out_bin,
        store_app,
    )

    from openexpress import ciq, cli, service

    face, widget = (uuid.UUID("c33a05fa-bb7b-46a4-87b4-482a3e726a11"),
                    uuid.UUID("0d1e2f3a-0000-4000-8000-000000000002"))
    xml = device_root / "GARMIN/GarminDevice.xml"
    xml.write_text(xml.read_text().replace("</MassStorageMode>",
                                           IQ_XML_TYPES + "</MassStorageMode>"))
    (device_root / "GARMIN/APPS/OUT.BIN").write_bytes(out_bin([
        (STORE_ID, 1, "Connect IQ Store", False, "DADE7E4"),
        (face, 3, "Face It", False, "FACE1"),
        (widget, 2, "Weather Glance", True, "GLANCE1"),
        (PRELOADED, 8, "Softball", True, "")]))
    newer = ciq.StoreApp(face, "Face It", "dev", "watchface", 4321, 69, "2.1", True)
    monkeypatch.setattr(cli, "make_client", lambda settings, need_auth: CountingClient())
    monkeypatch.setattr(service, "require_account_device", lambda c, unit_id, **_kw: [])
    monkeypatch.setattr(ciq, "StoreApi", FakeStore)
    monkeypatch.setattr(FakeStore, "account_apps", lambda self, device, locale: [
        store_app(face, "Face It"), store_app(uuid.UUID(int=7), "Trail Field", "datafield")])
    monkeypatch.setattr(FakeStore, "updates", lambda self, device, installed, locale: [newer])

    result = running.ask("apps.list", {"builtin": True})["result"]
    assert "lines" not in result and "exit_code" not in result
    assert result["summary"] == {"store_apps": 3, "free_slots": 31, "free_bytes": 66_000_000}
    store, shown_face, glance = result["apps"]
    assert store == {"id": str(STORE_ID), "name": "Connect IQ Store", "kind": "app",
                     "kind_name": "app", "version": 68, "enabled": True, "update": None,
                     "waiting": None, "waiting_text": None, "removable": False}
    assert (shown_face["kind"], shown_face["kind_name"], shown_face["removable"]) == (
        "watch_face", "watch face", True)
    assert shown_face["update"] == {"version": "2.1", "size": 4321, "permissions_changed": True}
    assert (glance["kind"], glance["enabled"], glance["update"]) == ("widget", False, None)
    assert result["account"] == [{
        "id": str(uuid.UUID(int=7)), "name": "Trail Field", "kind": "data_field",
        "kind_name": "data field", "version": "1.0", "size": 1000, "developer": "dev",
        "permissions_changed": False}]
    assert result["builtin"] == [{"id": str(PRELOADED), "name": "Softball", "kind": "activity",
                                  "kind_name": "activity", "enabled": False}]
    assert result["message"] is None and result["warning"] is None
    assert result["note"].startswith("New apps are chosen in the Connect IQ Store")
    # Without `builtin`, the built-in ones are left out.
    assert "builtin" not in running.ask("apps.list")["result"]


def _track_fit(count: int) -> bytes:
    from fakes import FitData, FitDefinition, fit_file

    start = 1_100_000_000
    return fit_file(
        FitDefinition(0, 0, ((0, 0x00), (4, 0x86))), FitData(0, (4, start)),
        FitDefinition(1, 20, ((253, 0x86), (0, 0x85), (1, 0x85), (3, 0x02), (2, 0x84))),
        FitData(1, (start - 1, None, None, 90, None)),  # no position: no point
        *(FitData(1, (start + n, 600_000_000 + n, 150_000_000, 120 + n % 50, 2600))
          for n in range(count)))


def test_the_track_of_a_fit_file_gives_its_positions_with_time_heart_rate_and_altitude(
        running, backups):
    path = backups / "1234567890" / "2026-11-18 (10.00.00)" / "FIT_TYPE_4" / "run.fit"
    path.parent.mkdir(parents=True)
    path.write_bytes(_track_fit(3))
    points = running.ask("activities.track", {"fit": str(path)})["result"]["points"]
    assert len(points) == 3
    assert points[0]["lat"] == pytest.approx(600_000_000 * 180 / 2**31, abs=1e-6)
    assert points[0]["lon"] == pytest.approx(150_000_000 * 180 / 2**31, abs=1e-6)
    assert points[0]["hr"] == 120 and points[0]["alt"] == 2600 / 5 - 500
    assert "speed" not in points[0]  # not in its records: left out, never 0
    assert points[1]["time"].startswith("2024-11-08T")  # ISO, with the offset


@pytest.mark.parametrize("where", ["outside", "escaping"])
def test_a_local_track_outside_the_backups_is_refused(running, backups, tmp_path, where):
    """A device's listing with a name that begins with "/" must not read a file of this
    computer: only a copy in the backup folder is read."""
    path = tmp_path / "elsewhere.fit"
    path.write_bytes(_track_fit(3))
    asked = str(path) if where == "outside" else f"{backups}/../elsewhere.fit"
    error = running.ask("activities.track", {"fit": asked})["error"]
    assert error["code"] == "invalid_params"


def test_a_long_track_is_thinned_to_2000_points_keeping_its_last(tmp_path):
    points = engine.track(_track_fit(5001))["points"]
    assert len(points) <= engine.TRACK_POINTS
    assert points[-1]["hr"] == 120 + 5000 % 50


def test_the_track_of_a_file_on_the_device_is_read_from_it(running, device_root, backups):
    (device_root / "GARMIN" / "Activity" / "run.fit").write_bytes(_track_fit(2))
    result = running.ask("activities.track", {"fit": "GARMIN/Activity/run.fit"})
    assert len(result["result"]["points"]) == 2
    gone = f"{backups}/1234567890/no such backup/run.fit"  # a copy since removed
    error = running.ask("activities.track", {"fit": gone})["error"]
    assert error["code"] == "not_found"


@pytest.fixture
def three_activities(device_root, monkeypatch):
    """The device with three activity files, each read counted (`reads`, by name)."""
    from conftest import make_fit

    from openexpress import activities

    (device_root / "GARMIN" / "Activity" / "2026-10-03-10-00-00.fit").write_bytes(
        make_fit(1100172800))
    reads = []
    real = activities.DeviceActivities.read_file

    def read_file(self, activity):
        reads.append(activity.name)
        return real(self, activity)

    monkeypatch.setattr(activities.DeviceActivities, "read_file", read_file)
    monkeypatch.setattr(engine, "READING_EVERY_S", 0)  # every file told
    return reads


def test_an_activity_file_that_cannot_be_read_still_has_a_start(running, device_root):
    """The app needs a time for every row: an unreadable file gets its time of change."""
    (device_root / "GARMIN" / "Activity" / "2026-10-04-10-00-00.fit").write_bytes(b"not a FIT")
    listed = running.ask("activities.list")["result"]["activities"]
    broken = next(a for a in listed if a["name"] == "2026-10-04-10-00-00.fit")
    assert broken["state"] in ("unreadable", "damaged")
    assert all(a["start"] for a in listed)
    # Every activity has its heart rate and calories, null where the file does not tell them.
    assert all({"average_heart_rate", "calories"} <= set(a) for a in listed)
    assert broken["average_heart_rate"] is None and broken["calories"] is None


def test_activities_list_tells_each_file_it_reads_as_progress_then_that_all_are_read(
        running, three_activities):
    request_id = running.send("activities.list", {"limit": 10})
    assert len(running.response(request_id)["result"]["activities"]) == 3
    steps = running.events("progress", request=request_id)
    assert {s["phase"] for s in steps} == {"reading_activities"}
    assert [(s["done"], s["total"]) for s in steps] == [(0, 3), (1, 3), (2, 3), (3, 3)]
    assert [s.get("item") for s in steps[:3]] == three_activities  # the newest first


def test_a_later_activities_list_reads_only_files_it_has_not_read_and_tells_no_progress(
        running, three_activities, device_root):
    from conftest import make_fit

    running.ask("activities.list", {"limit": 10})
    assert len(three_activities) == 3
    second = running.send("activities.list", {"limit": 10})
    assert len(running.response(second)["result"]["activities"]) == 3
    assert len(three_activities) == 3 and running.events("progress", request=second) == []
    (device_root / "GARMIN" / "Activity" / "2026-10-04-10-00-00.fit").write_bytes(
        make_fit(1100259200))
    running.ask("activities.list", {"limit": 10})
    assert three_activities[3:] == ["2026-10-04-10-00-00.fit"]  # the new one alone


def test_a_cancelled_activities_list_keeps_what_it_read_and_the_next_call_resumes(
        running, three_activities, monkeypatch):
    from openexpress import activities

    first_read, go = threading.Event(), threading.Event()
    counting = activities.DeviceActivities.read_file

    def read_file(self, activity):
        data = counting(self, activity)
        if len(three_activities) == 1:
            first_read.set()
            go.wait(10)
        return data

    monkeypatch.setattr(activities.DeviceActivities, "read_file", read_file)
    request_id = running.send("activities.list", {"limit": 10})
    assert first_read.wait(10)
    running.ask("cancel", {"request": request_id})
    go.set()
    assert running.response(request_id)["error"]["code"] == "cancelled"
    assert len(three_activities) == 1  # stopped before the next file
    assert len(running.ask("activities.list", {"limit": 10})["result"]["activities"]) == 3
    assert len(three_activities) == 3  # the two left, not the first again


def _kept_on_disk() -> list[str]:
    from openexpress import activities

    data = json.loads(activities.summaries_path(int(UNIT)).read_text())
    return [entry["path"].rsplit("/", 1)[-1] for entry in data["files"]]


def test_a_new_engine_reads_no_activity_file_the_one_before_read(devices, three_activities):
    first = Running(devices)
    first.ask("activities.list", {"limit": 10})
    first.stop()
    assert len(three_activities) == 3 and len(_kept_on_disk()) == 3
    second = Running(devices)
    try:
        request_id = second.send("activities.list", {"limit": 10})
        result = second.response(request_id)["result"]["activities"]
        assert [a["state"] for a in result] == ["complete"] * 3
        assert len(three_activities) == 3  # nothing read again
        assert second.events("progress", request=request_id) == []
    finally:
        second.stop()


def test_a_file_changed_since_is_read_again_alone_and_one_gone_leaves_the_kept_file(
        devices, three_activities, device_root):
    from conftest import make_fit

    first = Running(devices)
    first.ask("activities.list", {"limit": 10})
    first.stop()
    folder = device_root / "GARMIN" / "Activity"
    (folder / "2026-10-02-10-00-00.fit").write_bytes(make_fit(1100086401) + b"\0")  # its size
    (folder / "2026-10-01-10-00-00.fit").unlink()
    second = Running(devices)
    try:
        assert len(second.ask("activities.list", {"limit": 10})["result"]["activities"]) == 2
    finally:
        second.stop()
    assert three_activities[3:] == ["2026-10-02-10-00-00.fit"]
    assert sorted(_kept_on_disk()) == ["2026-10-02-10-00-00.fit", "2026-10-03-10-00-00.fit"]


@pytest.mark.parametrize("kept", [
    "{not json",
    json.dumps({"format": activities.SUMMARIES_FORMAT, "openexpress": "0.0.1", "files": []}),
    json.dumps({"format": 99, "openexpress": __version__, "files": []}),
    json.dumps({"format": activities.SUMMARIES_FORMAT, "openexpress": __version__,
                "files": [{"path": 3}]}),
], ids=["damaged", "older version", "other format", "damaged entry"])
def test_kept_summaries_that_are_damaged_or_of_another_version_are_read_again(
        running, three_activities, kept):
    from openexpress import activities

    path = activities.summaries_path(int(UNIT))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kept)
    assert len(running.ask("activities.list", {"limit": 10})["result"]["activities"]) == 3
    assert len(three_activities) == 3
    assert len(_kept_on_disk()) == 3  # written anew


def test_the_kept_summaries_cannot_be_written_and_the_list_is_answered_all_the_same(
        running, three_activities, monkeypatch):
    from openexpress import activities, paths

    real = paths.write_private

    def refused(path, data):
        if path == activities.summaries_path(int(UNIT)):
            raise PermissionError("read-only cache")
        real(path, data)

    monkeypatch.setattr(paths, "write_private", refused)
    assert len(running.ask("activities.list", {"limit": 10})["result"]["activities"]) == 3
    assert not activities.summaries_path(int(UNIT)).exists()


def test_what_a_cancelled_activities_list_read_is_kept_on_disk_too(
        running, three_activities, monkeypatch):
    from openexpress import activities

    first_read, go = threading.Event(), threading.Event()
    counting = activities.DeviceActivities.read_file

    def read_file(self, activity):
        data = counting(self, activity)
        first_read.set()
        go.wait(10)
        return data

    monkeypatch.setattr(activities.DeviceActivities, "read_file", read_file)
    request_id = running.send("activities.list", {"limit": 10})
    assert first_read.wait(10)
    running.ask("cancel", {"request": request_id})
    go.set()
    assert running.response(request_id)["error"]["code"] == "cancelled"
    assert _kept_on_disk() == three_activities[:1]


def test_forgetting_a_device_deletes_the_summaries_kept_of_its_activities(
        running, three_activities):
    from openexpress import activities, forget, known_devices

    running.ask("activities.list", {"limit": 10})
    path = activities.summaries_path(int(UNIT))
    assert path.exists()
    known_devices.remember(int(UNIT), "Test watch", "006-B0000-00", "23.39", "mtp")
    assert forget.forget_device(int(UNIT))
    assert not path.exists()


def test_progress_steps_are_put_in_phases_with_what_they_move_as_the_item():
    assert engine.phase_of(downloading_text("TopoActive Europe"), "maps.install") == (
        "downloading", "TopoActive Europe")
    assert engine.phase_of(copying_text("GPS"), "updates.install") == ("copying", "GPS")
    assert engine.phase_of("Copying to the backup", "device.backup") == (
        "backup", "Copying to the backup")


@pytest.mark.parametrize("step, unit", [
    (Step(downloading_text("TopoActive"), 1024, 4096, in_bytes=True), "bytes"),
    (Step("Copying to the backup", 3, 25, bytes_done=120, bytes_total=1200), "bytes"),
    (Step("Copying to the backup", 3, 25), None),  # files counted: no speed
])
def test_a_progress_in_bytes_says_so_for_the_speed_the_app_shows(step, unit):
    import io

    out = io.StringIO()
    eng = engine.Engine(out)
    eng.progress_for(engine.Request(1, "device.backup", {}))(step)
    (event,) = [json.loads(line) for line in out.getvalue().splitlines()]
    assert event["event"] == "progress" and event["params"].get("unit") == unit


def test_a_request_writing_to_the_device_stops_between_files_never_inside_one():
    writes = engine.Request(1, "maps.install", {}, writes=True)
    reads = engine.Request(2, "device.backup", {})
    in_a_file = Step(copying_text("TopoActive"), 1024, 4096, in_bytes=True)
    assert not engine.Engine._safe_point(writes, in_a_file)
    assert engine.Engine._safe_point(writes, Step(copying_text("TopoActive"), 1, 3))
    assert engine.Engine._safe_point(writes, Step(downloading_text("TopoActive"), 1024, 4096,
                                                  in_bytes=True))
    assert engine.Engine._safe_point(reads, in_a_file)


# --- cancel, and one request at a time per device ---------------------------------------------

@pytest.fixture
def held_backup(monkeypatch):
    """A backup that tells its first file and then waits for `go` before the next."""
    go, first = threading.Event(), threading.Event()

    def copy_files(device, storage, target, previous, progress):
        progress(Step("Copying to the backup", 1, 3))
        first.set()
        go.wait(10)
        progress(Step("Copying to the backup", 2, 3))
        return backup.BackupResult(target, 0, 0, [])

    monkeypatch.setattr(backup, "_copy_files", copy_files)
    return first, go


def test_cancel_stops_a_running_backup_at_its_next_file_and_leaves_no_partial_backup(
        running, tmp_path, held_backup):
    first, go = held_backup
    dest = tmp_path / "dest"
    request_id = running.send("device.backup", {"device": UNIT, "dest": str(dest),
                                                "apply": True})
    assert first.wait(10)
    assert running.ask("cancel", {"request": request_id})["result"] == {"cancelling": True}
    go.set()
    error = running.response(request_id)["error"]
    assert error["code"] == "cancelled"
    assert list((dest / UNIT).iterdir()) == []


def test_a_request_for_a_device_waits_for_the_one_before_and_a_waiting_one_can_be_cancelled(
        running, tmp_path, held_backup):
    first, go = held_backup
    backing_up = running.send("device.backup", {"device": UNIT, "dest": str(tmp_path / "d"),
                                                "apply": True})
    assert first.wait(10)
    info = running.send("device.info", {"device": UNIT})
    waiting = running.send("device.info", {"device": UNIT})
    time.sleep(0.3)
    assert not [m for m in running.out.messages() if m.get("id") in (info, waiting)]
    # The reader stays free meanwhile: it answers what needs no device.
    assert "result" in running.ask("hello")
    running.ask("cancel", {"request": waiting})
    assert running.response(waiting)["error"]["code"] == "cancelled"
    go.set()
    assert "result" in running.response(backing_up)
    assert running.response(info)["result"]["unit_id"] == UNIT


def test_cancel_names_a_request_and_says_whether_it_was_running(running):
    assert running.ask("cancel", {"request": 12345})["result"] == {"cancelling": False}
    assert running.ask("cancel", {})["error"]["code"] == "invalid_params"


# --- the account -------------------------------------------------------------------------

class Server:
    """Garmin's servers, of the test: answers by path."""

    def __init__(self, routes):
        self.routes, self.paths = routes, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                server.paths.append(path)
                status, body = server.routes.get(path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Type", "application/json"
                                 if body.startswith(b"{") else "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def garmin(monkeypatch):
    server = Server({
        "/oauth-service/oauth/preauthorized": (
            200, b"oauth_token=made-up-token&oauth_token_secret=made-up-secret"),
        "/userprofile-service/userprofile/userProfileBase": (
            200, json.dumps({"userName": "tomek_b", "emailAddress": "tomek@example.com"})
            .encode()),
    })
    monkeypatch.setitem(settings_mod.ENVIRONMENTS, "test", {
        "sso": server.url, "connectapi": server.url, "omt": server.url, "gcs": server.url})
    settings_mod.Settings(environment="test").save()
    settings_mod.save_consumer(settings_mod.Consumer("ck", "cs"))
    yield server
    server.close()


def test_sign_in_takes_the_address_with_the_ticket_saves_the_token_and_names_the_account_masked(
        running, garmin):
    from openexpress.auth import store

    assert running.ask("account.status")["result"]["signed_in"] is False
    result = running.ask("account.sign_in", {
        "ticket_url": "https://sso.garmin.com/sso/embed?ticket=ST-0123-made-up-cas"})["result"]
    assert result == {"account": "t…b"}
    assert "/oauth-service/oauth/preauthorized" in garmin.paths
    assert store.load().token == "made-up-token"
    status = running.ask("account.status")["result"]
    assert status["signed_in"] is True and status["account"] == "t…b"
    assert status["setup_done"] is True
    assert "tomek" not in "".join(running.out.written)


def test_sign_in_with_an_address_without_a_ticket_asks_nothing_of_the_server(running, garmin):
    error = running.ask("account.sign_in", {"ticket_url": "https://sso.garmin.com/"})["error"]
    assert error["code"] == "invalid_ticket" and "ticket=ST-" in error["message"]
    assert garmin.paths == []
    assert running.ask("account.sign_in", {})["error"]["code"] == "invalid_params"


def test_the_sign_in_address_is_garmin_s_page_for_the_settings_environment(running, garmin):
    assert running.ask("account.sign_in_url")["result"]["url"].startswith(
        garmin.url + "mobile/sso/")


def test_settings_are_read_and_written_by_the_names_of_setup_s_options(running, tmp_path):
    folder = tmp_path / "drop"
    assert running.ask("settings.set", {"key": "copy_fit_to", "value": str(folder)})[
        "result"] == {"key": "copy_fit_to", "value": str(folder)}
    assert running.ask("settings.set", {"key": "backup_keep", "value": 5})["result"]["value"] == 5
    every = running.ask("settings.get")["result"]["settings"]
    assert every["copy_fit_to"] == str(folder) and every["backup_keep"] == 5
    assert running.ask("settings.set", {"key": "backup_keep", "value": "five"})[
        "error"]["code"] == "invalid_params"
    assert running.ask("settings.get", {"key": "colour"})["error"]["code"] == "unknown_setting"


def test_the_strava_upload_after_a_sync_is_off_until_turned_on_as_setup_s_option_does(running):
    from openexpress.settings import Settings

    assert running.ask("settings.get", {"key": "strava_upload"})["result"]["value"] is False
    assert running.ask("settings.set", {"key": "strava_upload", "value": True})[
        "result"] == {"key": "strava_upload", "value": True}
    assert Settings.load().strava_upload is True  # the same setting `setup --strava-upload` sets
    assert running.ask("settings.set", {"key": "strava_upload", "value": "on"})[
        "error"]["code"] == "invalid_params"


def test_strava_connect_gives_the_consent_page_first_then_trades_the_code_for_the_token(
        running, monkeypatch):
    import dataclasses
    import types

    @dataclasses.dataclass
    class Status:
        configured: bool
        connected: bool

    saved, traded = [], []
    strava = types.SimpleNamespace(
        Client=lambda client_id, secret: (client_id, secret), save_client=saved.append,
        status=lambda: Status(bool(saved), bool(traded)),
        authorize_url=lambda redirect: "https://www.strava.com/oauth/authorize?r=" + redirect,
        connect_with_code=lambda code, redirect: traded.append(code) or Status(True, True))
    monkeypatch.setitem(sys.modules, "openexpress.strava", strava)
    monkeypatch.setattr(openexpress, "strava", strava, raising=False)  # what an import finds
    first = running.ask("strava.connect", {"client_id": "123", "client_secret": "made-up",
                                           "redirect_uri": "http://localhost:5555/"})["result"]
    assert first == {"configured": True, "connected": False, "authorize_url":
                     "https://www.strava.com/oauth/authorize?r=http://localhost:5555/"}
    assert saved == [("123", "made-up")]
    second = running.ask("strava.connect", {"code": "made-up-code",
                                            "redirect_uri": "http://localhost:5555/"})
    assert second["result"] == {"configured": True, "connected": True}
    assert traded == ["made-up-code"]


# --- stdout carries the protocol alone -------------------------------------------------------

def test_what_the_code_prints_during_a_request_becomes_its_log_events_never_stdout(
        capsys, monkeypatch, devices, tmp_path):
    # Started here, in the test itself: the engine routes the sys.stdout it finds as it
    # starts, and pytest puts its own in place again as the test begins.
    running = Running(devices)

    def run_backup(*args, **kwargs):
        print("a line the command line would print")
        print("a warning on stderr", file=sys.stderr)
        raise backup.DeviceError("the device said no")

    monkeypatch.setattr(backup, "run_backup", run_backup)
    request_id = running.send("device.backup", {"device": UNIT, "dest": str(tmp_path / "d"),
                                                "apply": True})
    error = running.response(request_id)["error"]
    assert error == {"code": "device_error", "message": "the device said no"}
    assert running.events("log", request=request_id) == [
        {"level": "info", "message": "a line the command line would print",
         "request": request_id},
        {"level": "warning", "message": "a warning on stderr", "request": request_id}]
    running.out.messages()  # every line written is JSON
    running.stop()
    assert capsys.readouterr().out == ""


# --- the readings of the account ---------------------------------------------------------

class ReadingsClient:
    """The client of Garmin Connect, of the readings' tests: what the account answers, made
    up; `failing` names the requests that fail."""

    def __init__(self, signed_in=True, devices=None, sensors=(), solar=None, failing=()):
        self.auth = object() if signed_in else None
        self.devices, self.sensors, self.solar = devices, list(sensors), solar or {}
        self.failing, self.asked = set(failing), []

    def _ask(self, what):
        self.asked.append(what)
        if what in self.failing:
            from openexpress.connect import ApiError
            raise ApiError(f"{what}: HTTP 503")

    def account_devices(self, fresh=False):
        self._ask("devices")
        return self.devices if self.devices is not None else []

    def account_sensors(self):
        self._ask("sensors")
        return self.sensors

    def solar_gain(self, device_id, start, end):
        self._ask("solar")
        return [{"localconnectdate": (start + datetime.timedelta(days=n)).isoformat(),
                 "totalactivitytimegainedms": self.solar.get(n, 0)} for n in range(7)]


def _readings_client(monkeypatch, client):
    from openexpress import service
    monkeypatch.setattr(service, "make_client", lambda settings, need_auth=None: client)
    return client


def _on_account(solar=True, unit=UNIT):
    return [{"unitid": int(unit), "deviceid": 91234567, "productdisplayname": "Made-up 8 Solar",
             "solarpanelutilizationcapable": solar}]


SENSORS = [
    {"devicename": "Made-up Strap", "batterylevel": 80, "batterystatus": "OK",
     "lastconnected": "2026-10-21T09:00:00.0"},
    {"devicename": "Made-up Light", "batterystatus": "LOW",
     "lastconnected": "2026-10-19T09:00:00.0"},
]


def test_account_readings_give_the_sensors_low_first_and_the_sun_of_a_solar_device(
        running, monkeypatch, clock):
    # Today is the last of the seven days: 35 min today, 4 h 10 min in all.
    _readings_client(monkeypatch, ReadingsClient(
        devices=_on_account(), sensors=SENSORS, solar={6: 35 * 60_000, 2: 215 * 60_000}))
    result = running.ask("account.readings", {"device": UNIT})["result"]
    assert (result["signed_in"], result["on_account"]) == (True, True)
    sensors = result["sensors"]
    assert (sensors["state"], sensors["low"]) == ("read", 1)
    assert sensors["summary"] == caption("2 sensors", "1 with a low battery")
    assert [(s["name"], s["low"]) for s in sensors["sensors"]] == [
        ("Made-up Light", True), ("Made-up Strap", False)]
    assert sensors["sensors"][0]["detail"].startswith(caption("Battery low", "last connected "))
    assert sensors["sensors"][1]["detail"].startswith(caption("Battery 80%", "last connected "))
    solar = result["solar"]
    assert (solar["state"], solar["today_ms"], solar["week_ms"]) == (
        "read", 35 * 60_000, 250 * 60_000)
    assert solar["summary"] == caption("Activity time gained: today 35\u00a0min",
                                       "last 7\u00a0days 4\u00a0h\u00a010\u00a0min")


def test_a_week_of_little_sun_is_said_in_a_sentence_and_a_device_without_sun_has_no_row(
        running, monkeypatch, clock):
    client = _readings_client(monkeypatch, ReadingsClient(devices=_on_account(),
                                                          solar={6: 20_000}))
    solar = running.ask("account.readings", {"device": UNIT})["result"]["solar"]
    assert solar["summary"] == "Activity time gained: under a minute in the last 7\u00a0days"
    client.devices = _on_account(solar=False)
    result = running.ask("account.readings", {"device": UNIT, "fresh": True})["result"]
    assert result["solar"] is None and result["sensors"] is None  # no sensor: no row either


def test_a_device_off_the_signed_in_account_has_no_readings(running, monkeypatch):
    client = _readings_client(monkeypatch, ReadingsClient(
        devices=_on_account(unit="99"), sensors=SENSORS))
    assert running.ask("account.readings", {"device": UNIT})["result"] == {
        "signed_in": True, "on_account": False, "sensors": None, "solar": None}
    assert client.asked == ["devices"]


def test_signed_out_a_solar_model_by_its_name_asks_to_sign_in_and_nothing_is_asked(
        running, monkeypatch):
    from openexpress import known_devices

    known_devices.remember(4000000001, "Made-up Instinct Solar", "006-B0000-00", "1.00", "mtp")
    client = _readings_client(monkeypatch, ReadingsClient(signed_in=False))
    assert running.ask("account.readings", {"device": "4000000001"})["result"] == {
        "signed_in": False, "on_account": None, "sensors": {"state": "signed_out"},
        "solar": {"state": "signed_out"}}
    plain = running.ask("account.readings", {"device": UNIT})["result"]
    assert plain["solar"] is None and client.asked == []


def test_a_reading_that_fails_says_so_and_the_other_is_still_given(running, monkeypatch,
                                                                    clock):
    _readings_client(monkeypatch, ReadingsClient(
        devices=_on_account(), sensors=SENSORS, failing={"sensors"}))
    result = running.ask("account.readings", {"device": UNIT})["result"]
    assert result["sensors"] == {"state": "failed", "message": "sensors: HTTP 503"}
    assert result["solar"]["state"] == "read"


def test_the_account_s_list_not_known_the_device_is_taken_as_on_it(running, monkeypatch):
    from openexpress import known_devices

    known_devices.remember(4000000001, "Made-up Instinct Solar", "006-B0000-00", "1.00", "mtp")
    _readings_client(monkeypatch, ReadingsClient(sensors=SENSORS, failing={"devices"}))
    result = running.ask("account.readings", {"device": "4000000001"})["result"]
    assert result["on_account"] is None and result["sensors"]["state"] == "read"
    # Without its entry the device's ID on the account is not known.
    assert result["solar"] == {"state": "failed", "message": None}


def test_fresh_readings_forget_what_was_kept_of_them(running, monkeypatch):
    from openexpress import answers

    forgotten = []
    monkeypatch.setattr(answers.ANSWERS, "forget", lambda kinds=None, about=None:
                        forgotten.append(tuple(kinds)))
    _readings_client(monkeypatch, ReadingsClient(devices=_on_account(solar=False)))
    running.ask("account.readings", {"device": UNIT})
    assert forgotten == []
    running.ask("account.readings", {"device": UNIT, "fresh": True})
    assert forgotten == [(answers.SENSORS, answers.SOLAR)]
    assert running.ask("account.readings", {"device": "x"})["error"]["code"] == "invalid_params"


def test_the_engine_as_a_program_writes_only_json_lines_on_its_stdout(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "c"),
               XDG_DATA_HOME=str(tmp_path / "d"), XDG_CACHE_HOME=str(tmp_path / "k"),
               XDG_STATE_HOME=str(tmp_path / "s"), OPENEXPRESS_LANG="en")
    requests = "\n".join(json.dumps(r) for r in (
        {"id": 1, "method": "hello", "params": {}}, "not json",
        {"id": 2, "method": "shutdown"})) + "\n"
    done = subprocess.run([sys.executable, "-m", "openexpress", "engine"], input=requests,
                          capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert [m.get("id") for m in lines] == [1, None, 2]
    assert lines[0]["result"]["engine"] == __version__

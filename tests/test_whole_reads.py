"""Large files of a device over MTP read whole (whole_reads, GioStorage.copy_to_local): from
COPY_FROM one copy of gvfs into a temporary file of the cache, read there and removed at once;
smaller files, drives and folders as a stream. From source the copy is
Gio.File.copy, in the Flatpak the host's gio copy (made up here by a small script of this
computer). The device and gvfs are made up (gio_fake); nothing reaches a device or the
network."""

import hashlib
import logging
import os
import re
import sys
import time
import types
from collections import namedtuple

import pytest
from gio_fake import GioOp, MtpFiles

from openexpress import updates
from openexpress.device import whole_reads
from openexpress.device.storage import DeviceError, DeviceNotResponding, LocalStorage

gio_storage = pytest.importorskip("openexpress.device.gio_storage")

MIB = 1024 * 1024
BIG = 5 * MIB + 123  # above COPY_FROM, not a whole number of pieces
PATH = "GARMIN/ASR/Updates/xx_XX.srd"
LOGGER = "openexpress.device.gio_storage"


def big_bytes(size: int = BIG) -> bytes:
    return bytes(range(256)) * (size // 256) + bytes(size % 256)


@pytest.fixture
def mtp(monkeypatch):
    """make(size=BIG, plan=None): a made-up watch over MTP (gvfs answering a stream with at
    most 256 KiB, as it does) holding one file at PATH, and a GioStorage of it; short times."""
    monkeypatch.setattr(gio_storage, "CANCEL_WAIT_S", 0.1)
    monkeypatch.setattr(gio_storage, "_copy_unavailable", None)
    monkeypatch.setattr(updates, "_md5_read", {})
    made = []

    def make(size=BIG, plan=None):
        data = big_bytes(size)
        files = MtpFiles({"GARMIN": None, PATH: data}, plan=plan, most=256 * 1024)
        st = gio_storage.GioStorage("mtp://Made_Up_Device/Internal")
        st.root = files.file()
        made.append(files)
        return files, st, data

    yield make
    for files in made:
        files.clean()


def left() -> list[str]:
    """The temporary files of whole reads still there."""
    try:
        return sorted(p.name for p in whole_reads.folder().iterdir())
    except FileNotFoundError:
        return []


def stream_of(st, real=PATH):
    return lambda: st.read_at(real)


# --- from source: Gio.File.copy -----------------------------------------------------------

def test_a_large_update_file_is_checked_by_one_copy_not_read_as_a_stream(mtp):
    """The checksum of a software update's file on the device: one copy of gvfs instead of a
    stream of 256 KiB answers."""
    files, st, data = mtp()
    u = updates.SoftwareUpdate(
        part_number="006-D0000-00", name="Speech", version="1.22", severity="Recommended",
        is_primary=False, is_reinstall=False, restart_required=False, device_path=PATH,
        url="", md5=hashlib.md5(data).hexdigest(), size=len(data), order=0, on_device=True)
    found = updates.presence(st, u)
    assert found.why == updates.MATCHES
    assert files.calls.count("copy") == 1 and "chunk" not in files.calls
    assert left() == []


def test_a_copy_gives_the_bytes_of_the_file_and_tells_its_progress(mtp):
    _files, st, data = mtp()
    told = []
    got = whole_reads.whole_file(st, PATH, len(data), stream_of(st),
                                 lambda done, total: told.append((done, total)))
    assert got == data
    assert told[-1] == (len(data), len(data)) and len(told) == 21  # pieces of 256 KiB
    assert left() == []


def test_a_small_file_a_drive_and_a_folder_are_read_as_a_stream(mtp, tmp_path):
    files, st, data = mtp(size=whole_reads.COPY_FROM - 1)
    assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    assert "copy" not in files.calls and "chunk" in files.calls
    (tmp_path / "GARMIN").mkdir()
    (tmp_path / "GARMIN" / "BIG.BIN").write_bytes(big_bytes())
    folder = LocalStorage(tmp_path)
    assert not hasattr(folder, "copy_to_local")
    assert whole_reads.whole_file(folder, "GARMIN/BIG.BIN", BIG,
                                  lambda: folder.read("GARMIN/BIG.BIN")) == big_bytes()


def test_without_a_stream_of_its_own_the_file_is_read_by_its_path(mtp, tmp_path):
    """The callers that read a file by the path they listed give no stream: storage.read."""
    files, st, data = mtp(size=whole_reads.COPY_FROM - 1)
    assert whole_reads.whole_file(st, PATH, len(data)) == data
    assert "copy" not in files.calls and "chunk" in files.calls
    files, st, data = mtp(plan={("copy", 1): GioOp(says="error", message="not supported")})
    assert whole_reads.whole_file(st, PATH, len(data)) == data
    assert files.calls.count("copy") == 1 and files.calls.count("open") == 1
    (tmp_path / "GARMIN").mkdir()
    (tmp_path / "GARMIN" / "BIG.BIN").write_bytes(big_bytes())
    assert whole_reads.whole_file(LocalStorage(tmp_path), "GARMIN/BIG.BIN", BIG) == big_bytes()


def test_a_copy_that_fails_after_it_began_is_not_read_again_and_leaves_nothing(mtp):
    files, st, data = mtp(plan={("copy", 1): GioOp(says="error", message="made-up failure")})
    files.copy_fails_after = 2 * MIB
    with pytest.raises(DeviceError, match="Cannot read GARMIN/ASR/Updates/xx_XX.srd"):
        whole_reads.whole_file(st, PATH, len(data), stream_of(st))
    assert files.calls.count("copy") == 1 and "open" not in files.calls
    assert left() == []


def test_a_copy_refused_before_its_first_byte_is_read_once_as_a_stream(mtp, caplog):
    caplog.set_level(logging.INFO, logger="openexpress.device.whole_reads")
    files, st, data = mtp(plan={("copy", 1): GioOp(says="error", message="not supported")})
    assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    assert files.calls.count("copy") == 1 and files.calls.count("open") == 1
    assert any("the copy was refused before it began" in r.getMessage() for r in caplog.records)
    assert left() == []


def test_a_pygobject_that_cannot_copy_reads_as_a_stream_and_says_why_once(mtp, monkeypatch,
                                                                         caplog):
    """An older PyGObject cannot give Gio.File.copy_async its two callbacks: every large file
    of the process is read as a stream, the reason in the log once."""
    import gio_fake

    caplog.set_level(logging.INFO, logger="openexpress.device")

    def an_older_binding(self, *args):
        raise TypeError("Gio.File.copy_async() takes exactly 9 arguments (7 given)")

    monkeypatch.setattr(gio_fake._MtpFile, "copy_async", an_older_binding)
    files, st, data = mtp()
    for _ in range(3):
        assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    said = [r.getMessage() for r in caplog.records if "copy" in r.getMessage()]
    once = ("device: this PyGObject cannot copy a file with its progress "
            "(Gio.File.copy_async() takes exactly 9 arguments (7 given)); large files are read "
            "as a stream")
    assert said == [once]
    assert files.calls.count("open") == 3 and left() == []


class Stopped(Exception):
    """A progress that stops the work (as the window's, once it is closed)."""


def test_a_progress_that_raises_stops_the_copy_and_its_file_is_removed(mtp):
    files, st, data = mtp()
    files.copy_piece_delay_s = 0.02

    def told(done, total):
        raise Stopped

    with pytest.raises(Stopped):
        whole_reads.whole_file(st, PATH, len(data), stream_of(st), told)
    assert "open" not in files.calls and left() == []


def test_no_progress_for_the_stall_time_stops_the_copy_and_the_storage_keeps_it(
        mtp, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(gio_storage, "STALL_S", 0.3)
    files, st, data = mtp(plan={("copy", 1): GioOp(when="never")})
    with pytest.raises(DeviceNotResponding):
        whole_reads.whole_file(st, PATH, len(data), stream_of(st))
    assert st.stalled and "open" not in files.calls and left() == []
    assert any("no answer to a copy in 0.3 s" in r.getMessage() for r in caplog.records)


def test_a_slow_copy_whose_bytes_keep_coming_is_not_a_stall(mtp, monkeypatch):
    """Each piece renews the time limit: the whole copy takes longer than STALL_S."""
    monkeypatch.setattr(gio_storage, "STALL_S", 0.3)
    files, st, data = mtp()
    files.most = None  # pieces of 1 MiB: each shorter than STALL_S, all of them longer
    files.copy_piece_delay_s = 0.12
    started = time.monotonic()
    assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    assert time.monotonic() - started > gio_storage.STALL_S and not st.stalled


def test_the_log_says_the_pace_of_a_copy_without_a_name(mtp, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    _files, st, data = mtp()
    whole_reads.whole_file(st, PATH, len(data), stream_of(st))
    said = [r.getMessage() for r in caplog.records if "copied whole" in r.getMessage()]
    assert len(said) == 1 and "xx_XX" not in said[0]
    assert re.fullmatch(r"device: a file copied whole by Gio\.File\.copy: 5_243_003 B in "
                        r"\d+\.\d s, \d+\.\d\d MB/s", said[0])


def test_too_little_room_in_the_cache_reads_as_a_stream(mtp, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="openexpress.device.whole_reads")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(whole_reads.shutil, "disk_usage",
                        lambda path: usage(10**12, 10**12 - 1000, 1000))
    files, st, data = mtp()
    assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    assert "copy" not in files.calls
    assert any("free in the cache" in r.getMessage() and "read as a stream" in r.getMessage()
               for r in caplog.records)


def test_the_folder_of_the_copies_is_this_users_only(mtp):
    _files, st, data = mtp()
    whole_reads.whole_file(st, PATH, len(data), stream_of(st))
    assert whole_reads.folder().stat().st_mode & 0o777 == 0o700


# --- the leftovers of a run that ended during a copy ------------------------------------------

def test_leftovers_are_removed_at_the_start_and_copies_under_way_are_not(caplog):
    caplog.set_level(logging.INFO, logger="openexpress.device.whole_reads")
    where = whole_reads._made_folder()
    gone = where / "4194999-1.part"  # a process number above any Linux gives
    mine = where / f"{os.getpid()}-7.part"
    old = where / f"{os.getpid()}-8.part"
    for p in (gone, mine, old):
        p.write_bytes(b"part of a copy")
    an_hour_ago = time.time() - whole_reads.LEFT_AFTER_S - 60
    os.utime(old, (an_hour_ago, an_hour_ago))
    from openexpress.cli import main

    assert main(["cache"]) == 0
    assert left() == [mine.name]
    assert any("2 temporary file(s) left by an earlier run removed" in r.getMessage()
               for r in caplog.records)


def test_in_the_flatpak_only_the_age_of_a_leftover_counts(monkeypatch):
    """Another instance of the Flatpak numbers its processes on its own: its copy under way
    is never taken for a leftover by its number."""
    monkeypatch.setattr(whole_reads.paths, "in_flatpak", lambda: True)
    where = whole_reads._made_folder()
    other = where / "4194999-1.part"  # a number this instance does not have
    old = where / "4194999-2.part"
    for p in (other, old):
        p.write_bytes(b"part of a copy")
    an_hour_ago = time.time() - whole_reads.LEFT_AFTER_S - 60
    os.utime(old, (an_hour_ago, an_hour_ago))
    whole_reads.remove_leftovers()
    assert left() == [other.name]


def test_the_copies_count_as_temporary_files_of_the_cache():
    from openexpress import cache

    assert whole_reads.FOLDER in cache.CATEGORIES["temp"]


# --- in the Flatpak: the host's gio copy --------------------------------------------------------

HOST_GIO = """
import os, sys, time
mode, source, dest, pid_file = sys.argv[1:5]
open(pid_file, "w").write(str(os.getpid()))
data = open(source, "rb").read()
if mode == "refused":
    sys.stderr.write("gio: made-up refusal\\n")
    sys.exit(1)
total, piece, done = len(data), 1024 * 1024, 0
with open(dest, "wb") as f:
    while done < total:
        if mode in ("stall", "late") and done >= total // 2:
            if mode == "late":
                sys.stderr.write("gio: made-up failure\\n")
                sys.exit(2)
            time.sleep(60)
        f.write(data[done:done + piece])
        f.flush()
        done = min(total, done + piece)
        sys.stdout.write(f"\\rTransferred {done / 1e6:.1f} MB out of {total / 1e6:.1f} MB "
                         "(1.0 MB/s)")
        sys.stdout.flush()
        time.sleep(0.02)
sys.stdout.write("\\n")
"""


@pytest.fixture
def flatpak(mtp, monkeypatch, tmp_path):
    """make(mode): the made-up watch as the Flatpak copies from it: the host's gio copy is a
    script of this computer that copies the watch's file ("ok", "refused" before its first
    byte, "late": fails half-way, "stall": stops half-way); leaves `commands`."""
    monkeypatch.setattr(gio_storage, "in_flatpak", lambda: True)
    monkeypatch.setattr(gio_storage, "_host_visible", lambda path: True)
    monkeypatch.setattr(gio_storage.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(gio_storage, "STALL_S", 0.5)
    monkeypatch.setattr(gio_storage, "HOST_PULL_LOOK_S", 0.05)
    monkeypatch.setattr(gio_storage, "END_WAIT_S", 1.0)
    script = tmp_path / "host_gio.py"
    script.write_text(HOST_GIO, encoding="utf-8")
    commands = []

    def make(mode="ok"):
        files, st, data = mtp()
        source = tmp_path / "on-the-watch.bin"
        source.write_bytes(data)

        def host_command(*args, watch_bus=False):
            commands.append((args[:3], watch_bus))
            return [sys.executable, str(script), mode, str(source), args[-1],
                    str(tmp_path / "pid")]

        monkeypatch.setattr(gio_storage, "host_command", host_command)
        return types.SimpleNamespace(files=files, st=st, data=data, commands=commands,
                                     pid=lambda: int((tmp_path / "pid").read_text()))

    return make


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_in_the_flatpak_the_hosts_gio_copy_copies_it(flatpak, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    w = flatpak()
    told = []
    got = whole_reads.whole_file(w.st, PATH, len(w.data), stream_of(w.st),
                                 lambda done, total: told.append(done))
    assert got == w.data and "copy" not in w.files.calls and "chunk" not in w.files.calls
    assert w.commands == [(("gio", "copy", "-p"), True)]  # ends with flatpak-spawn
    assert told and told[-1] == len(w.data) and left() == []
    assert any("copied whole by the host's gio copy" in r.getMessage() for r in caplog.records)


def test_in_the_flatpak_a_cache_the_host_does_not_see_is_read_as_a_stream_saying_why(
        mtp, monkeypatch, tmp_path, caplog):
    """The host's gio copy writes where the host sees the same path as the sandbox (the
    Flatpak's cache in the home folder). A cache outside it is refused before the first byte
    (the real check), read as a stream, and the line of the log still says why once the log's
    filter has hidden the path."""
    from openexpress import applog

    caplog.set_level(logging.INFO, logger="openexpress.device.whole_reads")
    outside = tmp_path / "outside-the-home-folder"
    monkeypatch.setattr(whole_reads.paths, "cache_dir", lambda: outside)
    assert not gio_storage._host_visible(outside / whole_reads.FOLDER)
    monkeypatch.setattr(gio_storage, "in_flatpak", lambda: True)
    monkeypatch.setattr(gio_storage.shutil, "which", lambda name: f"/usr/bin/{name}")

    def never(*args, **kwargs):
        raise AssertionError("a command was run on the host")

    monkeypatch.setattr(gio_storage, "host_command", never)
    files, st, data = mtp()
    assert whole_reads.whole_file(st, PATH, len(data), stream_of(st)) == data
    assert files.calls.count("open") == 1 and not list((outside / whole_reads.FOLDER).iterdir())
    (said,) = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    filtered = applog.redact(said)
    assert filtered.startswith("whole reads: read as a stream, the copy was refused before it "
                               "began: Cannot read GARMIN/ASR/Updates/xx_XX.srd: the host does "
                               "not see ")
    assert str(outside) not in filtered


def test_in_the_flatpak_a_refusal_before_the_first_byte_is_read_once_as_a_stream(flatpak):
    w = flatpak("refused")
    assert whole_reads.whole_file(w.st, PATH, len(w.data), stream_of(w.st)) == w.data
    assert w.files.calls.count("open") == 1 and left() == []


def test_in_the_flatpak_a_copy_failing_half_way_is_not_read_again(flatpak):
    w = flatpak("late")
    with pytest.raises(DeviceError, match="made-up failure"):
        whole_reads.whole_file(w.st, PATH, len(w.data), stream_of(w.st))
    assert "open" not in w.files.calls and left() == []


def test_in_the_flatpak_a_stall_ends_the_copy_on_the_host(flatpak):
    w = flatpak("stall")
    with pytest.raises(DeviceNotResponding):
        whole_reads.whole_file(w.st, PATH, len(w.data), stream_of(w.st))
    assert w.st.stalled and gone(w.pid()) and left() == []


def test_in_the_flatpak_a_progress_that_raises_ends_the_copy_on_the_host(flatpak):
    w = flatpak()

    def told(done, total):
        raise Stopped

    with pytest.raises(Stopped):
        whole_reads.whole_file(w.st, PATH, len(w.data), stream_of(w.st), told)
    assert gone(w.pid()) and left() == []


# --- the window: the list of updates waits for the checksums ---------------------------------

def test_the_list_of_updates_is_shown_only_after_the_files_are_checked(monkeypatch, mtp):
    """While a file on the device is copied for its checksum the page shows its check under
    way ("Checking for Updates…"), so no item of it can be installed yet."""
    from fakes import RunNow, window, window_methods

    files, st, data = mtp()
    shown, during = [], []

    class Section:
        loaded = False

        def show(self, what):
            shown.append(what)

        def clear(self):
            shown.append("cleared")

        def show_error(self, text):
            shown.append(f"error: {text}")

    real_whole_file = whole_reads.whole_file

    def checking(*args, **kwargs):
        during.append(list(shown))
        return real_whole_file(*args, **kwargs)

    monkeypatch.setattr(whole_reads, "whole_file", checking)
    md5 = hashlib.md5(data).hexdigest()
    monkeypatch.setattr(updates, "offered", lambda storage, answer, device, progress=None: [
        updates.presence(storage, updates.SoftwareUpdate(
            part_number="006-D0000-00", name="Speech", version="1.22", severity="Recommended",
            is_primary=False, is_reinstall=False, restart_required=False, device_path=PATH,
            url="", md5=md5, size=len(data), order=0, on_device=True))])
    session = types.SimpleNamespace(
        storage=st, device=types.SimpleNamespace(raw=b"", unit_id=1, part_number="006-B0000-00"),
        unit_info_answer={"segmenttype": "Wellness"},
        client=types.SimpleNamespace(software_updates=lambda raw, include_reinstalls=False: {},
                                     unit_info=lambda unit_id, part_number: {}))
    win = window(session=session, sections={"updates": Section()}, run_task=RunNow(),
                 progress=lambda *a: None)
    window_methods(win, ["_load", "load_updates"])
    win.show_updates = lambda items, beta=False: shown.append(("list", items[0].why))
    win.load_updates()
    assert during == [["loading"]]
    assert shown == ["loading", "cleared", ("list", updates.MATCHES), "content"]
    assert files.calls.count("copy") == 1

"""A session of our own and the other uses of the device in the same process (gio_storage._InUse,
_unmounted). Before it unmounts the device, the reads, listings, copies and deletions of the
other threads end (at most OWN_USE_WAIT_S; then the session is refused as busy and nothing is
cut short); an unmount gvfs refuses as busy (another program reads the device) is asked again
a few times. While a session waits or runs, a new operation of a worker thread waits for
the device to be mounted again (at most MOUNT_WAIT_S; an unplug and the window's closing end
the wait at once); the main thread and the session's own thread never wait. The device and
gvfs are made up (gio_fake): its gvfs refuses an unmount while a file is open, which here is
only while it is read (real gvfs: while an operation is under way), and not during a copy."""

import logging
import threading
import time
import types

import pytest
from gio_fake import GioOf, GioOp, GvfsDevice, MtpFiles, in_a_worker, settle

from openexpress.device import session_mark

gio_storage = pytest.importorskip("openexpress.device.gio_storage")

URI = "mtp://Made_Up_Device/"
XML = b"<Device>made up</Device>"
LOGGER = "openexpress.device.gio_storage"
PLACE = (3, 14)
BUSY = "Cannot unmount the device: File system is busy"
BUSY_TEXT = ("The device is busy with another operation, such as a copy in the file manager or "
             "another task of openExpress. Try again when it ends.")
LIMIT_S = 10.0  # no test waits this long: a thread still alive by then is stuck


@pytest.fixture
def made(monkeypatch):
    """make(plan=None, **device): the made-up device in gvfs (GvfsDevice) with its files over
    MTP (MtpFiles on it) and a GioStorage of them, with times of a fraction of a second."""
    for name, value in (("UNMOUNT_TIMEOUT_S", 2.0), ("MOUNT_TIMEOUT_S", 1.0),
                        ("CANCEL_WAIT_S", 0.2), ("SETTLED_S", 0.2), ("SETTLE_POLL_S", 0.01),
                        ("SETTLE_LIMIT_S", 2.0), ("REMOUNT_READY_S", 1),
                        ("OWN_USE_WAIT_S", 2.0), ("UNMOUNT_BUSY_PAUSE_S", 0.01),
                        ("MOUNT_WAIT_S", 5.0), ("UNPLUG_POLL_S", 0.02)):
        monkeypatch.setattr(gio_storage, name, value, raising=False)
    monkeypatch.setattr(gio_storage, "_quiet_until", {})
    monkeypatch.setattr(gio_storage, "_quiet_usb", {})
    real_gio = gio_storage.Gio
    made_now = []

    def make(plan=None, **kw):
        device = GvfsDevice(URI, unmount_takes_s=0.02, **kw)
        monkeypatch.setattr(gio_storage, "Gio", GioOf(real_gio, device))
        files = MtpFiles({"GARMIN": None, "GARMIN/GarminDevice.xml": XML,
                          "GARMIN/NewFiles": None}, plan=plan, device=device)
        st = gio_storage.GioStorage(URI + "Internal")
        st.root = files.file()
        st.device_uri = URI
        made_now.append((device, files))
        return device, files, st

    yield make
    for device, files in made_now:
        device.stop()
        files.clean()


def the_session_storage(device):
    """What a session of the device asks of its storage, at 3-14 on USB."""
    return types.SimpleNamespace(device_uri=URI, usb=gio_storage.GarminUsb(None, *PLACE),
                                 exists=device.exists)


def a_session(storage, inside=None):
    """A session of our own as _exclusive takes it, `inside()` run in it: "done", or the
    error it ended with."""
    try:
        with gio_storage._unmounted(storage, "PTP", "session closed, device mounted again",
                                    "the session"):
            if inside is not None:
                inside()
        return "done"
    except BaseException as e:  # noqa: BLE001 - told to the test
        return e


def until(condition, limit_s=LIMIT_S) -> None:
    deadline = time.monotonic() + limit_s
    while not condition():
        assert time.monotonic() < deadline, "it never came"
        time.sleep(0.005)


def ended(*threads) -> None:
    settle(*threads, limit_s=LIMIT_S)
    assert not any(t.is_alive() for t in threads), "a thread is stuck"


def lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# --- the operations of this process end first ------------------------------------------------

def test_a_session_waits_until_another_thread_has_closed_its_file(made, caplog):
    """As a window opening a watch: a reading of the device is still under way (gvfs then
    refuses the unmount) when the reading of the folders is let go and its session begins."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    device, _files, st = made(plan={("chunk", 1): GioOp(when="before", delay_s=0.3)})
    reader, read = in_a_worker(lambda: st.read("GARMIN/GarminDevice.xml"), "diagnostics")
    until(lambda: device.open_files == 1)
    task, session = in_a_worker(lambda: a_session(the_session_storage(device)), "task")
    ended(reader, task)
    assert read["value"] == XML and session["value"] == "done"
    assert device.unmounts_asked == 1 and device.events == ["unmounted", "mounted"]
    assert "PTP: waits until 1 operation(s) of openExpress on the device end" in lines(caplog)


def test_a_session_waits_until_a_write_of_another_thread_has_ended(made):
    """The made-up gvfs does not refuse an unmount during a copy, so the test shows the
    session's own wait: without it the device would be taken away in the middle of the write."""
    device, _files, st = made()
    sending, release, mounted_at_the_end = threading.Event(), threading.Event(), []

    def send(local, real_path, progress, what):
        sending.set()
        assert release.wait(LIMIT_S)
        mounted_at_the_end.append(device.mounted)

    st._send = send
    writer, wrote = in_a_worker(lambda: st.write_file("GARMIN/NewFiles/NEW.BIN", __file__),
                                "a write")
    assert sending.wait(LIMIT_S)
    task, session = in_a_worker(lambda: a_session(the_session_storage(device)), "task")
    time.sleep(0.2)
    assert device.unmounts_asked == 0  # still waiting for the write
    release.set()
    ended(writer, task)
    assert wrote["value"] == "GARMIN/NewFiles/NEW.BIN" and mounted_at_the_end == [True]
    assert session["value"] == "done" and device.events == ["unmounted", "mounted"]


def test_a_write_longer_than_the_wait_leaves_the_device_mounted_and_is_not_cut(
        made, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(gio_storage, "OWN_USE_WAIT_S", 0.2)
    device, _files, st = made()
    sending, release = threading.Event(), threading.Event()

    def send(local, real_path, progress, what):
        sending.set()
        assert release.wait(LIMIT_S)

    st._send = send
    writer, wrote = in_a_worker(lambda: st.write_file("GARMIN/NewFiles/NEW.BIN", __file__),
                                "a write")
    assert sending.wait(LIMIT_S)
    task, session = in_a_worker(lambda: a_session(the_session_storage(device)), "task")
    ended(task)
    refused = session["value"]
    release.set()
    ended(writer)
    assert isinstance(refused, gio_storage.DeviceBusy)
    assert str(refused) == BUSY_TEXT
    assert device.unmounts_asked == 0 and device.events == [] and device.mounted
    assert wrote["value"] == "GARMIN/NewFiles/NEW.BIN"
    assert not session_mark.active(PLACE)  # the other processes are told it is over
    assert ("PTP: 1 operation(s) of openExpress on the device still under way after 0.2 s; "
            "the device is not unmounted") in lines(caplog)


def test_a_copy_longer_than_the_wait_leaves_the_device_mounted_and_is_not_cut(
        made, monkeypatch, tmp_path):
    """A large file copied whole (copy_to_local) is one long operation on the device: a
    session asked meanwhile waits OWN_USE_WAIT_S, then gives up as busy, and the copy ends
    whole."""
    monkeypatch.setattr(gio_storage, "OWN_USE_WAIT_S", 0.2)
    device, files, st = made()
    size = 3 * 1024 * 1024
    files.tree["GARMIN/BIG.BIN"] = size
    files.copy_piece_delay_s = 0.25  # three pieces of 1 MiB: about 0.75 s
    dest = tmp_path / "copy.bin"
    copier, copied = in_a_worker(lambda: st.copy_to_local("GARMIN/BIG.BIN", dest, size),
                                 "a copy")
    until(lambda: "copy" in files.calls)
    task, session = in_a_worker(lambda: a_session(the_session_storage(device)), "task")
    ended(task)
    refused = session["value"]
    ended(copier)
    assert isinstance(refused, gio_storage.DeviceBusy)
    assert copied["value"] == size and dest.read_bytes() == bytes(size)
    assert device.unmounts_asked == 0 and device.events == [] and device.mounted


# --- gvfs refuses it as busy: another program reads the device -------------------------------

@pytest.mark.parametrize("where", ["command line", "window"])
def test_an_unmount_refused_as_busy_is_asked_again(made, caplog, where):
    caplog.set_level(logging.INFO, logger=LOGGER)
    device, _files, _st = made(busy_refusals=2)
    if where == "command line":
        done = a_session(the_session_storage(device))
    else:
        task, session = in_a_worker(lambda: a_session(the_session_storage(device)), "task")
        ended(task)
        done = session["value"]
    assert done == "done" and device.unmounts_asked == 3
    assert device.events == ["unmounted", "mounted"]
    said = lines(caplog)
    assert [line for line in said if "busy" in line] == [
        "PTP: gvfs says the device is busy, tried again in 0.0 s (2 of 5)",
        "PTP: gvfs says the device is busy, tried again in 0.0 s (3 of 5)"]
    assert not any("waits until" in line for line in said)  # nothing of its own to wait for


def test_a_device_busy_at_every_try_stays_mounted(made, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    monkeypatch.setattr(gio_storage, "REMOUNT_QUIET_S", 0)  # the mark's quiet over at once
    device, _files, _st = made(busy_refusals=99)
    refused = a_session(the_session_storage(device))
    assert isinstance(refused, gio_storage.DeviceBusy) and str(refused) == BUSY_TEXT
    assert str(refused.__cause__) == BUSY  # the words of gvfs, for the log
    assert device.unmounts_asked == 5 and device.events == [] and device.mounted
    assert not session_mark.active(PLACE)
    assert "PTP: gvfs said the device is busy 5 times; not unmounted" in lines(caplog)


def test_a_busy_device_is_said_in_polish_without_the_words_of_the_system():
    from fakes import in_language

    with in_language("pl"):
        said = gio_storage.busy_text()
    assert said == ("Urządzenie jest zajęte inną operacją, np. kopiowaniem w\u00a0menedżerze plików "
                    "albo innym zadaniem openExpressu. Spróbuj ponownie, gdy się skończy.")


# --- an operation that meets a session waits for the mount -----------------------------------

@pytest.fixture
def held(made):
    """make(**device): a session of the device running in a worker thread until `release`
    is set; (device, files, storage, release, the session's thread and what it said)."""
    def make(**kw):
        device, files, st = made(**kw)
        inside, release = threading.Event(), threading.Event()

        def in_it():
            inside.set()
            assert release.wait(LIMIT_S)

        task, session = in_a_worker(lambda: a_session(the_session_storage(device), in_it),
                                    "task")
        assert inside.wait(LIMIT_S)
        assert not device.mounted
        return device, files, st, release, task, session

    return make


def test_a_read_of_a_worker_during_a_session_waits_until_the_device_is_back(held, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    device, files, st, release, task, session = held()
    reader, read = in_a_worker(lambda: st.read("GARMIN/GarminDevice.xml"), "a page")
    time.sleep(0.2)
    assert reader.is_alive() and files.calls == []  # nothing asked of the unmounted device
    release.set()
    ended(task, reader)
    assert session["value"] == "done" and read["value"] == XML
    assert files.mounted_at and all(files.mounted_at)
    assert ("gio: an operation on the device waits until openExpress's own session ends"
            in lines(caplog))


@pytest.mark.parametrize("ends", ["unplugged", "window closed", "time limit"])
def test_a_wait_for_the_mount_ends_at_an_unplug_the_window_closing_or_its_limit(
        held, monkeypatch, caplog, ends):
    """Then the read goes on and meets the device as it is, with the error it would have
    without the wait; the session itself is not touched."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    plugged = {"in": True}
    monkeypatch.setattr(gio_storage, "usb_still_there", lambda usb: plugged["in"])
    if ends == "time limit":
        monkeypatch.setattr(gio_storage, "MOUNT_WAIT_S", 0.3)
    device, _files, st, release, task, _session = held()
    st.usb = gio_storage.GarminUsb(None, *PLACE)
    reader, read = in_a_worker(lambda: st.read_at("GARMIN/GarminDevice.xml"), "a page")
    time.sleep(0.1)
    assert reader.is_alive()
    begun = time.monotonic()
    if ends == "unplugged":
        plugged["in"] = False
    elif ends == "window closed":
        gio_storage.stop_waiting()
    ended(reader)
    took = time.monotonic() - begun
    assert isinstance(read["error"], gio_storage.DeviceError)
    assert str(read["error"]) == ("Cannot read GARMIN/GarminDevice.xml: The specified location "
                                  "is not mounted")
    assert took < 1.0 and task.is_alive()
    said = {"unplugged": "gio: the device was unplugged while an operation waited for it",
            "window closed": "gio: the window closes, an operation on the device waits no more",
            "time limit": ("gio: an operation waited 0.3 s for the device to be mounted again; "
                           "it goes on")}
    assert said[ends] in lines(caplog)
    release.set()
    ended(task)


def test_the_main_thread_never_waits_for_a_session(held):
    """The window's loop (and the command line): what it asks goes on at once, as without a
    session."""
    _device, _files, st, release, task, _session = held()
    begun = time.monotonic()
    with pytest.raises(gio_storage.DeviceError, match="not mounted"):
        st.info("GARMIN")
    assert time.monotonic() - begun < 1.0
    release.set()
    ended(task)


def test_the_sessions_own_thread_never_waits_for_itself(made):
    """What the session's thread asks of gvfs in the session goes on at once, also when that
    thread opened the session within an operation of its own (a write that needs one)."""
    device, _files, st = made()
    asked = {}

    def in_it():
        begun = time.monotonic()
        try:
            st.info("GARMIN")
        except gio_storage.DeviceError as e:
            asked["error"] = str(e)
        asked["took"] = time.monotonic() - begun

    def within_an_operation():
        with st._in_use():
            return a_session(the_session_storage(device), in_it)

    task, session = in_a_worker(within_an_operation, "task")
    ended(task)
    assert session["value"] == "done" and asked["took"] < 1.0
    assert "not mounted" in asked["error"]
    assert device.events == ["unmounted", "mounted"]


def test_a_session_of_the_command_line_with_nothing_else_running_waits_for_nothing(
        made, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    device, _files, st = made()
    assert st.read("GARMIN/GarminDevice.xml") == XML  # done before, as a command reads
    assert a_session(the_session_storage(device), lambda: None) == "done"
    assert device.unmounts_asked == 1 and device.events == ["unmounted", "mounted"]
    assert not any("waits" in line or "busy" in line for line in lines(caplog))

"""The Updates page after openExpress copied an update: the file it wrote is not read back from
the device a second time to be recognised (its size, the server's MD5 and its time on the device
are kept from the copy, also for the next window or command), and a file that must be read says
so with how far the read is. A made-up watch over a folder; all files are made up."""

import hashlib

import pytest

from openexpress import download, updates
from openexpress.connect import lower_keys
from openexpress.device.storage import LocalStorage

SOFTWARE = b"made-up watch software, of the size the server offers"


def server_answer():
    return {"updates": [{
        "partNumber": "006-B0000-00", "productName": "Made-up Watch", "dataType": "Firmware",
        "displayVersion": "2.18", "severity": "Recommended", "isPrimaryFirmware": True,
        "locale": None, "releaseState": "live", "releaseNotes": [], "eulaUrl": None,
        "deliverable": {"downloadUrl": "https://example.org/GUPDATE.GSP",
                        "md5": hashlib.md5(SOFTWARE).hexdigest().upper(),
                        "sizeInBytes": len(SOFTWARE)},
        "installation": {"path": "Garmin", "fileName": "GUPDATE.GSP", "isReinstall": False,
                         "isProductInstalled": True, "isRestartRequired": False, "order": 0}}]}


class _Counting(LocalStorage):
    """The made-up watch, counting the whole reads of a file."""

    def __init__(self, root):
        super().__init__(root)
        self.reads = []

    def _read(self, real_path):
        if real_path.upper().endswith("GUPDATE.GSP"):
            self.reads.append(real_path)
        return super()._read(real_path)


@pytest.fixture
def watch(device_root, monkeypatch, tmp_path):
    def fetch(urls, dest, size, md5, session=None, progress=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(SOFTWARE)
        return dest

    monkeypatch.setattr(updates, "cache_path", lambda u: tmp_path / "cache" / u.filename)
    monkeypatch.setattr(download, "fetch", fetch)
    return _Counting(device_root)


def _offered(storage, progress=None):
    from openexpress import service

    device = service.read_device(storage, found_now=False)
    return updates.offered(storage, lower_keys(server_answer()), device, forget_installed=False,
                           progress=progress)


def test_the_file_it_copied_is_not_read_again(watch, xdg):
    (u,) = _offered(watch)
    updates.install([u], watch)
    watch.reads.clear()
    (again,) = _offered(watch)
    assert again.on_device and watch.reads == []


def test_nor_by_the_next_window_or_command(watch, xdg, monkeypatch):
    (u,) = _offered(watch)
    updates.install([u], watch)
    monkeypatch.setattr(updates, "_md5_read", {})  # another process: nothing in its memory
    watch.reads.clear()
    (again,) = _offered(watch)
    assert again.on_device and watch.reads == []


def test_a_file_changed_since_is_read(watch, xdg, device_root):
    import os

    (u,) = _offered(watch)
    updates.install([u], watch)
    written = next(device_root.rglob("GUPDATE.GSP"))
    os.utime(written, (1_700_000_000, 1_700_000_000))  # another time on the device
    watch.reads.clear()
    (again,) = _offered(watch)
    assert again.on_device and len(watch.reads) == 1


def test_a_file_it_did_not_copy_is_read_once_with_how_far(watch, xdg, device_root):
    (device_root / "GARMIN" / "GUPDATE.GSP").write_bytes(SOFTWARE)  # copied by someone else
    told = []
    (u,) = _offered(watch, progress=told.append)
    assert u.on_device and len(watch.reads) == 1
    n = len(SOFTWARE)
    assert told[-1].replace("\u00a0", " ") == (
        f"Checking the update file · {n} B of {n} B")
    watch.reads.clear()
    (again,) = _offered(watch)
    assert again.on_device and watch.reads == []  # its MD5 kept


# --- the time of a file openExpress wrote, given again by the device -----------------------------
#
# A device may give a file it was given another time later, in steps of 2 s, rounded down: what
# openExpress wrote itself (with the server's MD5) is still known at a time up to
# WRITTEN_TIME_SLACK_S earlier, never at a later one (another write). A file read whole is known
# only at the very time it was read.


def _written(watch, device_root):
    (u,) = _offered(watch)
    updates.install([u], watch)
    return next(device_root.rglob("GUPDATE.GSP"))


def _moved(path, by: float) -> None:
    import os

    t = path.stat().st_mtime + by
    os.utime(path, (t, t))


def _another_process(monkeypatch, watch):
    monkeypatch.setattr(updates, "_md5_read", {})  # nothing in the memory of this process
    watch.reads.clear()


def test_our_file_given_back_two_seconds_earlier_is_not_read_again(watch, xdg, device_root,
                                                                   monkeypatch, caplog):
    import logging

    written = _written(watch, device_root)
    _moved(written, -1.6)  # written at 12:00:01.6, given back as 12:00:00
    _another_process(monkeypatch, watch)
    with caplog.at_level(logging.INFO, logger="openexpress"):
        (again,) = _offered(watch)
    assert again.on_device and watch.reads == []
    said = [r.getMessage() for r in caplog.records if "written by openExpress" in r.getMessage()]
    assert len(said) == 1 and said[0].endswith("(1.6 s earlier, within 2 s): known from that write, "
                                               "not read")


def test_our_file_three_seconds_apart_is_read_again_and_the_log_says_why(watch, xdg, device_root,
                                                                       monkeypatch, caplog):
    import logging

    written = _written(watch, device_root)
    _moved(written, -3)
    _another_process(monkeypatch, watch)
    with caplog.at_level(logging.INFO, logger="openexpress"):
        (again,) = _offered(watch)
    assert again.on_device and len(watch.reads) == 1
    said = [r.getMessage() for r in caplog.records if "written by openExpress" in r.getMessage()]
    assert len(said) == 1 and said[0].endswith("(3 s earlier): read again")
    assert said[0].startswith("updates: GUPDATE.GSP written by openExpress with the time ")


def test_our_file_with_a_later_time_is_another_write_and_read_again(watch, xdg, device_root,
                                                                     monkeypatch, caplog):
    """A device rounds a time down: a later one is a write after ours (of the same size)."""
    import logging

    written = _written(watch, device_root)
    _moved(written, 1)
    _another_process(monkeypatch, watch)
    with caplog.at_level(logging.INFO, logger="openexpress"):
        (again,) = _offered(watch)
    assert again.on_device and len(watch.reads) == 1
    said = [r.getMessage() for r in caplog.records if "written by openExpress" in r.getMessage()]
    assert len(said) == 1 and said[0].endswith("(1 s later): read again")


def test_a_file_read_whole_is_read_again_at_another_time_even_one_second_apart(watch, xdg,
                                                                             device_root,
                                                                             monkeypatch):
    on_device = device_root / "GARMIN" / "GUPDATE.GSP"
    on_device.write_bytes(SOFTWARE)  # copied by someone else: read once, kept by its time
    _offered(watch)
    _moved(on_device, 1)
    _another_process(monkeypatch, watch)
    (again,) = _offered(watch)
    assert again.on_device and len(watch.reads) == 1


def test_our_file_without_a_time_is_read(watch, xdg, device_root, monkeypatch):
    import os

    written = _written(watch, device_root)
    os.utime(written, (0, 0))  # no time known
    _another_process(monkeypatch, watch)
    (again,) = _offered(watch)
    assert again.on_device and len(watch.reads) == 1


def test_our_file_of_another_size_is_not_read_and_differs(watch, xdg, device_root, monkeypatch):
    written = _written(watch, device_root)
    written.write_bytes(SOFTWARE + b" and more")
    _another_process(monkeypatch, watch)
    (again,) = _offered(watch)
    assert not again.on_device and watch.reads == []


def test_the_kept_checksums_of_before_still_count(watch, xdg, device_root, monkeypatch):
    """md5-on-device.json as it was written before: a key of the device, the path, the size and
    the time, and the MD5. Its entries still spare a read."""
    import json

    from openexpress import paths

    on_device = device_root / "GARMIN" / "GUPDATE.GSP"
    on_device.write_bytes(SOFTWARE)
    real = watch.resolve("GARMIN/GUPDATE.GSP")
    entry = watch.info(real)
    key = "|".join((hashlib.sha256(str(device_root).encode()).hexdigest()[:32], real,
                    str(entry.size), repr(float(entry.mtime))))
    kept = paths.cache_dir() / updates.MD5_KEPT
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(json.dumps({key: hashlib.md5(SOFTWARE).hexdigest()}))
    _another_process(monkeypatch, watch)
    (u,) = _offered(watch)
    assert u.on_device and watch.reads == []


def test_at_most_so_many_checksums_are_kept_and_written_whole(watch, xdg, device_root,
                                                              monkeypatch):
    import json

    from openexpress import paths

    kept = paths.cache_dir() / updates.MD5_KEPT
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_text(json.dumps({f"made-up|{n}|1|1.0": "0" * 32
                                for n in range(updates.MD5_KEPT_AT_MOST)}))
    _written(watch, device_root)
    now = json.loads(kept.read_text())
    assert len(now) == updates.MD5_KEPT_AT_MOST and "made-up|0|1|1.0" not in now
    assert not list(kept.parent.glob("*.tmp"))  # written to a temporary file, then put in place


def test_our_second_copy_of_a_file_takes_the_place_of_the_first(watch, xdg, device_root,
                                                                monkeypatch):
    import json

    from openexpress import paths

    (u,) = _offered(watch)
    updates.install([u], watch)
    written = next(device_root.rglob("GUPDATE.GSP"))
    _moved(written, -100)  # the first copy, a while ago
    updates.install([u], watch)  # written again
    kept = json.loads((paths.cache_dir() / updates.MD5_KEPT).read_text())
    ours = [v for k, v in kept.items() if k.startswith("written|") and k.endswith("GUPDATE.GSP")]
    assert len(ours) == 1 and ours[0]["mtime"] == written.stat().st_mtime
    assert [k for k in kept if not k.startswith("written|")] == []  # no other entry of a write

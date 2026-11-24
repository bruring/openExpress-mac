"""A device opened while it was still starting (gvfs offered it before its place on USB could be
read): a session of openExpress's own is not taken for impossible. Its place is read again from
/sys when the session is asked for, and a hidden folder is read or written after a short wait,
not refused at once. All devices here are made up."""

import contextlib
import types

import pytest
from fakes import hidden_folder_waits, usb_devices

from openexpress import hidden_folders
from openexpress.device import gio_storage
from openexpress.device.gio_storage import GarminUsb, GioStorage, usb_place
from openexpress.device.storage import LocalStorage


@pytest.fixture
def opening(monkeypatch):
    """A session opened without libusb nor a device: what it was opened for."""
    opened = []
    monkeypatch.setattr(gio_storage.ptp, "load_libusb", lambda: None)
    monkeypatch.setattr(gio_storage, "_exclusive",
                        lambda storage, node: opened.append(usb_place(storage.usb)) or "session")
    return opened


def _storage_found_while_starting():
    storage = GioStorage("mtp://Garmin_made_up_watch/")
    storage.device_uri = storage.uri if hasattr(storage, "uri") else "mtp://Garmin_made_up_watch/"
    storage.usb = GarminUsb(None)  # gvfs offered it; its place on USB not read yet
    return storage


def test_the_place_on_usb_is_read_again_when_a_session_is_asked(monkeypatch, tmp_path, opening):
    put = usb_devices(monkeypatch, tmp_path / "usb")
    put({"1-4": ("51b4", 1, 9)})
    storage = _storage_found_while_starting()
    assert storage.exclusive_ptp() == "session"
    assert opening == [(1, 9)] and usb_place(storage.usb) == (1, 9)


def test_two_garmin_devices_leave_the_place_unknown(monkeypatch, tmp_path, opening):
    put = usb_devices(monkeypatch, tmp_path / "usb")
    put({"1-4": ("51b4", 1, 9), "2-1": ("51b4", 2, 3)})
    storage = _storage_found_while_starting()
    assert storage.exclusive_ptp() is None and opening == []


class _Starting(LocalStorage):
    """A made-up watch over MTP whose place on USB is known only after `starting` asks."""

    def __init__(self, root, starting, blocked=False):
        super().__init__(root)
        self.device_uri = "mtp://made-up-watch/"
        self.usb = GarminUsb(None)
        self.starting, self.blocked, self.asked = starting, blocked, 0

    def usb_blocked(self):
        return self.blocked

    def exclusive_ptp(self):
        self.asked += 1
        if self.starting:
            self.starting -= 1
            return None
        self.usb = GarminUsb("51b4", 1, 9)

        @contextlib.contextmanager
        def opened():
            yield types.SimpleNamespace()

        return opened()


@pytest.fixture
def waits(monkeypatch):
    return hidden_folder_waits(monkeypatch)


def test_a_device_still_starting_is_waited_for(tmp_path, waits):
    storage = _Starting(tmp_path, starting=2)
    told = []
    opener = hidden_folders.session_for(storage, told.append)
    assert opener is not None and storage.asked == 3 and len(waits) == 2
    assert told == ["Waiting for the device to get ready…"] * 2


def test_a_device_whose_place_stays_unknown_is_refused_after_the_wait(tmp_path, waits):
    storage = _Starting(tmp_path, starting=99)
    assert hidden_folders.session_for(storage) is None
    assert sum(waits) == sum(hidden_folders.STARTING_WAITS) and storage.asked == 1 + len(waits)


def test_the_flatpak_without_usb_is_not_waited_for(tmp_path, waits):
    storage = _Starting(tmp_path, starting=99, blocked=True)
    assert hidden_folders.session_for(storage) is None and waits == [] and storage.asked == 1

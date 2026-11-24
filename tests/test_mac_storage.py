"""The storage of an MTP device on macOS (MacStorage, over the program's own PTP session), and
how it is found and opened there (mac_storage, platform.mac_usb). Against the fake device of
ptp_fake: no USB device is listed or opened, ptpcamerad is never signalled, and all data is
made up. Nothing here needs PyGObject."""

import plistlib
import re
import signal
import struct
import time
import types

import pytest
from ptp_fake import STORAGE, FakeDevice

from openexpress.device import found, mac_storage, ptp
from openexpress.device.mac_storage import MacStorage
from openexpress.device.storage import CopyNotStarted, DeviceError, DeviceNotResponding
from openexpress.platform import mac_usb

# The functions conftest replaces in every test (no_usb_on_macos), as they are, for the tests of
# their own.
REGISTERED_DEVICES = mac_usb.registered_devices
GARMIN_DEVICES = mac_usb.garmin_devices  # the real one; conftest stubs it for every test
DAEMON_STOPPED = mac_usb.daemon_stopped

XML = (b'<Device xmlns="http://www.garmin.com/xmlschemas/GarminDevice/v2">'
       b"<Id>3000000001</Id></Device>")
URI = "mtp://[usb:008,037]/"


def _fake() -> FakeDevice:
    """A device with its GarminDevice.xml, an activity, an app folder it took over and an app
    in it, a folder at its top level; it makes folders when asked."""
    fake = FakeDevice()
    fake.add("GARMIN/GarminDevice.xml", XML)
    fake.add("GARMIN/Activity/2026-11-17-09-30-00.fit", b"fit" * 100)
    fake.add("GARMIN/Apps/Media", hidden=True)
    fake.add("GARMIN/Apps/Media/A1B2C3D4.PRG", b"old app", hidden=True)
    fake.add("Music")
    fake.allows_folders = True
    return fake


@pytest.fixture
def device():
    return _fake()


@pytest.fixture
def storage(device):
    session = ptp.Session(device)
    session.open()
    st = MacStorage(session, STORAGE, found.GarminUsb("51b8", 8, 37), URI, "Internal Storage")
    yield st
    st.close()


@pytest.fixture(autouse=True)
def nothing_left_open():
    """Each test begins without a device open in the process and without one that stopped
    responding, and leaves none."""
    yield
    mac_storage.close_all()
    mac_storage._stalled_at.clear()


def _sent(device: FakeDevice, operation: int) -> int:
    return device.sent(operation)


# --- reading -------------------------------------------------------------------------------

def test_a_folder_lists_what_ordinary_mtp_shows_and_not_what_the_device_hides(storage):
    assert sorted(e.name for e in storage.listdir("GARMIN")) == [
        "Activity", "Apps", "GarminDevice.xml"]
    assert storage.listdir("GARMIN/Apps") == []  # Media only in Garmin's own listing
    assert not storage.exists("GARMIN/Apps/Media/A1B2C3D4.PRG")


def test_paths_are_looked_up_in_any_letter_case_and_given_in_the_devices(storage):
    assert storage.resolve("garmin/garmindevice.XML") == "GARMIN/GarminDevice.xml"
    assert storage.read("Garmin/GarminDevice.xml") == XML
    assert storage.resolve("GARMIN/Nowhere") is None


def test_a_folder_is_listed_once_and_then_looked_up_without_asking_the_device(storage, device):
    storage.read("GARMIN/GarminDevice.xml")
    listings = _sent(device, ptp.GET_OBJECT_HANDLES)
    storage.read("garmin/GarminDevice.xml")
    storage.size("GARMIN/Activity/2026-11-17-09-30-00.fit")
    assert _sent(device, ptp.GET_OBJECT_HANDLES) == listings + 1  # Activity, once
    assert _sent(device, ptp.GARMIN_GET_OBJECT_IDS) == 0  # never Garmin's own listing


def test_entries_have_the_size_and_the_modification_date_the_device_tells(storage, device):
    device.objects[device.handle("GARMIN/Activity/2026-11-17-09-30-00.fit")].modified = \
        "20261117T093000Z"
    (entry,) = storage.listdir("GARMIN/Activity")
    assert (entry.name, entry.is_dir, entry.size) == ("2026-11-17-09-30-00.fit", False, 300)
    assert entry.mtime == 1794907800.0
    assert next(e for e in storage.listdir("") if e.name == "GARMIN").size == 0


def test_the_start_of_a_file_is_read_alone_with_get_partial_object(storage, device):
    assert storage.read_start("GARMIN/Activity/2026-11-17-09-30-00.fit", 5) == b"fitfi"
    assert _sent(device, ptp.GET_PARTIAL_OBJECT) == 1 and _sent(device, ptp.GET_OBJECT) == 0


def test_the_start_of_a_file_on_a_device_without_partial_reads_is_cut_from_the_whole(
        storage, device):
    device.refuse[ptp.GET_PARTIAL_OBJECT] = ptp.OPERATION_NOT_SUPPORTED
    assert storage.read_start("GARMIN/GarminDevice.xml", 8) == XML[:8]
    assert _sent(device, ptp.GET_OBJECT) == 1


def test_a_file_not_there_is_said_by_its_path(storage):
    with pytest.raises(DeviceError, match="GARMIN/Nothing.fit"):
        storage.read("GARMIN/Nothing.fit")


def test_info_asks_the_device_again_for_a_file_it_listed(storage, device):
    """A file the device wrote while connected has its size now, not the size listed."""
    path = "GARMIN/Activity/2026-11-17-09-30-00.fit"
    assert storage.size(path) == 300
    device.objects[device.handle(path)].data = b"x" * 512
    assert storage.size(path) == 300  # as listed
    assert storage.info(path).size == 512
    assert storage.size(path) == 512  # the listing kept has it too


def test_refresh_drops_what_was_listed(storage, device):
    path = "GARMIN/Activity/2026-11-17-09-30-00.fit"
    storage.size(path)
    device.add("GARMIN/Activity/2026-11-17-10-00-00.fit", b"new")
    assert storage.size("GARMIN/Activity/2026-11-17-10-00-00.fit") is None
    assert storage.refresh() is True
    assert storage.size("GARMIN/Activity/2026-11-17-10-00-00.fit") == 3


def test_free_space_and_capacity_come_from_the_storage_info(storage, device):
    assert storage.free_space() == device.free
    assert storage.capacity() == 2 * device.free
    assert storage.label == URI + "Internal Storage"


def test_the_free_space_and_the_capacity_are_one_question_until_something_changes_them(
        storage, device):
    asked = _sent(device, ptp.GET_STORAGE_INFO)
    storage.free_space(), storage.capacity(), storage.free_space()
    assert _sent(device, ptp.GET_STORAGE_INFO) == asked + 1
    storage.write("Music/new.mp3", b"x" * 1000)
    assert storage.free_space() == device.free  # asked again after the write
    assert _sent(device, ptp.GET_STORAGE_INFO) == asked + 2
    storage.refresh()
    storage.capacity()
    assert _sent(device, ptp.GET_STORAGE_INFO) == asked + 3


# --- writing -------------------------------------------------------------------------------

def test_a_new_file_is_written_into_its_folder_and_listed_at_its_size(storage, device):
    real = storage.write_checked("garmin/activity/NEW.FIT", b"0123456789")
    assert real == "GARMIN/Activity/NEW.FIT"
    assert device.objects[device.handle("GARMIN/Activity/NEW.FIT")].data == b"0123456789"
    assert storage.size("GARMIN/Activity/NEW.FIT") == 10


def test_a_file_written_again_deletes_the_old_one_first(storage, device):
    old = device.handle("GARMIN/GarminDevice.xml")
    storage.write("GARMIN/GarminDevice.xml", b"<Device/>")
    assert old not in device.objects
    assert device.names("GARMIN").count("GarminDevice.xml") == 1
    assert storage.read("GARMIN/GarminDevice.xml") == b"<Device/>"
    assert storage.replaces_atomically is False


def test_missing_folders_are_made_on_the_way_as_libmtp_makes_them(storage, device):
    storage.write("Music/Album/Song.mp3", b"la la")
    assert device.objects[device.handle("Music/Album")].data is None  # a folder
    assert device.objects[device.handle("Music/Album/Song.mp3")].data == b"la la"
    folder_info = next(t for t in device.transfers
                       if len(t) > 12 and struct.unpack_from("<H", t, 4)[0] == ptp.FORMAT_FOLDER)
    assert folder_info == ptp.folder_info_dataset("Album")
    assert storage.make_dir("Music/Album") == "Music/Album"  # there: not made again


def test_a_folder_at_the_top_level_is_made_there(storage, device):
    assert storage.make_dir("NEWFILES") == "NEWFILES"
    assert device.objects[device.handle("NEWFILES")].parent == ptp.ROOT


def test_a_folder_the_device_refuses_to_make_writes_nothing(storage, device):
    device.allows_folders = False
    with pytest.raises(DeviceError, match="Cannot create directory Music/Album"):
        storage.write("Music/Album/Song.mp3", b"la la")
    assert device.names("Music") == []


def test_a_large_file_is_sent_as_it_is_read_with_its_progress(storage, device, tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(ptp, "WRITE_CHUNK", 1000)
    local = tmp_path / "map.img"
    local.write_bytes(bytes(range(256)) * 10)
    told = []
    storage.write_file_checked("GARMIN/Map.img", local, lambda done, total: told.append(
        (done, total)))
    assert device.objects[device.handle("GARMIN/Map.img")].data == local.read_bytes()
    assert told == [(1000, 2560), (2000, 2560), (2560, 2560)]


def test_a_local_file_that_cannot_be_read_leaves_the_old_file_as_it_was(storage, device,
                                                                         tmp_path):
    with pytest.raises(CopyNotStarted):
        storage.write_file("GARMIN/GarminDevice.xml", tmp_path / "missing")
    assert device.objects[device.handle("GARMIN/GarminDevice.xml")].data == XML


def test_a_new_file_the_device_refuses_before_anything_is_sent_is_copy_not_started(storage,
                                                                                      device):
    device.refuse_names["FULL.FIT"] = ptp.STORE_FULL
    with pytest.raises(CopyNotStarted, match="StoreFull"):
        storage.write("GARMIN/Activity/FULL.FIT", b"data")
    assert "FULL.FIT" not in device.names("GARMIN/Activity")


def test_what_a_send_that_broke_off_left_is_deleted_from_the_device(storage, device):
    """SendObjectInfo made the object, SendObject was refused: the object left under the name
    is removed (_discard_partial), and the listing does not show it."""
    device.refuse[ptp.SEND_OBJECT] = ptp.GENERAL_ERROR
    with pytest.raises(DeviceError, match="GeneralError"):
        storage.write("GARMIN/Activity/HALF.FIT", b"data")
    assert "HALF.FIT" not in device.names("GARMIN/Activity")
    assert not storage.exists("GARMIN/Activity/HALF.FIT")


def test_delete_removes_the_file_and_the_listing_forgets_it(storage, device):
    assert storage.delete("garmin/activity/2026-11-17-09-30-00.fit")
    assert device.names("GARMIN/Activity") == []
    assert storage.listdir("GARMIN/Activity") == []
    assert not storage.delete("GARMIN/Activity/2026-11-17-09-30-00.fit")


# --- the session lent, a device that stops responding, the end ------------------------------

def test_exclusive_ptp_lends_the_open_session_and_drops_the_listings_after_it(storage, device):
    """What ciq, satellite and hidden_folders do through it: Garmin's own listing and writes
    into a hidden folder, on the same session, without another one opened."""
    storage.listdir("GARMIN/Apps")
    with storage.exclusive_ptp() as session:
        assert session is storage._session
        media = session.lookup(STORAGE, "GARMIN/Apps/Media")
        session.send_file(STORAGE, media.handle, "0A1B2C3D.PRG", b"new app")
        session.delete(session.lookup(STORAGE, "GARMIN/Apps/Media/A1B2C3D4.PRG").handle)
    assert _sent(device, ptp.OPEN_SESSION) == 1
    assert storage._folders == {}
    assert storage.read("GARMIN/GarminDevice.xml") == XML  # the storage goes on


def test_a_silent_device_stopped_responding_and_is_let_go(storage, device):
    device.silent = True
    with pytest.raises(DeviceNotResponding, match="Unplug it and plug it in again"):
        storage.read("GARMIN/GarminDevice.xml")
    assert storage.stalled and device.closed
    asked = len(device.transfers)
    with pytest.raises(DeviceNotResponding):
        storage.listdir("GARMIN")  # at once: nothing more is asked of it
    with pytest.raises(DeviceNotResponding):
        storage.exclusive_ptp()
    assert len(device.transfers) == asked
    assert set(mac_storage._stalled_at) == {(8, 37)}


def test_close_ends_the_session_and_nothing_is_asked_afterwards(storage, device):
    storage.close()
    assert not device.session_open and device.closed
    with pytest.raises(DeviceError, match="closed"):
        storage.read("GARMIN/GarminDevice.xml")
    storage.close()  # a second close does nothing


# --- dates ---------------------------------------------------------------------------------

def test_dates_of_ptp_in_utc_with_an_offset_and_in_local_time(monkeypatch):
    assert ptp.ptp_time("20261117T093000Z") == 1794907800.0
    assert ptp.ptp_time("20261117T093000.5Z") == 1794907800.0
    assert ptp.ptp_time("20261117T103000+0100") == 1794907800.0
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert ptp.ptp_time("20261117T093000") == 1794907800.0
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    for text in ("", "2026", "20261117X093000", "20261117T0930AB", "20261117T093000+1"):
        assert ptp.ptp_time(text) == 0.0


# --- finding and opening a device -------------------------------------------------------------

def _usb(product="51b8", address=37, classes=("ff",), transfer=True, holder=None, name=None):
    return mac_usb.UsbDevice(8, address, product, classes, transfer, name, "000012345678",
                             holder)


@pytest.fixture
def usb(monkeypatch):
    """The Garmin devices on USB (`usb.devices`), a holder the IORegistry tells
    (`usb.holder`), and each fake device the transport opens (`usb.opened`): a fresh _fake()
    each time unless `usb.device` is set."""
    state = types.SimpleNamespace(devices=[_usb()], holder=None, opened=[], device=None)
    monkeypatch.setattr(mac_usb, "garmin_devices", lambda reuse=False: list(state.devices))
    monkeypatch.setattr(mac_usb, "holder_of", lambda bus, address: state.holder)

    def opened(bus, address):
        mac_usb.check_free(bus, address)
        state.opened.append((bus, address))
        return state.device or _fake()

    monkeypatch.setattr(mac_usb, "open_transport", opened)
    return state


def test_found_are_the_devices_offering_mtp_never_one_in_its_garmin_usb_mode(usb):
    usb.devices = [_usb(), _usb("0003", 38, ("ff",)), _usb("2b4a", 39, ("08",), False),
                   _usb("0b89", 40, ("03",), False)]
    (device,) = mac_storage.find_devices()
    assert (device.uri, device.kind, device.mounted) == (URI, "mtp", True)
    assert (device.usb.usb_id, device.usb.bus, device.usb.address) == ("091e:51b8", 8, 37)
    assert device.name == "Garmin device"
    assert mac_storage.find_all_devices() == [device]
    assert usb.opened == []  # nothing opened to find them


@pytest.mark.parametrize("devices, state", [
    ([], found.ABSENT),
    ([_usb("0003")], found.STARTING),
    ([_usb("0003"), _usb(address=38)], found.PRESENT),
    ([_usb("2b4a", classes=("08",), transfer=False)], found.MASS_STORAGE),
    ([_usb("0b89", classes=("03", "08"), transfer=False)], found.UNSUPPORTED),
    ([_usb("0b89", classes=("03",), transfer=False), _usb("0003", 38)], found.STARTING),
])
def test_the_usb_state_from_the_products_and_interfaces_on_usb(usb, devices, state):
    usb.devices = devices
    assert mac_storage.usb_state() == state


def test_a_device_in_its_garmin_usb_mode_is_said_with_its_usb_id(usb):
    usb.devices = [_usb("0003")]
    with pytest.raises(found.NotThereYet, match=r"Garmin USB mode \(091e:0003\)"):
        mac_storage.open_device_storage()
    assert usb.opened == []


def test_a_device_in_its_garmin_usb_mode_is_waited_for_until_it_offers_mtp(usb):
    usb.devices = [_usb("0003")]
    told, clock = [], [0.0]

    def sleep(seconds):
        clock[0] += seconds
        if clock[0] >= 3:
            usb.devices = [_usb()]

    st = mac_storage.open_device_storage(wait=True, notify=told.append, sleep=sleep,
                                         clock=lambda: clock[0])
    assert st.read("GARMIN/GarminDevice.xml") == XML
    assert told == ["The device is starting…"]


def test_a_device_that_stays_in_its_garmin_usb_mode_is_given_up_after_the_wait(usb):
    usb.devices = [_usb("0003")]
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    with pytest.raises(found.NotThereYet, match="091e:0003"):
        mac_storage.open_device_storage(wait=True, sleep=sleep, clock=lambda: clock[0])
    assert clock[0] == found.WAIT_FOR_DEVICE_S


def test_no_garmin_device_on_usb_is_no_device(usb):
    usb.devices = []
    with pytest.raises(found.NoDevice, match="No Garmin device"):
        mac_storage.open_device_storage(wait=True)


def test_a_garmin_drive_is_not_opened_over_mtp_and_said_as_a_drive(usb):
    """Not waited for over MTP: it is opened as a drive (test_mac_drives)."""
    usb.devices = [_usb("2b4a", classes=("08",), transfer=False)]
    with pytest.raises(found.MassStorageNotice, match="USB drive"):
        mac_storage.open_device_storage(wait=True)
    assert usb.opened == []


def test_several_devices_ask_for_device_uri_with_their_places(usb):
    usb.devices = [_usb(), _usb("4f42", 41)]
    with pytest.raises(found.SeveralDevices, match=r"mtp://\[usb:008,041\]/") as error:
        mac_storage.open_device_storage()
    assert len(error.value.devices) == 2
    st = mac_storage.open_device_storage("mtp://[usb:008,041]/")
    assert usb.opened == [(8, 41)] and st.device_uri == "mtp://[usb:008,041]/"


def test_an_address_of_another_form_says_the_form_it_takes(usb):
    with pytest.raises(DeviceError, match=r"mtp://\[usb:008,037\]/"):
        mac_storage.open_device_storage("mtp://Garmin_fenix_8_0000/")


def test_a_device_held_by_another_program_is_never_opened_and_the_program_is_named(usb):
    usb.holder = mac_usb.Holder("Garmin Express Service", 4242)
    with pytest.raises(mac_usb.DeviceInUse, match="Garmin Express Service is using the device"):
        mac_storage.open_device_storage(wait=True)
    assert usb.opened == []


def test_a_device_held_by_the_camera_service_or_this_process_is_opened(usb, monkeypatch):
    usb.holder = mac_usb.Holder("ptpcamerad", 4242)
    mac_storage.open_device_storage().close()
    usb.holder = mac_usb.Holder("python3.13", mac_usb.os.getpid())
    mac_storage.open_device_storage()
    assert usb.opened == [(8, 37), (8, 37)]


def test_a_device_is_opened_once_in_a_process(usb):
    first = mac_storage.open_device_storage()
    assert mac_storage.open_device_storage() is first
    assert usb.opened == [(8, 37)]
    first.close()
    assert mac_storage.open_device_storage() is not first  # closed: opened anew


def test_a_device_without_garmin_device_xml_is_closed_again(usb):
    usb.device = FakeDevice()
    usb.device.add("Music")
    with pytest.raises(found.NotThereYet, match="GarminDevice.xml not found"):
        mac_storage.open_device_storage()
    assert usb.device.closed and not usb.device.session_open


# --- a device with more than one storage (e.g. a Forerunner 645 Music) ----------------------

def test_every_storage_is_logged_and_the_others_are_kept_on_the_one_opened(usb, caplog):
    fake = _fake()
    fake.add_storage(0x00020002, "Media", 4_000_000_000, 1_000_000_000)
    usb.device = fake
    with caplog.at_level("INFO", logger="openexpress"):
        st = mac_storage.open_device_storage()
    assert st.other_storages == ("Media",)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("device: "))
    assert line.startswith("device: 2 storages: ")
    assert "Media (4.0 GB, 1.0 GB free)" in line
    assert "; GARMIN on Internal" in line
    # Never the device's serial number or unit ID.
    assert "MADEUPSERIAL" not in line and "3000000001" not in line


def _with_card(usb):
    """A handheld (a GPSMAP H1i Plus) with a memory card: its second storage, RemovableRAM."""
    fake = _fake()
    fake.add_storage(0x00020001, "Memory Card", 128_000_000_000, 127_000_000_000,
                     kind=ptp.STORAGE_REMOVABLE_RAM)
    usb.device = fake
    return fake


def test_a_memory_card_is_opened_beside_the_internal_storage_on_its_session(usb):
    _with_card(usb)
    st = mac_storage.open_device_storage()
    (card,) = st.others
    assert (card.description, card.removable, card.store) == ("Memory Card", True, 0x00020001)
    assert st.removable is False
    # One session, one lock: a transaction on the card never starts within one on internal.
    assert card._session is st._session and card._lock is st._lock
    assert card.free_space() == 127_000_000_000 and card.capacity() == 128_000_000_000
    assert card.listdir("") == []


def test_the_card_closes_with_the_device_and_alone_closes_nothing(usb):
    fake = _with_card(usb)
    st = mac_storage.open_device_storage()
    card = st.others[0]
    card.close()
    assert card.closed and not st.closed and fake.session_open  # the session is the device's
    assert st.exists("GARMIN/GarminDevice.xml")
    with pytest.raises(DeviceError, match="closed"):
        card.listdir("")
    st.close()
    assert not fake.session_open


def test_the_card_is_closed_when_the_device_is(usb):
    _with_card(usb)
    st = mac_storage.open_device_storage()
    card = st.others[0]
    st.close()
    assert card.closed
    with pytest.raises(DeviceError, match="closed"):
        card.listdir("")
    assert card.free_space() is None  # as for the device's own storage: not known


def test_a_device_that_stops_responding_on_its_card_stops_for_both(usb):
    fake = _with_card(usb)
    st = mac_storage.open_device_storage()
    card = st.others[0]
    card.refresh()
    fake.silent = True
    with pytest.raises(DeviceNotResponding):
        card.listdir("Garmin")
    assert card.stalled and st.stalled and fake.closed
    with pytest.raises(DeviceNotResponding):
        st.listdir("GARMIN")  # at once: nothing more is asked of the device


def test_a_refresh_drops_what_was_listed_on_the_card_too(usb):
    _with_card(usb)
    st = mac_storage.open_device_storage()
    card = st.others[0]
    card.listdir("")
    card.free_space()
    assert "" in card._folders and card._space_told is not None
    st.refresh()
    assert card._folders == {} and card._space_told is None


def test_a_card_taken_out_and_put_back_is_seen_at_the_next_refresh(usb):
    """The session stays open (the engine keeps it between requests): the device lists its
    storages anew, and a refresh asks it."""
    fake = _with_card(usb)
    st = mac_storage.open_device_storage()
    card = st.others[0]
    del fake.extra_storages[0x00020001]  # taken out
    st.refresh()
    assert st.others == () and st.other_storages == () and card.closed
    fake.add_storage(0x00020001, "Memory Card", 128_000_000_000, 127_000_000_000,
                     kind=ptp.STORAGE_REMOVABLE_RAM)  # put back
    st.refresh()
    (back,) = st.others
    assert (back.description, back.removable, back.closed) == ("Memory Card", True, False)
    assert back._lock is st._lock and st.other_storages == ("Memory Card",)
    assert back.free_space() == 127_000_000_000


def test_a_single_storage_device_has_no_other_storages(usb, caplog):
    with caplog.at_level("INFO", logger="openexpress"):
        st = mac_storage.open_device_storage()
    assert st.other_storages == ()
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("device: "))
    assert re.match(r"device: 1 storage: Internal \([0-9.]+ [kMG]B, [0-9.]+ [kMG]B free\); "
                    r"GARMIN on Internal", line)


def test_a_device_that_stopped_responding_is_left_out_a_while_then_tried_again(usb,
                                                                              monkeypatch):
    """Tried again at the same place: opening it cancels what it was left in (a fēnix 8 that
    reset its USB connection as it started came back so, and only so)."""
    now = [1000.0]
    monkeypatch.setattr(mac_storage, "_clock", lambda: now[0])
    usb.device = _fake()
    st = mac_storage.open_device_storage()
    usb.device.silent = True
    with pytest.raises(DeviceNotResponding):
        st.read("GARMIN/Activity/2026-11-17-09-30-00.fit")
    assert mac_storage.find_devices() == []
    now[0] += mac_storage.STALL_RETRY_S - 1
    assert mac_storage.find_devices() == []
    now[0] += 1
    assert len(mac_storage.find_devices()) == 1
    mac_storage.note_stalled(_usb().garmin_usb())
    usb.devices = []
    mac_storage.find_devices()  # unplugged: forgotten at once
    usb.devices = [_usb()]
    assert len(mac_storage.find_devices()) == 1


def test_probe_reads_garmin_device_xml_and_keeps_the_device_open(usb):
    (device,) = mac_storage.find_devices()
    summary = mac_storage.probe(device)
    assert summary.ok and summary.unit_id == 3000000001
    assert summary.storage is mac_storage.open_device_storage()


def test_probe_of_a_device_in_use_has_no_details(usb):
    usb.holder = mac_usb.Holder("Garmin Express Service", 4242)
    (device,) = mac_storage.find_devices()
    assert mac_storage.probe(device).reason == found.UNREADABLE


# --- the engine's polling and the device kept open between its requests -----------------------

def test_a_poll_tells_the_devices_and_the_other_programs_holding_them_from_one_look(
        usb, monkeypatch):
    asked = []
    devices = [_usb(), _usb("4f42", 41, holder=mac_usb.Holder("Garmin Express Service", 4242)),
               _usb("4f43", 42, holder=mac_usb.Holder("ptpcamerad", 77)),
               _usb("4f44", 43, holder=mac_usb.Holder("openExpress", mac_usb.os.getpid())),
               _usb("0003", 44, holder=mac_usb.Holder("Garmin Express Service", 4242))]
    monkeypatch.setattr(mac_usb, "garmin_devices",
                        lambda reuse=False: asked.append(reuse) or list(devices))
    found_there, held = mac_storage.poll()
    assert asked == [True]  # one look, the IORegistry's last one reused if it may be
    assert [d.uri for d in found_there] == [
        URI, "mtp://[usb:008,041]/", "mtp://[usb:008,042]/", "mtp://[usb:008,043]/"]
    assert held == {(8, 41): "Garmin Express Service"}  # not ours, not the camera service's


@pytest.fixture
def ioreg(monkeypatch):
    """The runs of `ioreg` (registered_devices) counted; what it tells is `ioreg.told`; the
    clock of the looks is `ioreg.now`."""
    state = types.SimpleNamespace(runs=0, told={}, now=100.0)

    def registered(read=None):
        state.runs += 1
        return dict(state.told)

    monkeypatch.setattr(mac_usb, "registered_devices", registered)
    monkeypatch.setattr(mac_usb, "_clock", lambda: state.now)
    return state


def test_the_ioregistry_is_read_again_only_when_the_garmin_devices_on_usb_change(ioreg):
    watch = [_usb()]
    for _n in range(3):
        mac_usb._registered(watch, reuse=True)
    assert ioreg.runs == 1
    ioreg.now += 3600
    mac_usb._registered(watch, reuse=True)
    assert ioreg.runs == 1  # nothing changed, nobody held it: no `ioreg` at all
    mac_usb._registered([_usb(address=38)], reuse=True)  # plugged in again: a new address
    assert ioreg.runs == 2
    mac_usb._registered([_usb(address=38), _usb("4f42", 41)], reuse=True)
    assert ioreg.runs == 3
    mac_usb._registered([_usb(address=38), _usb("4f42", 41)], reuse=False)  # not the poll's
    assert ioreg.runs == 4
    mac_usb.look_again()
    mac_usb._registered([_usb(address=38), _usb("4f42", 41)], reuse=True)
    assert ioreg.runs == 5


def test_a_device_another_program_holds_is_looked_at_again_now_and_then_to_see_it_let_go(
        ioreg):
    watch = [_usb()]
    ioreg.told = {(8, 37): mac_usb._Registered(holder=mac_usb.Holder("Garmin Express", 42))}
    mac_usb._registered(watch, reuse=True)
    ioreg.now += mac_usb.HELD_LOOK_S - 1
    mac_usb._registered(watch, reuse=True)
    assert ioreg.runs == 1
    ioreg.told = {}
    ioreg.now += 2
    assert mac_usb._registered(watch, reuse=True) == {}  # let go: seen
    ioreg.now += mac_usb.HELD_LOOK_S + 1
    mac_usb._registered(watch, reuse=True)
    assert ioreg.runs == 2
    # Held by this process itself: nothing to wait for.
    ioreg.told = {(8, 37): mac_usb._Registered(holder=mac_usb.Holder("x", mac_usb.os.getpid()))}
    mac_usb.look_again()
    mac_usb._registered(watch, reuse=True)
    ioreg.now += 3600
    mac_usb._registered(watch, reuse=True)
    assert ioreg.runs == 3


def test_the_name_of_a_holder_is_asked_of_ps_once_a_process(monkeypatch):
    import subprocess

    runs = []

    def ps(cmd, **_kw):
        runs.append(cmd[-1])
        out = "/Applications/Garmin Express.app/Contents/MacOS/Garmin Express Service"
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(mac_usb.subprocess, "run", ps)
    for _n in range(3):
        assert mac_usb.parse_holder("pid 4242, Garmin Express S").name == "Garmin Express Service"
    assert runs == ["comm=", "args="]
    mac_usb.parse_holder("pid 4243, Garmin Express S")
    assert len(runs) == 4  # another process: asked


def test_a_device_held_for_the_engine_stays_open_and_is_listed_anew_at_each_request(
        usb, monkeypatch, caplog):
    (device,) = mac_storage.find_devices()
    first = mac_storage.hold(device)
    assert usb.opened == [(8, 37)]
    first.read("GARMIN/GarminDevice.xml")
    listed = first._folders.copy()
    assert listed
    looks = []
    monkeypatch.setattr(mac_usb, "garmin_devices", lambda reuse=False: looks.append(1) or [])
    assert mac_storage.hold(device) is first
    assert first._folders == {}  # the device may have written files since
    assert usb.opened == [(8, 37)]
    # What the request opens by its address is that one, without a look at USB, and the log
    # does not say it was opened again.
    with caplog.at_level("INFO", logger="openexpress"):
        assert mac_storage.open_device_storage(URI) is first and looks == []
    assert not any("opened over" in r.getMessage() for r in caplog.records)
    mac_storage.close_found(device)
    assert first.closed
    monkeypatch.setattr(mac_usb, "garmin_devices", lambda reuse=False: list(usb.devices))
    assert mac_storage.hold(device) is not first  # let go: opened anew
    assert usb.opened == [(8, 37), (8, 37)]


def test_a_device_that_stopped_responding_is_not_held(usb):
    (device,) = mac_storage.find_devices()
    mac_storage.note_stalled(device.usb)
    assert mac_storage.hold(device) is None and usb.opened == []


@pytest.fixture
def handover(monkeypatch):
    """The holders the IORegistry tells one after the other (the last one stays); each wait
    recorded with whether the want lock was held then."""
    state = types.SimpleNamespace(holders=[], waits=[], now=0.0)

    def holder_of(bus, address):
        return state.holders.pop(0) if len(state.holders) > 1 else state.holders[0]

    def sleep(seconds):
        state.waits.append(mac_usb.wanted_elsewhere(8, 37))
        state.now += seconds

    monkeypatch.setattr(mac_usb, "holder_of", holder_of)
    monkeypatch.setattr(mac_usb, "_sleep", sleep)
    monkeypatch.setattr(mac_usb, "_clock", lambda: state.now)
    return state


def test_another_openexpress_holding_the_device_is_waited_for_until_it_lets_go(handover):
    ours = mac_usb.Holder("openExpress", 4242)
    handover.holders = [ours, ours, ours, None]
    mac_usb.check_free(8, 37)
    assert handover.waits == [True, True, True]  # it saw that one waits for the device
    assert mac_usb.wanted_elsewhere(8, 37) is False  # nobody waits any more


def test_another_openexpress_that_keeps_the_device_is_said_after_the_wait(handover):
    handover.holders = [mac_usb.Holder("openExpress", 4242)]
    with pytest.raises(mac_usb.DeviceInUse, match="openExpress is using the device"):
        mac_usb.check_free(8, 37)
    assert len(handover.waits) == mac_usb.HANDOVER_WAIT_S / mac_usb.HANDOVER_EVERY_S


def test_another_program_holding_the_device_is_not_waited_for(handover):
    handover.holders = [mac_usb.Holder("Garmin Express Service", 4242)]
    with pytest.raises(mac_usb.DeviceInUse):
        mac_usb.check_free(8, 37)
    assert handover.waits == []


# --- platform.mac_usb: the IORegistry, the claim, ptpcamerad ---------------------------------

def _ioreg(*devices) -> bytes:
    hub = {"IOObjectClass": "IOUSBHostDevice", "idVendor": 0x05AC, "USB Address": 1,
           "locationID": 0x08300000, "IORegistryEntryChildren": list(devices)}
    return plistlib.dumps([hub])


def test_the_holder_and_the_serial_are_read_from_the_ioregistry_by_the_place_libusb_gives(
        monkeypatch):
    monkeypatch.setattr(mac_usb, "_program_name", lambda pid, short: short + "ervice")
    watch = {"idVendor": 0x091E, "idProduct": 0x51B8, "USB Address": 37,
             "locationID": 0x08310000, "USB Serial Number": "000012345678",
             "IORegistryEntryChildren": [
                 {"IORegistryEntryName": "MTP", "bInterfaceClass": 255,
                  "UsbExclusiveOwner": "pid 4242, Garmin Express S"}]}
    other = {"idVendor": 0x091E, "USB Address": 2, "locationID": 0x01100000,
             "USB Product Name": "Edge 1040"}
    found_there = REGISTERED_DEVICES(_ioreg(watch, other))
    assert found_there[(8, 37)].holder == mac_usb.Holder("Garmin Express Service", 4242)
    assert found_there[(8, 37)].serial == "000012345678"
    assert found_there[(1, 2)].name == "Edge 1040" and found_there[(1, 2)].holder is None
    assert REGISTERED_DEVICES(b"not a plist") == {}


def test_a_garmin_device_on_usb_hides_its_serial_in_the_log(monkeypatch):
    hidden = []
    monkeypatch.setattr(mac_usb.applog, "hide_usb", hidden.append)
    usb = _usb().garmin_usb()
    assert hidden == [usb] and usb.serial == "000012345678"
    assert "000012345678" not in repr(usb)


@pytest.mark.parametrize("product, classes, mode", [
    ("0003", ("ff",), found.STARTING),
    ("51b8", ("ff",), found.PRESENT),
    ("51b8", ("06",), found.PRESENT),
    ("2b4a", ("08",), found.MASS_STORAGE),
    ("0b89", ("03", "08"), found.UNSUPPORTED),
    ("51b8", (), found.PRESENT),
])
def test_the_mode_of_a_device_by_its_product_and_its_interfaces(product, classes, mode):
    assert _usb(product, classes=classes).mode == mode


def test_the_looks_at_usb_share_one_libusb_context_that_is_never_exited(monkeypatch):
    """On macOS the libusb_exit of the last context stops libusb's thread of USB events, and
    deadlocks when a device comes or goes then: the looks keep one context for good."""
    calls = []

    class Lib:
        def libusb_init(self, ref):
            calls.append("init")
            return 0

        def libusb_get_device_list(self, context, ref):
            calls.append("list")
            return 0

        def __getattr__(self, name):
            return lambda *args: calls.append(name) or 0

    monkeypatch.setattr(mac_usb, "_libusb", Lib)
    monkeypatch.setattr(mac_usb, "_context", None)
    assert GARMIN_DEVICES() == [] and GARMIN_DEVICES() == []
    assert calls.count("init") == 1 and "libusb_exit" not in calls
    assert calls.count("list") == 2


class ClaimingLibusb:
    """libusb as MacTransport claims an interface with it: each claim answers the next of
    `answers` (0: claimed, -3: LIBUSB_ERROR_ACCESS); the rest of libusb records its calls."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def libusb_claim_interface(self, handle, interface):
        self.calls.append("claim")
        return self.answers.pop(0)

    def libusb_error_name(self, code):
        return {-3: b"LIBUSB_ERROR_ACCESS", -4: b"LIBUSB_ERROR_NO_DEVICE"}[code]

    def __getattr__(self, name):
        return lambda *args: self.calls.append(name) or 0


@pytest.fixture
def daemon(monkeypatch):
    """The signals sent to ptpcamerad ("STOP", "CONT"), recorded, none sent; no wait between
    claims."""
    sent = []
    monkeypatch.setattr(mac_usb, "_signal_daemon", lambda name: sent.append(name) or True)
    monkeypatch.setattr(mac_usb, "_sleep", lambda seconds: sent.append(seconds))
    monkeypatch.setattr(mac_usb, "_paused", 0)
    monkeypatch.setattr(mac_usb, "_hooks", True)  # no handlers set by a test
    return sent


def test_ptpcamerad_is_paused_before_the_claim_and_goes_on_when_the_transport_closes(daemon):
    lib = ClaimingLibusb(0)
    mac_usb.MacTransport._claim_interface(lib, 99, 0)
    assert daemon == ["STOP"] and lib.calls == ["claim"]
    transport = mac_usb.MacTransport(lib, 7, 99, 0, 1, 0x81, 512)
    transport.close()
    transport.close()  # once only
    assert daemon == ["STOP", "CONT"]


def test_a_claim_refused_while_the_device_is_let_go_is_tried_again(daemon):
    lib = ClaimingLibusb(-3, -3, 0)
    mac_usb.MacTransport._claim_interface(lib, 99, 0)
    assert daemon == ["STOP", 0.25, 0.5] and lib.calls == ["claim"] * 3


def test_a_claim_refused_for_good_lets_ptpcamerad_go_on(daemon):
    lib = ClaimingLibusb(*[-3] * 5)
    with pytest.raises(ptp.UsbUnavailable, match="LIBUSB_ERROR_ACCESS") as error:
        mac_usb.MacTransport._claim_interface(lib, 99, 0)
    assert error.value.code == -3
    assert daemon == ["STOP", *mac_usb.CLAIM_WAITS_S, "CONT"]


def test_another_refusal_of_the_claim_is_not_tried_again(daemon):
    with pytest.raises(ptp.UsbUnavailable, match="NO_DEVICE"):
        mac_usb.MacTransport._claim_interface(ClaimingLibusb(-4), 99, 0)
    assert daemon == ["STOP", "CONT"]


def test_a_claim_refused_for_good_names_the_program_that_holds_the_device(monkeypatch):
    holders = iter([None, mac_usb.Holder("Image Capture Extension", 77)])
    monkeypatch.setattr(mac_usb, "holder_of", lambda bus, address: next(holders))
    refused = ptp.UsbUnavailable("busy")
    refused.code = -3

    def open_refused(cls, bus, address):
        raise refused

    monkeypatch.setattr(mac_usb.MacTransport, "open", classmethod(open_refused))
    with pytest.raises(mac_usb.DeviceInUse, match="Image Capture Extension"):
        mac_usb.open_transport(8, 37)


def test_ptpcamerad_paused_twice_goes_on_after_the_second_close(daemon):
    mac_usb.pause_camera_daemon()
    mac_usb.pause_camera_daemon()
    mac_usb.resume_camera_daemon()
    assert daemon == ["STOP"]
    mac_usb.resume_camera_daemon()
    mac_usb.resume_camera_daemon()
    assert daemon == ["STOP", "CONT"]


def test_ptpcamerad_goes_on_at_a_signal_that_ends_the_program(daemon):
    mac_usb.pause_camera_daemon()
    with pytest.raises(SystemExit) as ended:
        mac_usb._on_signal(signal.SIGTERM, None, signal.SIG_DFL)
    assert ended.value.code == 128 + signal.SIGTERM
    assert daemon == ["STOP", "CONT"]
    told = []
    mac_usb.pause_camera_daemon()
    mac_usb._on_signal(signal.SIGHUP, None, lambda signum, frame: told.append(signum))
    assert told == [signal.SIGHUP] and daemon == ["STOP", "CONT", "STOP", "CONT"]


def test_ptpcamerad_goes_on_at_exit_whatever_still_has_it_paused(daemon):
    mac_usb.pause_camera_daemon()
    mac_usb.pause_camera_daemon()
    mac_usb.resume_camera_daemon_now()
    mac_usb.resume_camera_daemon_now()
    assert daemon == ["STOP", "CONT"]


def test_ptpcamerad_goes_on_only_once_no_process_of_ours_has_it_paused(daemon, tmp_path):
    """Another process holds the lock file, shared, as one that paused it does: this one lets
    go of it without waking ptpcamerad; then, with nobody holding it, it is woken."""
    import fcntl
    import os

    other = os.open(mac_usb.pause_lock_path(), os.O_RDWR | os.O_CREAT)
    fcntl.flock(other, fcntl.LOCK_SH)  # as another process holds it (flock: per descriptor)
    mac_usb.pause_camera_daemon()
    mac_usb.resume_camera_daemon()
    assert daemon == ["STOP"]
    os.close(other)
    mac_usb.pause_camera_daemon()
    mac_usb.resume_camera_daemon()
    assert daemon == ["STOP", "STOP", "CONT"]


def test_ptpcamerad_left_stopped_by_a_killed_run_is_woken_at_the_start(daemon, monkeypatch):
    import fcntl
    import os

    monkeypatch.setattr(mac_usb, "daemon_stopped", lambda: True)
    other = os.open(mac_usb.pause_lock_path(), os.O_RDWR | os.O_CREAT)
    fcntl.flock(other, fcntl.LOCK_SH)  # a run of ours that has it paused
    assert mac_usb.wake_camera_daemon_left_stopped() is False
    os.close(other)  # killed: the system lets go of its lock
    assert mac_usb.wake_camera_daemon_left_stopped() is True
    assert daemon == ["CONT"]
    monkeypatch.setattr(mac_usb, "daemon_stopped", lambda: False)
    assert mac_usb.wake_camera_daemon_left_stopped() is False  # running: nothing sent
    assert daemon == ["CONT"]


def test_a_stopped_ptpcamerad_is_told_by_its_state(monkeypatch):
    answers = {"pgrep": "20637\n", "ps": "T\n"}

    def run(command, **_kw):
        return types.SimpleNamespace(stdout=answers[command[0].rsplit("/", 1)[-1]])

    monkeypatch.setattr(mac_usb.subprocess, "run", run)
    assert DAEMON_STOPPED() is True
    answers["ps"] = "Ss\n"
    assert DAEMON_STOPPED() is False
    answers["pgrep"] = ""
    assert DAEMON_STOPPED() is False


@pytest.mark.parametrize("comm, args, expected", [
    (("/opt/homebrew/Cellar/python@3.13/3.13.18/Frameworks/Python.framework/Versions/3.13/"
     "Resources/Python.app/Contents/MacOS/Python"),
     "/opt/homebrew/…/Python /Users/u/.venvs/openexpress-mac/bin/openexpress engine", "openExpress"),
    ("/usr/bin/python3", "/usr/bin/python3 -m openexpress info", "openExpress"),
    (("/Applications/Garmin Express.app/Contents/Library/LoginItems/Garmin Express Service.app/"
     "Contents/MacOS/Garmin Express Service"),
     "/Applications/Garmin Express.app/…/Garmin Express Service -fromClient true",
     "Garmin Express Service"),
])
def test_a_device_held_by_another_openexpress_names_openexpress_not_python(monkeypatch, comm,
                                                                           args, expected):
    import subprocess

    def ps(cmd, **_kw):
        out = comm if cmd[-1] == "comm=" else args
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    monkeypatch.setattr(mac_usb.subprocess, "run", ps)
    short = "Python" if expected == "openExpress" else "Garmin Express S"
    assert mac_usb._program_name(4242, short) == expected

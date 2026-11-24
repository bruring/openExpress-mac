"""Maps on a device's memory card (docs/design/maps-on-memory-card.md), the read side: the card
is found among the device's storages (by the name GarminDevice.xml gives it, else as the only
one the device says can be taken out), a map's files are looked for on both, and where its own
.img files are is its place. A map on the card is never taken for removed and its version is
read there; one on both storages is neither installed, updated nor removed. Installing on the
card is tested in test_maps_card_install.py.

The device is a GPSMAP H1i Plus as its fixtures give it (tests/data/gpsmap_h1i_plus/), its two
storages folders: TopoActive Taiwan as Garmin Express put it on the card (its .img files and a
copy of the shared DA69900.sid there, its unlock and search files on internal), TopoActive
Canada and US West on internal, sharing the North America terrain layer."""

import json
from pathlib import Path

import pytest

from openexpress import engine
from openexpress import maps as mp
from openexpress.device import card as cards
from openexpress.device import xmldevice
from openexpress.device.storage import LocalStorage

DATA = Path(__file__).resolve().parent / "data" / "gpsmap_h1i_plus"
DEVICE = xmldevice.parse((DATA / "GarminDevice.xml").read_bytes())
ANSWER = json.loads((DATA / "universal_maps.json").read_text(encoding="utf-8"))

INTERNAL_FILES = [
    # TopoActive Canada and US West, and the North America terrain they share
    "GARMIN/D9953060A.img", "GARMIN/D9953060A.gma", "GARMIN/DB961020A.img",
    "GARMIN/DB961020A.gma", "GARMIN/D4712100A.img", "GARMIN/SQL/D7729090A.db",
    "GARMIN/SID/D9958050A.sid", "GARMIN/SID/D9959050A.sid", "GARMIN/SID/DB966010A.sid",
    "GARMIN/SID/DB967010A.sid", "GARMIN/SID/DA69900.sid",
    # TopoActive Taiwan's unlock and search files: on internal, wherever the map is
    "GARMIN/D9213050A.gma", "GARMIN/SID/DA669020B.sid", "GARMIN/SID/DC290000A.sid",
]
CARD_FILES = ["Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"]
INSTALLED = ("TopoActive Canada", "TopoActive United States West", "TopoActive Taiwan")


def _folder(root: Path, files: list[str], description: str, removable: bool) -> LocalStorage:
    root.mkdir(parents=True, exist_ok=True)
    for path in files:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(b"x")
    st = LocalStorage(root)
    st.description, st.removable = description, removable
    return st


@pytest.fixture
def handheld(tmp_path):
    """The internal storage (its card among its `others`) and the card."""
    internal = _folder(tmp_path / "internal", INTERNAL_FILES, "Internal Storage", False)
    card = _folder(tmp_path / "card", CARD_FILES, "Memory Card", True)
    internal.others = (card,)
    return internal, card


def _maps(installed=INSTALLED) -> list[mp.UniversalMap]:
    found, _listed = mp.read_maps(ANSWER)
    for m in found:  # as Garmin's server answers once the device took them in
        m.state = 1 if m.name in installed else 0
    return found


def _by_name(maps) -> dict[str, mp.UniversalMap]:
    return {m.name: m for m in maps}


# --- which storage is the card, where a file is on it ----------------------------------------

def test_the_card_is_the_storage_named_as_the_device_names_its_card(handheld, tmp_path):
    internal, card = handheld
    other = _folder(tmp_path / "other", [], "Media", True)
    internal.others = (other, card)
    assert cards.card_of(internal, DEVICE) is card


def test_without_the_device_file_the_only_removable_storage_is_the_card(handheld, tmp_path):
    internal, card = handheld
    assert cards.card_of(internal) is card
    internal.others = (card, _folder(tmp_path / "other", [], "Card 2", True))
    assert cards.card_of(internal) is None  # two: not told apart
    internal.others = (_folder(tmp_path / "media", [], "Media", False),)
    assert cards.card_of(internal) is None


def test_a_device_with_one_storage_has_no_card(tmp_path):
    assert cards.card_of(LocalStorage(tmp_path), DEVICE) is None


@pytest.mark.parametrize("path, on_card", [
    ("Garmin/D9213050A.img", ["Garmin/D9213050A.img"]),
    ("Garmin/SID/DA69900.sid", ["Garmin/SID/DA69900.sid"]),
    ("Garmin/SQL/D7729090A.db", ["Garmin/SQL/D7729090A.db"]),
    ("Garmin/D9213050A.gma", []),  # an unlock: no data type of it may be on the card
    ("Garmin/GPX/Track.gpx", []),
])
def test_a_file_is_looked_for_on_the_card_where_its_data_type_goes(path, on_card):
    assert cards.card_paths(path, DEVICE) == on_card


def test_the_device_file_names_the_card_and_the_folders_of_maps_on_it():
    (location,) = DEVICE.storage_locations
    assert (location.name, location.mtp_id, location.ums_id, location.supported_format) == (
        "Map", "Memory Card", "GARMIN CARD", "FAT32")
    assert (location.min_size, location.max_size) == (4_000_000_000, 1_099_511_627_776)
    paths = {f.datatype: f.external_path for f in DEVICE.files if f.external_path}
    assert paths == {"PreProgrammedNamedMaps": "Garmin", "NMaps": "Garmin",
                     "TopoActiveDEM": "Garmin", "NamedTrails": "Garmin/SQL",
                     "Etyp": "Garmin/SID"}


# --- where each map is -----------------------------------------------------------------------

def test_each_map_is_where_its_own_images_are(handheld):
    internal, _card = handheld
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    by = _by_name(maps)
    assert by["TopoActive Taiwan"].location == cards.CARD
    assert by["TopoActive Canada"].location == by["TopoActive United States West"].location == (
        cards.INTERNAL)
    assert by["TopoActive Japan"].location is None  # not on the device
    assert all(m.waiting is None for m in maps)  # nothing taken for written or removed


def test_a_map_only_on_the_card_is_not_taken_for_removed(handheld):
    """Greenland is one .img and nothing else: on the card, none of its files is on internal.
    Without the card it would read as removed, waiting for the device to be unplugged."""
    internal, card = handheld
    (Path(card.root) / "Garmin/DA257000A.img").write_bytes(b"x")
    maps = _maps((*INSTALLED, "TopoActive Greenland"))
    mp.mark_waiting(internal, maps, DEVICE)
    greenland = _by_name(maps)["TopoActive Greenland"]
    assert (greenland.location, greenland.waiting) == (cards.CARD, None)
    internal.others = ()  # the card taken out: Greenland is presumably on it
    mp.mark_waiting(internal, maps, DEVICE)
    assert (greenland.location, greenland.waiting) == (cards.CARD_OUT, None)
    mp.mark_waiting(internal, maps)  # a device that declares no card: none of its files is there
    assert greenland.waiting == mp.WAIT_REMOVE


def test_a_map_whose_images_are_on_both_storages_is_on_both(handheld):
    internal, _card = handheld
    (Path(internal.root) / "GARMIN/D9213050A.img").write_bytes(b"x")
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    assert _by_name(maps)["TopoActive Taiwan"].location == cards.BOTH


def test_a_shared_terrain_layer_does_not_decide_where_a_map_is(handheld):
    """Canada's own .img on the card, the North America terrain it shares with US West still on
    internal: Canada is on the card."""
    internal, card = handheld
    (Path(internal.root) / "GARMIN/D9953060A.img").unlink()
    (Path(card.root) / "Garmin/D9953060A.img").write_bytes(b"x")
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    by = _by_name(maps)
    assert by["TopoActive Canada"].location == cards.CARD
    assert by["TopoActive United States West"].location == cards.INTERNAL


def test_a_card_that_cannot_be_read_takes_no_map_for_removed(handheld, monkeypatch, caplog):
    internal, card = handheld
    (Path(card.root) / "Garmin/DA257000A.img").write_bytes(b"x")

    def refused(path):
        raise OSError("the card would not be read")

    monkeypatch.setattr(card, "listdir", refused)
    maps = _maps((*INSTALLED, "TopoActive Greenland"))
    with caplog.at_level("WARNING", logger="openexpress"):
        mp.mark_waiting(internal, maps, DEVICE)
    by = _by_name(maps)
    assert by["TopoActive Greenland"].waiting is None
    assert by["TopoActive Greenland"].location == by["TopoActive Taiwan"].location == (
        cards.CARD_OUT)
    assert by["TopoActive Canada"].location == cards.INTERNAL
    assert "cannot read the map files on the memory card" in caplog.text


# --- what may be changed ---------------------------------------------------------------------

def test_a_map_on_the_card_may_be_updated_and_removed_there(handheld):
    internal, _card = handheld
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    taiwan = _by_name(maps)["TopoActive Taiwan"]
    assert mp.can_install(taiwan) and mp.removal_refused(taiwan) is None
    assert mp.install_target(taiwan) == cards.CARD  # an update stays where the map is


def test_a_map_on_both_storages_is_neither_installed_updated_nor_removed(handheld):
    internal, card = handheld
    (Path(internal.root) / "GARMIN/D9213050A.img").write_bytes(b"x")
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    taiwan = _by_name(maps)["TopoActive Taiwan"]
    assert not mp.can_install(taiwan)
    assert mp.cannot_install_message(taiwan) == mp.removal_refused(taiwan) == (
        "Map “TopoActive Taiwan” is on the device and on its memory card: openExpress does not "
        "change it until one of the two is removed, e.g. in Garmin Express.")
    assert "memory card" in mp.cannot_install_reason(taiwan)
    with pytest.raises(mp.MapError, match="on the device and on its memory card"):
        mp.uninstall(taiwan, maps, internal, device=DEVICE)
    assert all((Path(internal.root) / p).exists() for p in INTERNAL_FILES)  # nothing removed
    assert all((Path(card.root) / p).exists() for p in CARD_FILES)


def test_a_map_on_internal_storage_may_be_installed_and_removed(handheld):
    internal, _card = handheld
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    canada = _by_name(maps)["TopoActive Canada"]
    assert mp.removal_refused(canada) is None
    assert mp.can_install(_by_name(maps)["TopoActive Japan"])


# --- what the engine says of it --------------------------------------------------------------

def test_the_engine_names_both_storages(handheld):
    internal, _card = handheld
    storages = engine.map_storages(internal, DEVICE)
    assert [(s["id"], s["name"], s["removable"]) for s in storages] == [
        ("internal", "Internal Storage", False), ("card", "Memory Card", True)]
    # A device without a card: its own storage alone.
    alone = engine.map_storages(LocalStorage(Path(internal.root)), DEVICE)
    assert [(s["id"], s["name"], s["removable"]) for s in alone] == [("internal", None, False)]


def test_the_version_on_the_device_is_read_on_the_card(handheld, monkeypatch):
    internal, card = handheld
    read = []

    def header(storage):
        def read_start(path, size):
            read.append((storage, path))
            return b""
        return read_start

    monkeypatch.setattr(internal, "read_start", header("internal"))
    monkeypatch.setattr(card, "read_start", header("card"))
    monkeypatch.setattr("openexpress.ownmaps.read_title",
                        lambda data: "TopoActive Taiwan 2026.10")
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    assert engine.installed_map_version(internal, _by_name(maps)["TopoActive Taiwan"],
                                        DEVICE) == "2026.10"
    assert read == [("card", "Garmin/D9213050A.img")]


# --- the card taken out (e.g. to be written in the Mac's card reader) ---------------------------

def test_maps_on_a_card_taken_out_are_kept_and_not_changed(handheld):
    """The device declares a card and has none: Taiwan, its .img files not on internal storage,
    is on the card that is not there; nothing is installed, updated, moved or removed for it."""
    internal, _card = handheld
    internal.others = ()
    maps = _maps()
    mp.mark_waiting(internal, maps, DEVICE)
    by = _by_name(maps)
    taiwan = by["TopoActive Taiwan"]
    assert (taiwan.location, taiwan.waiting) == (cards.CARD_OUT, None)
    assert not mp.can_install(taiwan)
    message = ("Map “TopoActive Taiwan” is on the memory card, which is not in the device or "
               "cannot be read. Put the card back, then try again.")
    assert mp.cannot_install_message(taiwan) == mp.removal_refused(taiwan) == message
    assert mp.move_refused(taiwan) == message
    assert mp.cannot_install_reason(taiwan) == "On the memory card, which is not in the device"
    with pytest.raises(mp.MapError, match="not in the device"):
        mp.install_target(taiwan)
    with pytest.raises(mp.MapError, match="not in the device"):
        mp.uninstall(taiwan, maps, internal, device=DEVICE)
    assert all((Path(internal.root) / p).exists() for p in INTERNAL_FILES)  # nothing removed
    # The maps on internal storage, and those not on the device, as ever.
    assert by["TopoActive Canada"].location == cards.INTERNAL
    assert mp.removal_refused(by["TopoActive Canada"]) is None
    assert by["TopoActive Japan"].location is None and mp.can_install(by["TopoActive Japan"])


def test_a_card_taken_out_gives_the_engine_no_card(handheld):
    internal, _card = handheld
    internal.others = ()
    assert [s["id"] for s in engine.map_storages(internal, DEVICE)] == ["internal"]
    assert cards.declares_card(DEVICE) and not cards.declares_card(None)

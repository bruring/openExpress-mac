"""The device's memory card in the computer's card reader (docs/design/maps-on-memory-card.md,
its card reader): what tells the card apart is kept whenever it is seen over MTP (its capacity, the map
files on it); a mounted volume is the card only when it matches; then it stands in for the card
of an install or a move, the unlock and search files still going to internal storage over USB,
and it is ejected afterwards. A volume that does not match is never written.

The device is a GPSMAP H1i Plus by its GarminDevice.xml, given a unit ID; the maps are those of
test_maps_move, made up and small; the volumes are folders."""

import copy
import types
from pathlib import Path

import pytest
from test_maps_card_install import CONTENT, _has, _put, _storage
from test_maps_move import _answer

from openexpress import cli, engine
from openexpress import maps as mp
from openexpress.device import card as cards
from openexpress.device import xmldevice
from openexpress.device.storage import DeviceError, LocalStorage

DATA = Path(__file__).resolve().parent / "data" / "gpsmap_h1i_plus"
DEVICE = copy.copy(xmldevice.parse((DATA / "GarminDevice.xml").read_bytes()))
DEVICE.unit_id = 3_000_000_001


@pytest.fixture
def handheld(tmp_path, xdg):
    """Canada on internal storage, Taiwan on the card (a folder named as a mounted volume)."""
    internal = _storage(tmp_path / "internal", "Internal Storage", False)
    card = _storage(tmp_path / "Volumes" / "MAPCARD", "Memory Card", True)
    internal.others = (card,)
    for path in ("GARMIN/D9953060A.img", "GARMIN/D4712100A.img", "GARMIN/SID/DA69900.sid",
                 "GARMIN/D9953060A.gma", "GARMIN/SID/DA669020B.sid", "GARMIN/D9213050A.gma"):
        _put(internal, path)
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        _put(card, path)
    return internal, card


def _maps(internal):
    found, _n = mp.read_maps(_answer())
    mp.mark_waiting(internal, found, DEVICE)
    return found, {m.name: m for m in found}


def _seen_then_taken_out(internal, card):
    """The card seen over MTP (remembered), then taken out of the device."""
    _maps(internal)
    internal.others = ()
    return Path(card.root)


# --- remembered over MTP, recognised in the reader --------------------------------------------

def test_the_card_is_remembered_when_it_is_seen_over_mtp(handheld):
    internal, card = handheld
    assert cards.remembered(DEVICE.unit_id) is None
    _maps(internal)
    record = cards.remembered(DEVICE.unit_id)
    assert record["capacity"] == card.capacity() and record["name"] == "Memory Card"
    assert record["files"] == {"Garmin/D9213050A.img": len(CONTENT["Garmin/D9213050A.img"]),
                               "Garmin/D9221010A.img": len(CONTENT["Garmin/D9221010A.img"])}


def test_the_card_in_the_reader_is_told_apart(handheld):
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    assert cards.not_the_card(DEVICE.unit_id, volume) is None
    found = cards.reader_volumes(DEVICE.unit_id, volume.parent)
    assert found == [(volume, "")]


@pytest.mark.parametrize("change, reason", [
    ("never seen", "has not seen this device's memory card yet"),
    ("another size", "not that of the device's memory card"),
    ("a map changed", "not those the device last had on its card"),
    ("no Garmin folder", "no Garmin folder"),
])
def test_a_volume_that_is_not_the_card_is_never_used(handheld, change, reason):
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    if change == "never seen":
        cards._card_file(DEVICE.unit_id).unlink()
    elif change == "another size":
        record = cards._card_file(DEVICE.unit_id)
        record.write_text(record.read_text().replace(
            f'"capacity": {card.capacity()}', f'"capacity": {card.capacity() + 10**9}'))
    elif change == "a map changed":
        (volume / "Garmin/D9213050A.img").write_bytes(b"another map")
    else:
        (volume / "Garmin").rename(volume / "Other")
    assert reason in cards.not_the_card(DEVICE.unit_id, volume)
    with pytest.raises(DeviceError, match="is not this device's memory card"):
        cards.reader_card(DEVICE, volume)


def test_a_card_in_the_reader_is_not_remembered_again(handheld):
    """Its size in the reader is not its size over MTP: only MTP tells what the card is."""
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    before = cards._card_file(DEVICE.unit_id).read_text()
    internal.card_volume = cards.reader_card(DEVICE, volume)
    _maps(internal)
    assert cards._card_file(DEVICE.unit_id).read_text() == before


# --- writing through the reader ------------------------------------------------------------------

@pytest.fixture
def no_download(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("nothing is downloaded")
    monkeypatch.setattr(mp, "_download", refused)


def test_a_map_moves_to_the_card_in_the_reader(handheld, no_download):
    """Taiwan to internal over USB, then back to the card in the reader: the card's map files
    on the volume, its unlock and search files on internal storage as ever."""
    internal, card = handheld
    found, by = _maps(internal)
    mp.move(by["TopoActive Taiwan"], found, internal, DEVICE, cards.INTERNAL)
    volume = _seen_then_taken_out(internal, card)
    internal.card_volume = cards.reader_card(DEVICE, volume)
    found, by = _maps(internal)
    assert by["TopoActive Taiwan"].location == cards.INTERNAL
    rep = mp.move(by["TopoActive Taiwan"], found, internal, DEVICE, cards.CARD)
    # Its two .img files, and the ETYP file, whose card copy went when Taiwan left the card.
    assert len(rep.copied) == 3 and (volume / "Garmin/SID/DA69900.sid").exists()
    assert (volume / "Garmin/D9213050A.img").read_bytes() == CONTENT["Garmin/D9213050A.img"]
    assert not _has(internal, "Garmin/D9213050A.img")
    assert _has(internal, "Garmin/SID/DA669020B.sid") and _has(internal, "Garmin/D9213050A.gma")
    found, by = _maps(internal)
    assert by["TopoActive Taiwan"].location == cards.CARD


def test_without_the_reader_a_card_taken_out_is_out(handheld):
    internal, card = handheld
    _seen_then_taken_out(internal, card)
    _found, by = _maps(internal)
    assert by["TopoActive Taiwan"].location == cards.CARD_OUT


@pytest.mark.parametrize("platform, command", [("darwin", ["diskutil", "eject"]),
                                               ("linux", ["gio", "mount", "--eject"])])
def test_the_card_is_ejected_afterwards(handheld, monkeypatch, platform, command):
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    reader = cards.reader_card(DEVICE, volume)
    ran = []
    monkeypatch.setattr(cards.sys, "platform", platform)
    monkeypatch.setattr(cards.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or types.SimpleNamespace(returncode=0))
    assert cards.eject(reader) and ran == [command + [str(volume)]]
    assert not cards.eject(internal)  # not a card in the reader: nothing ejected


def test_cards_in_the_reader_are_looked_for_where_the_system_mounts_them(monkeypatch):
    monkeypatch.setattr(cards.sys, "platform", "darwin")
    assert cards.mount_roots() == [Path("/Volumes")]
    monkeypatch.setattr(cards.sys, "platform", "linux")
    monkeypatch.setenv("USER", "someone")
    assert cards.mount_roots() == [Path("/run/media/someone"), Path("/media/someone")]


# --- the command line and the engine --------------------------------------------------------------

def test_the_command_line_takes_the_card_in_the_reader():
    args = cli.build_parser().parse_args(["maps", "move", "Taiwan", "--to", "card",
                                          "--card-reader", "/Volumes/MAPCARD"])
    assert args.card_reader == "/Volumes/MAPCARD"
    args = cli.build_parser().parse_args(["maps", "install", "--to", "card", "--card-reader",
                                          "/Volumes/MAPCARD", "Japan"])
    assert args.card_reader == "/Volumes/MAPCARD"


def test_the_engine_answers_card_reader_for_a_device(handheld, monkeypatch):
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    real = cards.reader_volumes
    monkeypatch.setattr(cards, "reader_volumes", lambda unit_id: real(unit_id, volume.parent))
    eng = engine.Engine(__import__("io").StringIO())
    req = engine.Request(1, "maps.card_reader", {"device": str(DEVICE.unit_id)})
    assert eng.maps_card_reader(req) == {
        "volumes": [{"path": str(volume), "name": "MAPCARD", "is_card": True,
                     "reason": None}],
        "remembered": True}
    with pytest.raises(engine.EngineError):
        eng.maps_card_reader(engine.Request(2, "maps.card_reader", {"device": "card"}))


# --- what the reader leaves -----------------------------------------------------------------------

def test_no_macos_dot_underscore_file_is_left_beside_a_file_written(handheld, monkeypatch, tmp_path):
    """On an exFAT card macOS keeps a file's attributes in "._<name>" beside it, which the device
    might take for a map: each one is removed as soon as its file is written."""
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    reader = cards.reader_card(DEVICE, volume)
    real = LocalStorage._write_file

    def as_on_exfat(self, real_path, local, progress=None):
        real(self, real_path, local, progress)
        target = self._p(real_path)
        target.with_name("._" + target.name).write_bytes(b"attributes")

    monkeypatch.setattr(LocalStorage, "_write_file", as_on_exfat)
    local = tmp_path / "map.img"
    local.write_bytes(b"a map")
    reader.write_file_checked("Garmin/DB961020A.img", local)
    assert (volume / "Garmin/DB961020A.img").exists()
    assert not (volume / "Garmin/._DB961020A.img").exists()


def test_the_card_is_swept_of_dot_underscore_files_before_it_is_ejected(handheld, monkeypatch):
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    reader = cards.reader_card(DEVICE, volume)
    (volume / "Garmin/._D9213050A.img").write_bytes(b"attributes")  # beside a map: removed
    (volume / "Garmin/._alone").write_bytes(b"something")  # beside nothing: left
    monkeypatch.setattr(cards.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=0))
    assert cards.eject(reader)
    assert not (volume / "Garmin/._D9213050A.img").exists()
    assert (volume / "Garmin/._alone").exists()


def test_a_command_never_keeps_the_card_volume_of_one_before(handheld, monkeypatch):
    """The engine keeps the device's session open between requests: the card in the reader of
    one request (ejected since) is not the card of the next."""
    internal, card = handheld
    volume = _seen_then_taken_out(internal, card)
    internal.card_volume = cards.reader_card(DEVICE, volume)  # left by a request before
    monkeypatch.setattr(cli, "open_device", lambda args: (internal, DEVICE))
    client = types.SimpleNamespace(universal_maps=lambda raw: _answer())
    args = cli.build_parser().parse_args(["--path", "/unused", "maps"])
    st, _dev, found, _n = cli._load_maps(args, client)
    assert st.card_volume is None
    assert {m.name: m for m in found}["TopoActive Taiwan"].location == cards.CARD_OUT
    args = cli.build_parser().parse_args(["--path", "/unused", "maps", "move", "Taiwan", "--to",
                                          "internal", "--card-reader", str(volume)])
    st, _dev, found, _n = cli._load_maps(args, client)
    assert st.card_volume is not None and st.card_volume.in_reader
    assert {m.name: m for m in found}["TopoActive Taiwan"].location == cards.CARD

"""Installing, updating and removing maps on a device's memory card
(docs/design/maps-on-memory-card.md), as Garmin Express does it: a file whose data type has an
ExternalPath goes to the card under it (the map's .img files, the ETYP .sid), any other stays
on internal storage (the unlock, the InstaSearch .sid); a shared terrain layer already on the
device stays where it is and is not downloaded; a small shared file gets its own copy on the
card. An update stays where the map is; a removal takes its files from both storages, and the
card's copy of a shared file once no other map on the card uses it. Nothing on the device
changes before the map fits on both storages and every file fits on a FAT32 card.

The device is a GPSMAP H1i Plus by its GarminDevice.xml (tests/data/gpsmap_h1i_plus/); the maps
are made up, small, with the names and kinds of files of real TopoActive maps."""

import argparse
import base64
import json
from pathlib import Path

import pytest

from openexpress import cli
from openexpress import maps as mp
from openexpress.device import card as cards
from openexpress.device import xmldevice
from openexpress.device.storage import LocalStorage

DATA = Path(__file__).resolve().parent / "data" / "gpsmap_h1i_plus"
DEVICE = xmldevice.parse((DATA / "GarminDevice.xml").read_bytes())
# The first 20 bytes of real .sid files, as a GPSMAP H1i Plus has them: DA69900.sid (ETYP),
# DA669020B.sid (InstaSearch) and DC290000A.sid (Phonetic InstaSearch).
ETYP = bytes.fromhex("4741524d494e205349441c0000000d002e000000")
INSTA = bytes.fromhex("4741524d494e205349441c00000002002a000000")
PHONETIC = bytes.fromhex("4741524d494e205349441c00000009002a000000")
CONTENT = {
    "Garmin/D9213050A.img": b"Taiwan map",
    "Garmin/D9221010A.img": b"Taiwan terrain",
    "Garmin/SID/DA669020B.sid": INSTA + b" Taiwan",
    "Garmin/SID/DA69900.sid": ETYP,
    "Garmin/D9953060A.img": b"Canada map",
    "Garmin/D4712100A.img": b"North America terrain, shared",
    "Garmin/D7753130A.img": b"Middle America map",
    "Garmin/DA257000A.img": b"Greenland map",
}
MD5 = "0123456789ABCDEF0123456789ABCDEF"


def _entry(name, paths, state=0, remove=None, sizes=None):
    """A map as Garmin's server lists it; an installed one names its own files (and its unlock)
    among those to remove, as the server does."""
    if remove is None:
        remove = [*paths, paths[0].rsplit(".", 1)[0] + ".gma"] if state else []
    return {
        "displayname": name, "maptype": "TopoActive", "installationstate": state,
        "isreinstall": False, "canuninstall": True,
        "release": {"majorversion": 2026, "minorversion": 11},
        "downloadhosts": {"foregroundprimaryhost": "https://example.org/maps/",
                          "failoverhosts": []},
        "filestoremove": [{"filename": p} for p in remove],
        "installoptions": [{"ispreferred": True, "identifier": {"regionpartnumber": name},
                            "files": [{"filename": p, "deliverableoptions": [{
                                "type": 1, "url": p.rsplit("/", 1)[-1], "md5": MD5,
                                "sizeinbytes": (sizes or {}).get(p, len(CONTENT[p]))}]}
                                for p in paths]}],
    }


TAIWAN = ["Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA669020B.sid",
          "Garmin/SID/DA69900.sid"]
CANADA = ["Garmin/D9953060A.img", "Garmin/D4712100A.img", "Garmin/SID/DA69900.sid"]
MIDDLE_AMERICA = ["Garmin/D7753130A.img", "Garmin/D4712100A.img", "Garmin/SID/DA69900.sid"]


def _answer(taiwan_state=0, **extra):
    return {"maps": [_entry("TopoActive Taiwan", TAIWAN, taiwan_state, **extra),
                     _entry("TopoActive Canada", CANADA, 1),
                     _entry("TopoActive Middle America", MIDDLE_AMERICA),
                     _entry("TopoActive Greenland", ["Garmin/DA257000A.img"])]}


def _storage(root: Path, description: str, removable: bool) -> LocalStorage:
    root.mkdir(parents=True, exist_ok=True)
    st = LocalStorage(root)
    st.description, st.removable = description, removable
    return st


def _put(st: LocalStorage, path: str, data: bytes | None = None) -> None:
    target = Path(st.root) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(CONTENT.get(path.replace("GARMIN/", "Garmin/"), b"x") if data is None
                       else data)


def _has(st: LocalStorage, path: str) -> bool:
    return st.resolve(path, strict=True) is not None


@pytest.fixture
def handheld(tmp_path):
    """Canada on internal storage (with the shared terrain layer and ETYP file), an empty card."""
    internal = _storage(tmp_path / "internal", "Internal Storage", False)
    card = _storage(tmp_path / "card", "Memory Card", True)
    internal.others = (card,)
    for path in ("GARMIN/D9953060A.img", "GARMIN/D4712100A.img", "GARMIN/SID/DA69900.sid",
                 "GARMIN/D9953060A.gma"):
        _put(internal, path)
    return internal, card


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """The downloads: each file of CONTENT, recorded by its path (`downloads.asked`)."""
    folder = tmp_path / "downloads"
    folder.mkdir()
    asked: list[str] = []

    def fetched(m, session, progress, url_salt, only=None):
        out = {}
        for f in mp.write_order(m):
            if only is not None and f.device_path not in only:
                continue
            asked.append(f.device_path)
            local = folder / f.device_path.replace("/", "_")
            local.write_bytes(CONTENT[f.device_path])
            out[f.device_path] = local
        return out

    monkeypatch.setattr(mp, "_download", fetched)
    return asked


def _unlocks(name):
    def activate(ident):
        return {"unlocks": [{"filename": f"Garmin/{name}.gma",
                             "gma": base64.b64encode(b"UNLOCK").decode()}]}
    return activate


def _maps(internal, answer=None):
    found, _n = mp.read_maps(answer or _answer())
    mp.mark_waiting(internal, found, DEVICE)
    return found, {m.name: m for m in found}


# --- where each file goes ----------------------------------------------------------------------

@pytest.mark.parametrize("path, head, on_card", [
    ("Garmin/D9213050A.img", None, "Garmin/D9213050A.img"),  # the map
    ("Garmin/SID/DA69900.sid", ETYP, "Garmin/SID/DA69900.sid"),  # ETYP: Etyp has a place
    ("Garmin/SID/DA669020B.sid", INSTA, None),  # InstaSearch: none
    ("Garmin/SID/DC290000A.sid", PHONETIC, None),  # Phonetic InstaSearch: none
    ("Garmin/SID/DA669020B.sid", None, None),  # a .sid of a kind not read: internal
    ("Garmin/D9213050A.gma", None, None),  # the unlock: no data type of it
    ("Garmin/SQL/D7729090A.db", None, None),  # NamedTrails or Clc4Database: not told apart
    ("Garmin/gmapbmap.img", None, None),  # the base map: BaseMaps (by its name) has none
    ("Garmin/gmaptz.img", None, None),  # the time zone map: TimeZoneMaps has none
])
def test_each_file_goes_where_its_data_type_goes(path, head, on_card):
    assert cards.card_path_of(path, DEVICE, head) == on_card


def test_a_sid_names_its_kind_in_its_header():
    assert (cards.sid_kind(ETYP), cards.sid_kind(INSTA), cards.sid_kind(PHONETIC)) == (13, 2, 9)
    assert cards.sid_kind(b"GARMIN SID") is None and cards.sid_kind(b"something else" * 2) is None


# --- installing on the card ----------------------------------------------------------------------

def test_a_map_goes_to_the_card_as_garmin_express_puts_it(handheld, downloads):
    internal, card = handheld
    found, by = _maps(internal)
    rep = mp.install(by["TopoActive Taiwan"], found, internal, _unlocks("D9213050A"),
                     device=DEVICE, to=cards.CARD)
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        assert _has(card, path), path
    assert _has(internal, "Garmin/SID/DA669020B.sid") and _has(internal, "Garmin/D9213050A.gma")
    assert not _has(card, "Garmin/SID/DA669020B.sid") and not _has(card, "Garmin/D9213050A.gma")
    assert not _has(internal, "Garmin/D9213050A.img")
    assert len(rep.on_card) == 3 and len(rep.unlocks) == 1 and rep.shared == []
    # the internal copy of the shared ETYP file is the internal maps' own: untouched
    assert (Path(internal.root) / "GARMIN/SID/DA69900.sid").read_bytes() == ETYP
    found, by = _maps(internal)
    assert by["TopoActive Taiwan"].location == cards.CARD


def test_a_shared_terrain_layer_stays_where_it_is_and_is_not_downloaded(handheld, downloads):
    internal, card = handheld
    found, by = _maps(internal)
    rep = mp.install(by["TopoActive Middle America"], found, internal, _unlocks("D7753130A"),
                     device=DEVICE, to=cards.CARD)
    assert "Garmin/D4712100A.img" not in downloads
    assert rep.shared == ["Garmin/D4712100A.img"]
    assert _has(card, "Garmin/D7753130A.img") and not _has(card, "Garmin/D4712100A.img")
    assert _has(internal, "Garmin/D4712100A.img")
    found, by = _maps(internal)
    assert by["TopoActive Middle America"].location == cards.CARD
    assert by["TopoActive Canada"].location == cards.INTERNAL


def test_a_shared_layer_not_on_the_device_yet_goes_with_the_map(handheld, downloads):
    internal, card = handheld
    (Path(internal.root) / "GARMIN/D4712100A.img").unlink()
    found, by = _maps(internal)
    mp.install(by["TopoActive Middle America"], found, internal, _unlocks("D7753130A"),
               device=DEVICE, to=cards.CARD)
    assert _has(card, "Garmin/D4712100A.img")


def test_the_plan_says_where_each_file_goes_before_anything_is_downloaded(handheld, downloads):
    internal, _card = handheld
    found, by = _maps(internal)
    plan = mp.card_plan(by["TopoActive Middle America"], found, internal, DEVICE)
    assert [(p.path, p.where, p.storage is None) for p in plan] == [  # smallest first
        ("Garmin/D7753130A.img", cards.CARD, False),
        ("Garmin/SID/DA69900.sid", cards.INTERNAL, False),  # its kind not read yet
        ("Garmin/D4712100A.img", cards.INTERNAL, True),  # shared, on internal: stays there
    ]
    assert downloads == []


# --- refused before anything changes -------------------------------------------------------------

def test_without_a_card_nothing_is_installed_on_it(handheld, downloads):
    internal, _card = handheld
    internal.others = ()
    found, by = _maps(internal)
    with pytest.raises(mp.MapError, match="the device has no memory card"):
        mp.install(by["TopoActive Greenland"], found, internal, _unlocks("DA257000A"),
                   device=DEVICE, to=cards.CARD)
    assert downloads == []


def test_a_file_larger_than_fat32_holds_is_refused_before_the_download(handheld, downloads):
    internal, card = handheld
    big = {"maps": [_entry("TopoActive Greenland", ["Garmin/DA257000A.img"],
                           sizes={"Garmin/DA257000A.img": 2**32})]}
    found, by = _maps(internal, big)
    with pytest.raises(mp.MapError, match="larger than a FAT32 card holds"):
        mp.install(by["TopoActive Greenland"], found, internal, _unlocks("DA257000A"),
                   device=DEVICE, to=cards.CARD)
    assert downloads == [] and not _has(card, "Garmin/DA257000A.img")


def test_a_card_without_room_is_refused_before_the_download(handheld, downloads, monkeypatch):
    internal, card = handheld
    monkeypatch.setattr(card, "free_space", lambda: 1000)
    found, by = _maps(internal)
    with pytest.raises(mp.MapError):
        mp.install(by["TopoActive Greenland"], found, internal, _unlocks("DA257000A"),
                   device=DEVICE, to=cards.CARD)
    assert downloads == []


def test_a_map_on_one_storage_is_not_installed_on_the_other(handheld, downloads):
    internal, card = handheld
    found, by = _maps(internal)
    with pytest.raises(mp.MapError, match="on the device already"):
        mp.install(by["TopoActive Canada"], found, internal, _unlocks("D9953060A"),
                   device=DEVICE, to=cards.CARD)
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        _put(card, path)
    _put(internal, "GARMIN/SID/DA669020B.sid")
    found, by = _maps(internal, _answer(taiwan_state=1))
    with pytest.raises(mp.MapError, match="on the memory card already"):
        mp.install(by["TopoActive Taiwan"], found, internal, _unlocks("D9213050A"),
                   device=DEVICE, to=cards.INTERNAL)
    assert downloads == []


# --- an update stays on the card; a removal takes it from both -----------------------------------

def _taiwan_on_card(internal, card, extra_card=()):
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid",
                 *extra_card):
        _put(card, path)
    for path in ("GARMIN/SID/DA669020B.sid", "GARMIN/D9213050A.gma"):
        _put(internal, path)


def test_an_update_of_a_map_on_the_card_is_written_there(handheld, downloads):
    """The version on the card, D9213040A.img; the new one, D9213050A.img."""
    internal, card = handheld
    for path in ("Garmin/D9213040A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        _put(card, path)
    for path in ("GARMIN/SID/DA669020B.sid", "GARMIN/D9213040A.gma"):
        _put(internal, path)
    answer = _answer(taiwan_state=1, remove=[
        "Garmin/D9213040A.img", "Garmin/D9221010A.img", "Garmin/SID/DA669020B.sid",
        "Garmin/D9213040A.gma"])
    found, by = _maps(internal, answer)
    taiwan = by["TopoActive Taiwan"]
    assert taiwan.location == cards.CARD and mp.install_target(taiwan) == cards.CARD
    mp.install(taiwan, found, internal, _unlocks("D9213050A"), device=DEVICE)
    assert not _has(card, "Garmin/D9213040A.img")  # the old version, removed from the card
    assert not _has(internal, "Garmin/D9213040A.gma")  # and its unlock from internal
    assert _has(card, "Garmin/D9213050A.img") and _has(card, "Garmin/D9221010A.img")
    assert not _has(internal, "Garmin/D9213050A.img")
    assert _has(internal, "Garmin/D9213050A.gma") and _has(internal, "Garmin/SID/DA669020B.sid")


def test_a_removal_takes_the_map_from_both_storages(handheld):
    internal, card = handheld
    _taiwan_on_card(internal, card)
    found, by = _maps(internal, _answer(taiwan_state=1))
    removed = mp.uninstall(by["TopoActive Taiwan"], found, internal, device=DEVICE)
    for st, path in ((card, "Garmin/D9213050A.img"), (card, "Garmin/D9221010A.img"),
                     (internal, "Garmin/SID/DA669020B.sid")):
        assert not _has(st, path), path
    # The card's copy of the shared ETYP file: no other map on the card uses it, and internal
    # storage keeps its own for Canada.
    assert not _has(card, "Garmin/SID/DA69900.sid")
    assert _has(internal, "Garmin/SID/DA69900.sid")
    assert not _has(internal, "Garmin/D9213050A.gma")
    assert len(removed) == 5


def test_a_shared_copy_another_map_on_the_card_uses_stays(handheld):
    internal, card = handheld
    _taiwan_on_card(internal, card)
    _put(card, "Garmin/D7753130A.img")  # Middle America on the card too, using the same ETYP
    answer = _answer(taiwan_state=1)
    answer["maps"][2]["installationstate"] = 1
    found, by = _maps(internal, answer)
    assert by["TopoActive Middle America"].location == cards.CARD
    mp.uninstall(by["TopoActive Taiwan"], found, internal, device=DEVICE)
    assert _has(card, "Garmin/SID/DA69900.sid")


def test_a_map_on_internal_storage_is_removed_from_internal_storage_alone(handheld):
    """Canada, the only installed map using the North America layer: it goes with it; the ETYP
    file Taiwan on the card uses too stays, on both storages."""
    internal, card = handheld
    _taiwan_on_card(internal, card)
    found, by = _maps(internal, _answer(taiwan_state=1))
    places = mp.removal_places(by["TopoActive Canada"], found, internal, DEVICE)
    assert sorted((st is card, p) for st, p in places) == [
        (False, "Garmin/D4712100A.img"), (False, "Garmin/D9953060A.gma"),
        (False, "Garmin/D9953060A.img")]


# --- the command line ------------------------------------------------------------------------------

def test_the_dry_run_on_the_command_line_says_where_each_file_goes(handheld, downloads, capsys):
    internal, _card = handheld
    found, by = _maps(internal)
    args = argparse.Namespace(maps_cmd="install", apply=False, to="card",
                              hidden_folder_trial=False)
    assert cli.change_map(args, None, internal, DEVICE, found,
                          by["TopoActive Middle America"]) == 0
    out = capsys.readouterr().out.replace("\xa0", " ")
    assert "Installing map TopoActive Middle America 2026.11 on the memory card" in out
    assert "+ Garmin/D7753130A.img  18 B  (memory card)" in out
    assert "= Garmin/D4712100A.img  29 B  (shared, stays where it is)" in out
    assert "+ Garmin/SID/DA69900.sid  20 B  (the device or the memory card, by its kind)" in out
    assert "Space needed on the device" in out and "on the memory card" in out
    assert "Dry run: nothing was downloaded or written" in out
    assert downloads == []


def test_the_command_line_takes_where_to_install():
    args = cli.build_parser().parse_args(["maps", "install", "--to", "card", "Greenland"])
    assert (args.to, args.name) == ("card", "Greenland")
    assert cli.build_parser().parse_args(["maps", "install", "Greenland"]).to is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["maps", "install", "--to", "elsewhere", "Greenland"])


def test_the_answers_of_this_file_are_made_up():
    assert json.dumps(_answer()).count(MD5) == 11  # every file, one made-up checksum

"""Moving a map between a device's own storage and its memory card
(docs/design/maps-on-memory-card.md, its move): only the files with a place on the card move (its
.img files, the ETYP .sid), each from the cache when it has it, else read from the device, and
checked against Garmin's checksum before it is written; else downloaded again. The unlock and
search files and a shared terrain layer stay where they are; a shared ETYP file gets a copy
on the target, and its source copy goes only when no map off the target uses it. Nothing is
removed before every file is on the target, and a failure on the way leaves the source as it
was and removes what the move wrote. A map on both storages is kept on one alone, its copy
there checked first.

The device is a GPSMAP H1i Plus by its GarminDevice.xml; the maps are made up and small."""

import argparse
import hashlib
from pathlib import Path

import pytest
from test_maps_card_install import CONTENT, DEVICE, _has, _put, _storage

from openexpress import cli
from openexpress import maps as mp
from openexpress.device import card as cards

TAIWAN = ["Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA669020B.sid",
          "Garmin/SID/DA69900.sid"]
CANADA = ["Garmin/D9953060A.img", "Garmin/D4712100A.img", "Garmin/SID/DA69900.sid"]
MIDDLE_AMERICA = ["Garmin/D7753130A.img", "Garmin/D4712100A.img", "Garmin/SID/DA69900.sid"]


def _md5(path: str) -> str:
    return hashlib.md5(CONTENT[path]).hexdigest()


def _entry(name, paths, state=1, sizes=None):
    """An installed map as Garmin's server lists it: its own files among those to remove."""
    return {
        "displayname": name, "maptype": "TopoActive", "installationstate": state,
        # the server offers an installed, current map again as a reinstall
        "isreinstall": bool(state), "canuninstall": True,
        "release": {"majorversion": 2026, "minorversion": 11},
        "downloadhosts": {"foregroundprimaryhost": "https://example.org/maps/",
                          "failoverhosts": []},
        "filestoremove": [{"filename": p} for p in [*paths, paths[0][:-4] + ".gma"]],
        "installoptions": [{"ispreferred": True, "identifier": {"regionpartnumber": name},
                            "files": [{"filename": p, "deliverableoptions": [{
                                "type": 1, "url": p.rsplit("/", 1)[-1], "md5": _md5(p),
                                "sizeinbytes": (sizes or {}).get(p, len(CONTENT[p]))}]}
                                for p in paths]}],
    }


def _answer(middle_america=0, sizes=None):
    return {"maps": [_entry("TopoActive Taiwan", TAIWAN, sizes=sizes),
                     _entry("TopoActive Canada", CANADA),
                     _entry("TopoActive Middle America", MIDDLE_AMERICA, middle_america)]}


@pytest.fixture
def handheld(tmp_path, xdg):
    """Canada on internal storage, Taiwan on the card as Garmin Express put it there."""
    internal = _storage(tmp_path / "internal", "Internal Storage", False)
    card = _storage(tmp_path / "card", "Memory Card", True)
    internal.others = (card,)
    for path in ("GARMIN/D9953060A.img", "GARMIN/D4712100A.img", "GARMIN/SID/DA69900.sid",
                 "GARMIN/D9953060A.gma", "GARMIN/SID/DA669020B.sid", "GARMIN/D9213050A.gma"):
        _put(internal, path)
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        _put(card, path)
    return internal, card


@pytest.fixture
def no_download(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("nothing is downloaded")
    monkeypatch.setattr(mp, "_download", refused)


def _maps(internal, answer=None):
    found, _n = mp.read_maps(answer or _answer())
    mp.mark_waiting(internal, found, DEVICE)
    return found, {m.name: m for m in found}


def _move(internal, name, to, answer=None, **kwargs):
    found, by = _maps(internal, answer)
    return mp.move(by[name], found, internal, DEVICE, to, **kwargs)


def _location(internal, name, answer=None):
    return _maps(internal, answer)[1][name].location


# --- from the card to the device and back ----------------------------------------------------------

def test_a_map_on_the_card_moves_to_the_device(handheld, no_download):
    internal, card = handheld
    rep = _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    assert _has(internal, "Garmin/D9213050A.img") and _has(internal, "Garmin/D9221010A.img")
    assert not _has(card, "Garmin/D9213050A.img") and not _has(card, "Garmin/D9221010A.img")
    # The card's copy of the shared ETYP file: no map is left on the card, internal has its own.
    assert not _has(card, "Garmin/SID/DA69900.sid") and _has(internal, "Garmin/SID/DA69900.sid")
    assert _has(internal, "Garmin/SID/DA669020B.sid") and _has(internal, "Garmin/D9213050A.gma")
    assert (len(rep.copied), rep.read_from_device, rep.downloaded) == (2, 2, 0)
    assert len(rep.removed) == 3
    assert _location(internal, "TopoActive Taiwan") == cards.INTERNAL


def test_a_map_on_the_device_moves_to_the_card(handheld, no_download):
    internal, card = handheld
    _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    rep = _move(internal, "TopoActive Taiwan", cards.CARD)
    assert _has(card, "Garmin/D9213050A.img") and not _has(internal, "Garmin/D9213050A.img")
    # The ETYP file gets a copy on the card; Canada, on internal, keeps the internal one.
    assert _has(card, "Garmin/SID/DA69900.sid") and _has(internal, "Garmin/SID/DA69900.sid")
    # Its unlock and search file stay on internal.
    assert _has(internal, "Garmin/SID/DA669020B.sid") and _has(internal, "Garmin/D9213050A.gma")
    assert not _has(card, "Garmin/SID/DA669020B.sid")
    # The first move left the map's files in the cache; the ETYP file, which it did not copy,
    # is read from the device.
    assert rep.read_from_device == 1
    assert _location(internal, "TopoActive Taiwan") == cards.CARD


def test_a_shared_terrain_layer_stays_where_it_is(handheld, no_download):
    internal, card = handheld
    _put(internal, "GARMIN/D7753130A.img")  # Middle America on internal too, sharing the layer
    answer = _answer(middle_america=1)
    rep = _move(internal, "TopoActive Canada", cards.CARD, answer)
    assert _has(card, "Garmin/D9953060A.img") and not _has(internal, "Garmin/D9953060A.img")
    assert _has(internal, "Garmin/D4712100A.img") and not _has(card, "Garmin/D4712100A.img")
    assert "Garmin/D4712100A.img" not in rep.copied
    assert _location(internal, "TopoActive Canada", answer) == cards.CARD


def test_the_plan_says_what_moves_and_what_stays(handheld):
    internal, card = handheld
    found, by = _maps(internal)
    steps = mp.move_plan(by["TopoActive Taiwan"], found, internal, DEVICE, cards.INTERNAL)
    assert [(s.target_path, s.copy, s.verify, s.delete) for s in steps] == [
        ("Garmin/D9213050A.img", True, False, True),  # smallest first
        ("Garmin/D9221010A.img", True, False, True),
        ("Garmin/SID/DA69900.sid", False, False, True),  # internal has it: the card copy goes
    ]
    assert mp.move_space(steps) == len(CONTENT["Garmin/D9213050A.img"]) + len(
        CONTENT["Garmin/D9221010A.img"])


# --- Garmin's files, or nothing changes ----------------------------------------------------------

def test_a_file_from_the_cache_is_not_read_from_the_device(handheld, no_download):
    internal, _card = handheld
    found, by = _maps(internal)
    for f in by["TopoActive Taiwan"].files:
        mp.cache_file(f).parent.mkdir(parents=True, exist_ok=True)
        mp.cache_file(f).write_bytes(CONTENT[f.device_path])
    rep = mp.move(by["TopoActive Taiwan"], found, internal, DEVICE, cards.INTERNAL)
    assert (len(rep.copied), rep.read_from_device) == (2, 0)


def test_a_copy_on_the_device_that_is_not_garmins_file_moves_nothing(handheld):
    internal, card = handheld
    _put(card, "Garmin/D9221010A.img", b"Taiwan terrain, damaged")
    with pytest.raises(mp.MapError, match="is not Garmin's file"):
        _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    assert _has(card, "Garmin/D9213050A.img") and _has(card, "Garmin/D9221010A.img")
    assert not _has(internal, "Garmin/D9213050A.img")  # what the move wrote is removed again
    assert not _has(internal, "Garmin/D9221010A.img")


def test_a_copy_that_is_not_garmins_file_is_downloaded_again(handheld, monkeypatch, tmp_path):
    internal, card = handheld
    _put(card, "Garmin/D9221010A.img", b"Taiwan terrain, damaged")
    asked = []

    def fetched(m, session, progress, url_salt, only=None):
        (path,) = only
        asked.append(path)
        local = tmp_path / "downloaded"
        local.write_bytes(CONTENT[path])
        return {path: local}

    monkeypatch.setattr(mp, "_download", fetched)
    rep = _move(internal, "TopoActive Taiwan", cards.INTERNAL, session=object())
    assert asked == ["Garmin/D9221010A.img"] and rep.downloaded == 1
    assert (Path(internal.root) / "GARMIN/D9221010A.img").read_bytes() == CONTENT[
        "Garmin/D9221010A.img"]


def test_a_write_that_fails_leaves_the_source_and_removes_what_was_written(handheld, monkeypatch,
                                                                            no_download):
    internal, card = handheld
    written = []
    real = internal.write_file_checked

    def second_fails(path, local, *args, **kwargs):
        if written:
            raise mp.MapError("the device refused the file")
        written.append(path)
        return real(path, local, *args, **kwargs)

    monkeypatch.setattr(internal, "write_file_checked", second_fails)
    with pytest.raises(mp.MapError, match="refused"):
        _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    assert written and not _has(internal, written[0])
    for path in ("Garmin/D9213050A.img", "Garmin/D9221010A.img", "Garmin/SID/DA69900.sid"):
        assert _has(card, path), path
    assert _location(internal, "TopoActive Taiwan") == cards.CARD


# --- a map on both storages -----------------------------------------------------------------------

def test_a_map_on_both_storages_is_kept_on_one_its_copy_there_checked(handheld, no_download):
    internal, card = handheld
    for path in ("GARMIN/D9213050A.img", "GARMIN/D9221010A.img"):
        _put(internal, path)
    assert _location(internal, "TopoActive Taiwan") == cards.BOTH
    rep = _move(internal, "TopoActive Taiwan", cards.CARD)
    assert sorted(rep.verified) == ["Garmin/D9213050A.img", "Garmin/D9221010A.img"]
    assert rep.copied == []
    assert not _has(internal, "Garmin/D9213050A.img") and _has(card, "Garmin/D9213050A.img")
    assert _location(internal, "TopoActive Taiwan") == cards.CARD


def test_a_map_on_both_is_not_kept_where_its_copy_is_damaged(handheld, no_download):
    internal, card = handheld
    for path in ("GARMIN/D9213050A.img", "GARMIN/D9221010A.img"):
        _put(internal, path)
    _put(card, "Garmin/D9213050A.img", b"Taiwan map, damaged")
    with pytest.raises(mp.MapError, match="where it is to stay is not Garmin's file"):
        _move(internal, "TopoActive Taiwan", cards.CARD)
    assert _has(internal, "Garmin/D9213050A.img")  # nothing removed


def test_a_map_half_moved_is_finished(handheld, no_download):
    """Stopped after one of Taiwan's files was copied to internal: kept on internal, the copy
    there is checked and the other file copied."""
    internal, card = handheld
    _put(internal, "GARMIN/D9213050A.img")
    assert _location(internal, "TopoActive Taiwan") == cards.BOTH
    rep = _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    assert rep.verified == ["Garmin/D9213050A.img"] and len(rep.copied) == 1
    assert not _has(card, "Garmin/D9213050A.img") and not _has(card, "Garmin/D9221010A.img")


# --- refused -----------------------------------------------------------------------------------------

def test_a_map_already_there_is_not_moved(handheld):
    internal, _card = handheld
    with pytest.raises(mp.MapError, match="on the memory card already"):
        _move(internal, "TopoActive Taiwan", cards.CARD)


def test_a_map_not_on_the_device_or_not_up_to_date_is_not_moved(handheld):
    internal, _card = handheld
    found, by = _maps(internal)
    assert "is not on the device" in mp.move_refused(by["TopoActive Middle America"])
    taiwan = by["TopoActive Taiwan"]
    taiwan.is_reinstall, taiwan.state = False, 1
    taiwan.files_to_remove = ["Garmin/D9213040A.img"]  # an update: the device has another version
    assert taiwan.would_update
    assert "up to date" in mp.move_refused(taiwan)


def test_without_a_card_nothing_moves(handheld):
    internal, _card = handheld
    found, by = _maps(internal)
    internal.others = ()
    with pytest.raises(mp.MapError, match="has no memory card"):
        mp.move(by["TopoActive Canada"], found, internal, DEVICE, cards.CARD)


def test_a_file_larger_than_fat32_holds_does_not_move_to_the_card(handheld):
    internal, _card = handheld
    _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    answer = _answer(sizes={"Garmin/D9213050A.img": 2**32})
    found, by = _maps(internal, answer)
    taiwan = by["TopoActive Taiwan"]
    steps = mp.move_plan(taiwan, found, internal, DEVICE, cards.CARD)
    assert "larger than a FAT32 card holds" in mp.move_refusals(taiwan, steps, cards.CARD)[0]


def test_a_card_without_room_moves_nothing(handheld, monkeypatch, no_download):
    internal, card = handheld
    _move(internal, "TopoActive Taiwan", cards.INTERNAL)
    monkeypatch.setattr(card, "free_space", lambda: 10)
    with pytest.raises(mp.MapError):
        _move(internal, "TopoActive Taiwan", cards.CARD)
    assert _has(internal, "Garmin/D9213050A.img")


# --- the command line -------------------------------------------------------------------------------

def test_the_dry_run_says_what_moves(handheld, capsys):
    internal, _card = handheld
    found, by = _maps(internal)
    args = argparse.Namespace(maps_cmd="move", apply=False, to="internal")
    assert cli.change_map(args, None, internal, DEVICE, found, by["TopoActive Taiwan"]) == 0
    out = capsys.readouterr().out.replace("\xa0", " ")
    assert "Moving map TopoActive Taiwan to the device: 3 files" in out
    assert "> Garmin/D9213050A.img  10 B  (then removed from where it was)" in out
    assert "= Garmin/SID/DA69900.sid  20 B  (there already)" in out
    assert "Its unlock and search files" in out
    assert "Dry run: nothing was read, written or removed" in out
    assert _has(_card, "Garmin/D9213050A.img")


def test_the_command_line_takes_where_a_map_moves():
    args = cli.build_parser().parse_args(["maps", "move", "Taiwan", "--to", "card"])
    assert (args.maps_cmd, args.name, args.to) == ("move", "Taiwan", "card")
    with pytest.raises(SystemExit):  # where to: required
        cli.build_parser().parse_args(["maps", "move", "Taiwan"])


def test_the_list_of_maps_says_about_how_much_a_move_copies(handheld):
    """Canada's own .img; not the North America layer it shares with Middle America."""
    internal, _card = handheld
    _put(internal, "GARMIN/D7753130A.img")
    found, by = _maps(internal, _answer(middle_america=1))
    assert mp.move_size(by["TopoActive Canada"], found) == len(CONTENT["Garmin/D9953060A.img"])


# --- downloaded again rather than read off the device ------------------------------------------

def test_a_move_may_download_the_files_again_instead_of_reading_the_device(handheld, monkeypatch,
                                                                         tmp_path):
    internal, card = handheld
    asked = []

    def fetched(m, session, progress, url_salt, only=None):
        (path,) = only
        asked.append(path)
        local = tmp_path / path.replace("/", "_")
        local.write_bytes(CONTENT[path])
        return {path: local}

    def not_read(*args, **kwargs):
        raise AssertionError("nothing is read off the device")

    monkeypatch.setattr(mp, "_download", fetched)
    monkeypatch.setattr(card, "copy_out", not_read)
    rep = _move(internal, "TopoActive Taiwan", cards.INTERNAL, session=object(),
                source=mp.FROM_DOWNLOAD)
    assert sorted(asked) == ["Garmin/D9213050A.img", "Garmin/D9221010A.img"]
    assert (rep.downloaded, rep.read_from_device, len(rep.copied)) == (2, 0, 2)
    assert _has(internal, "Garmin/D9213050A.img") and not _has(card, "Garmin/D9213050A.img")


def test_a_file_in_the_cache_is_not_downloaded_again(handheld, monkeypatch):
    internal, _card = handheld
    found, by = _maps(internal)
    for f in by["TopoActive Taiwan"].files:
        mp.cache_file(f).parent.mkdir(parents=True, exist_ok=True)
        mp.cache_file(f).write_bytes(CONTENT[f.device_path])
    monkeypatch.setattr(mp, "_download", lambda *a, **k: pytest.fail("downloaded"))
    assert mp.move_cached(by["TopoActive Taiwan"], found) == mp.move_size(
        by["TopoActive Taiwan"], found)
    rep = mp.move(by["TopoActive Taiwan"], found, internal, DEVICE, cards.INTERNAL,
                  session=object(), source=mp.FROM_DOWNLOAD)
    assert (rep.downloaded, len(rep.copied)) == (0, 2)


def test_downloading_again_needs_a_connection(handheld):
    internal, _card = handheld
    with pytest.raises(mp.MapError, match="cannot be downloaded again"):
        _move(internal, "TopoActive Taiwan", cards.INTERNAL, source=mp.FROM_DOWNLOAD)


def test_the_speed_maps_download_at_is_kept(xdg):
    assert mp.download_speed() is None
    mp.note_download_speed(1_000_000, 1.0)  # too small to say anything
    assert mp.download_speed() is None
    mp.note_download_speed(100_000_000, 10.0)  # 10 MB/s
    assert mp.download_speed() == 10_000_000
    mp.note_download_speed(100_000_000, 5.0)  # 20 MB/s: averaged with the one before
    assert mp.download_speed() == 15_000_000


def test_the_command_line_takes_downloading_again():
    args = cli.build_parser().parse_args(["maps", "move", "Taiwan", "--to", "card", "--download"])
    assert args.download
    assert not cli.build_parser().parse_args(["maps", "move", "Taiwan", "--to", "card"]).download

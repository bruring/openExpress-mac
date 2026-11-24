"""tools/storage_probe.py, the snapshot of every storage of a device for finding where Garmin
Express puts a map's files, without a device: it lists each storage (a memory card told by its
StorageType) with its folders and files, never writes to the device, says what changed between
two snapshots, and masks the unit ID and the unlock codes of the GarminDevice.xml it saves. The
device and its files are made up."""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fakes import ptp_opened
from ptp_fake import FakeDevice

from openexpress.device import ptp

ROOT = Path(__file__).resolve().parents[1]
CARD = 0x00020001
XML = (b'<?xml version="1.0"?>\n<Device xmlns="http://www.garmin.com/xmlschemas/GarminDevice/v2">'
       b"<Model><PartNumber>006-B4552-00</PartNumber></Model><Id>3912345678</Id>"
       b"<Unlock><Code>ABCDEFGHIJKLMNOPQRSTUVWXY</Code></Unlock>"
       b"<Extensions><DeviceExtension><Ifix>1234567890</Ifix></DeviceExtension>"
       b"<IQAppExt><Modulus>QUJDRA==</Modulus><Exponent>65537</Exponent></IQAppExt>"
       b"</Extensions></Device>")


@pytest.fixture
def probe(monkeypatch):
    spec = importlib.util.spec_from_file_location("storage_probe",
                                                  ROOT / "tools" / "storage_probe.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "storage_probe", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handheld():
    """A handheld with TopoActive on its internal storage and a memory card."""
    fake = FakeDevice()
    fake.add("GARMIN/GarminDevice.xml", XML)
    fake.add("GARMIN/D9953060A.img", b"map" * 10)
    fake.add("GARMIN/D9953060A.gma", b"unlock")
    fake.add("GARMIN/SID/DA69900.sid", b"sid")
    fake.add_storage(CARD, "Memory Card", 128 * 10**9, 127 * 10**9,
                     kind=ptp.STORAGE_REMOVABLE_RAM)
    return fake


def _writes(fake) -> int:
    return sum(fake.sent(op) for op in (ptp.SEND_OBJECT_INFO, ptp.SEND_OBJECT,
                                        ptp.DELETE_OBJECT))


def test_every_storage_is_listed_with_its_kind_and_files(probe, handheld):
    with ptp_opened(handheld) as session:
        snap = probe.snapshot(session)
    internal, card = snap["storages"]
    assert (internal["description"], internal["type"], internal["removable"]) == (
        "Internal", "FixedRAM", False)
    assert internal["has_device_xml"] is True
    assert internal["files"]["GARMIN/D9953060A.img"] == 30
    assert "GARMIN/SID" in internal["folders"]
    assert (card["id"], card["description"], card["type"], card["removable"]) == (
        "00020001", "Memory Card", "RemovableRAM", True)
    assert (card["files"], card["has_device_xml"]) == ({}, False)
    assert _writes(handheld) == 0


def test_the_depth_stops_the_listing(probe, handheld):
    with ptp_opened(handheld) as session:
        internal = probe.snapshot(session, depth=1)["storages"][0]
    assert internal["folders"] == ["GARMIN"] and internal["files"] == {}


def test_the_difference_names_each_file_added_removed_or_changed(probe):
    def snap(files, free):
        return {"storages": [{"id": "00020001", "description": "Memory Card", "free": free,
                              "files": files}]}

    before = snap({"Garmin/old.img": 5, "Garmin/same.img": 7, "Garmin/grown.db": 1}, 100)
    after = snap({"Garmin/new.img": 9, "Garmin/same.img": 7, "Garmin/grown.db": 2}, 90)
    assert probe.diff(before, after) == [
        "## Memory Card: 3 file(s) changed, free space -10 bytes",
        "  ~ Garmin/grown.db  1 -> 2",
        "  + Garmin/new.img  9",
        "  - Garmin/old.img  5",
    ]


def test_a_storage_that_came_or_went_is_said(probe):
    card = {"id": "00020001", "description": "Memory Card", "free": 1, "files": {}}
    assert probe.diff({"storages": []}, {"storages": [card]}) == ["## Memory Card: added"]


def test_the_saved_device_file_has_no_unit_id_and_no_unlock_code(probe, handheld):
    masked = probe.masked_device_xml(XML)
    assert b"3912345678" not in masked and b"ABCDEFGHIJ" not in masked
    assert b"<Id>0</Id>" in masked and b"<Code>" + b"X" * 25 + b"</Code>" in masked
    assert b"1234567890" not in masked and b"QUJDRA==" not in masked
    assert b"<PartNumber>006-B4552-00</PartNumber>" in masked
    assert b"<Exponent>65537</Exponent>" in masked


def test_the_command_writes_the_snapshot_and_the_masked_file(probe, handheld, tmp_path,
                                                             capsys):
    out, xml = tmp_path / "before.json", tmp_path / "GarminDevice.xml"
    code = probe.main(["snapshot", str(out), "--device-xml", str(xml)],
                      opener=lambda: ptp_opened(handheld))
    assert code == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [s["description"] for s in saved["storages"]] == ["Internal", "Memory Card"]
    assert b"3912345678" not in xml.read_bytes()
    assert "Memory Card: RemovableRAM, 0 files" in capsys.readouterr().out
    assert _writes(handheld) == 0


def test_without_a_device_it_says_what_to_do(probe, tmp_path, capsys):
    assert probe.main(["snapshot", str(tmp_path / "x.json")], opener=lambda: None) == 2
    assert "quit openExpress for Mac and Garmin Express" in capsys.readouterr().err
    with contextlib.suppress(FileNotFoundError):
        assert not (tmp_path / "x.json").exists()

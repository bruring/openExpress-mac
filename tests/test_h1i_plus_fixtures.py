"""The GPSMAP H1i Plus fixtures (tests/data/gpsmap_h1i_plus/): its GarminDevice.xml and the
universal map service's answer for it, as a real device and Garmin's server gave them, masked.
They hold what installing maps on its memory card is designed from
(docs/design/maps-on-memory-card.md): the device declares the card as a place for maps, the map
data types say their folder on it (ExternalPath), the search data types say none, and maps share
large files, so a map cannot always move alone. Nothing in them tells the unit apart."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from openexpress import maps
from openexpress.device import xmldevice

DATA = Path(__file__).resolve().parent / "data" / "gpsmap_h1i_plus"
XML = (DATA / "GarminDevice.xml").read_bytes()
ANSWER = json.loads((DATA / "universal_maps.json").read_text(encoding="utf-8"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _external_paths() -> dict[str, str | None]:
    """Each data type with map-like files: its ExternalPath, or None when it has none."""
    out = {}
    for dt in ET.fromstring(XML).iter():
        if _local(dt.tag) != "DataType":
            continue
        name = next(c.text for c in dt if _local(c.tag) == "Name")
        found = [e.text for e in dt.iter() if _local(e.tag) == "ExternalPath"]
        out[name] = found[0] if found else None
    return out


def test_the_device_file_is_that_of_an_h1i_plus_with_its_unit_masked():
    device = xmldevice.parse(XML)
    assert (device.description, device.part_number, device.unit_id) == (
        "GPSMAP H1i Plus", "006-B4552-00", 0)
    for tag in ("Code", "Ifix", "Modulus"):
        values = re.findall(rb"<(?:\w+:)?" + tag.encode() + rb">([^<]*)<", XML)
        assert values and all(set(v) == {ord("X")} for v in values), tag


def test_the_memory_card_is_declared_as_a_place_for_maps():
    locations = [e.attrib for e in ET.fromstring(XML).iter()
                 if _local(e.tag) == "Location" and "MTPId" in e.attrib]
    assert locations == [{"MaxSize": "1099511627776", "MinSize": "4000000000",
                          "MTPId": "Memory Card", "Name": "Map", "SupportedFormat": "FAT32",
                          "UMSId": "GARMIN CARD"}]


def test_the_map_data_types_name_their_folder_on_the_card_and_the_search_ones_do_not():
    paths = _external_paths()
    for name in ("PreProgrammedNamedMaps", "NMaps", "TopoActiveDEM"):
        assert paths[name] == "Garmin", name
    assert paths["NamedTrails"] == "Garmin/SQL" and paths["Etyp"] == "Garmin/SID"
    for name in ("TopoActiveInstaSearch", "TopoActivePhoneticInstaSearch", "MapRegionGrouping",
                 "BaseMaps", "TimeZoneMaps"):
        assert paths[name] is None, name


def test_the_answer_names_no_place_and_its_maps_share_large_files():
    found, listed = maps.read_maps(ANSWER)
    assert listed == 26
    installed = sorted(m.name for m in found if m.installed)
    assert installed == ["Base maps", "TopoActive Canada", "TopoActive Central Europe",
                         "TopoActive Eastern Europe", "TopoActive United States East",
                         "TopoActive United States West", "TopoActive Western Europe"]
    # Every file is a path from the root of a storage; none says internal or card.
    assert all(f.device_path.startswith("Garmin/") for m in found for f in m.files)
    users = {}
    for m in found:
        for f in m.files:
            users.setdefault(f.device_path, []).append(m.name)
    assert sorted(users["Garmin/D4712100A.img"]) == [
        "TopoActive Canada", "TopoActive Middle America", "TopoActive United States East",
        "TopoActive United States West"]
    assert len(users["Garmin/D4713110A.img"]) == 3  # the three of Europe
    # The largest file still fits FAT32's limit of a file (4 GiB - 1).
    assert max(f.size for m in found for f in m.files) < 2**32
    # The activation codes are masked.
    assert all(set(m.identifier["activationrequestcode"]) == {"X"} for m in found)

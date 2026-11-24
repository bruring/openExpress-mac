"""Parser for GARMIN/GarminDevice.xml (device capabilities and paths)."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = "{http://www.garmin.com/xmlschemas/GarminDevice/v2}"

OUTPUT = "OutputFromUnit"
INPUT = "InputToUnit"
BOTH = "InputOutput"


@dataclass(frozen=True)
class DeviceFile:
    """One <File> entry of a <DataType>: where and which files of that type are on the device."""

    datatype: str
    direction: str
    path: str
    basename: str = ""
    extension: str = ""
    # Where files of this type go on the device's memory card, from the root of the card
    # (DataTypeLocationExtension/ExternalPath, e.g. "Garmin"); None: they are never put there.
    external_path: str | None = None

    @property
    def readable(self) -> bool:
        return self.direction in (OUTPUT, BOTH)

    @property
    def writable(self) -> bool:
        return self.direction in (INPUT, BOTH)

    def matches(self, device_path: str) -> bool:
        """Whether a file (path relative to the storage root) belongs to this entry.

        Case-insensitive comparison, as in Garmin Express; files in
        subdirectories count too.
        """
        p = device_path.casefold().strip("/")
        base = self.path.casefold().strip("/")
        if base and not p.startswith(base + "/"):
            return False
        name = posixpath.basename(p)
        stem, _, ext = name.rpartition(".")
        if not stem:
            stem, ext = ext, ""
        if self.extension and ext != self.extension.casefold():
            return False
        # Two separate checks read more clearly than one combined return.
        if self.basename and stem != self.basename.casefold():  # noqa: SIM103
            return False
        return True


@dataclass(frozen=True)
class UpdateFile:
    part_number: str
    version: str
    path: str
    filename: str


@dataclass(frozen=True)
class StorageLocation:
    """A storage the device declares for a kind of files besides its own memory
    (StorageLocationsExtension/Location): its memory card, as MTP names it (`mtp_id`, e.g.
    "Memory Card") and as the label of its drive in mass storage mode (`ums_id`)."""

    name: str  # what it holds, e.g. "Map"
    mtp_id: str
    ums_id: str = ""
    supported_format: str = ""  # e.g. "FAT32"
    min_size: int | None = None  # bytes
    max_size: int | None = None


@dataclass
class GarminDevice:
    unit_id: int
    part_number: str
    software_version: int
    description: str
    files: list[DeviceFile] = field(default_factory=list)
    update_files: list[UpdateFile] = field(default_factory=list)
    iq_max_apps: int | None = None
    iq_app_space: int | None = None
    storage_locations: list[StorageLocation] = field(default_factory=list)
    raw: bytes = b""

    @property
    def software_version_str(self) -> str:
        return f"{self.software_version // 100}.{self.software_version % 100:02d}"

    def datatypes(self) -> list[str]:
        seen: dict[str, None] = {}
        for f in self.files:
            seen.setdefault(f.datatype)
        return list(seen)

    def sources(self, datatype: str) -> list[DeviceFile]:
        return [f for f in self.files if f.datatype == datatype and f.readable]

    def targets(self, datatype: str) -> list[DeviceFile]:
        return [f for f in self.files if f.datatype == datatype and f.writable]


def _text(el: ET.Element | None, tag: str, default: str = "") -> str:
    if el is None:
        return default
    child = el.find(NS + tag)
    return child.text.strip() if child is not None and child.text else default


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _external_path(location: ET.Element | None) -> str | None:
    """The ExternalPath of a <Location> (its DataTypeLocationExtension, in a namespace of its
    own), "/" for "\\" and without slashes at its ends; None when it has none."""
    if location is None:
        return None
    for el in location.iter():
        if _local(el.tag) == "ExternalPath" and el.text and el.text.strip():
            return el.text.strip().replace("\\", "/").strip("/")
    return None


def _size(value: str | None) -> int | None:
    return int(value) if value and value.strip().isdecimal() else None


def _storage_locations(root: ET.Element) -> list[StorageLocation]:
    out = []
    for ext in root.iter():
        if _local(ext.tag) != "StorageLocationsExtension":
            continue
        for loc in ext:
            if _local(loc.tag) == "Location" and loc.get("MTPId"):
                out.append(StorageLocation(
                    name=loc.get("Name", ""), mtp_id=loc.get("MTPId", ""),
                    ums_id=loc.get("UMSId", ""), supported_format=loc.get("SupportedFormat", ""),
                    min_size=_size(loc.get("MinSize")), max_size=_size(loc.get("MaxSize"))))
    return out


def _component_version(major: str, minor: str) -> str:
    """A component's version as Garmin's update server writes it ("2.04"): some devices write
    the minor part without its zero ("4"), others with it ("04"). Anything that is no number
    stays as written."""
    return f"{major}.{int(minor):02d}" if minor.isdecimal() else f"{major}.{minor}"


def parse(data: bytes) -> GarminDevice:
    root = ET.fromstring(data)
    model = root.find(NS + "Model")
    dev = GarminDevice(
        unit_id=int(_text(root, "Id", "0")),
        part_number=_text(model, "PartNumber"),
        software_version=int(_text(model, "SoftwareVersion", "0") or 0),
        description=_text(model, "Description"),
        raw=data,
    )
    msm = root.find(NS + "MassStorageMode")
    if msm is not None:
        for dt in msm.findall(NS + "DataType"):
            name = _text(dt, "Name")
            for f in dt.findall(NS + "File"):
                loc = f.find(NS + "Location")
                dev.files.append(
                    DeviceFile(
                        datatype=name,
                        direction=_text(f, "TransferDirection"),
                        path=_text(loc, "Path"),
                        basename=_text(loc, "BaseName"),
                        extension=_text(loc, "FileExtension"),
                        external_path=_external_path(loc),
                    )
                )
        for uf in msm.findall(NS + "UpdateFile"):
            ver = uf.find(NS + "Version")
            dev.update_files.append(
                UpdateFile(
                    part_number=_text(uf, "PartNumber"),
                    version=_component_version(_text(ver, "Major"), _text(ver, "Minor")),
                    path=_text(uf, "Path"),
                    filename=_text(uf, "FileName"),
                )
            )
    for el in root.iter():
        if el.tag.endswith("}IQAppExt"):
            for child in el:
                if child.tag.endswith("}MaxApps") and child.text:
                    dev.iq_max_apps = int(child.text)
                elif child.tag.endswith("}AppSpace") and child.text:
                    dev.iq_app_space = int(child.text)
    dev.storage_locations = _storage_locations(root)
    return dev

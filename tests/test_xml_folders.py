"""The folders a device's GarminDevice.xml takes files in, each with the route the module that
writes there gives it, against the folders the device hides: Connect IQ apps (ciq.Operation:
a folder the listing shows the ordinary way, one it does not through openExpress's own
session), music (music.media_dir), courses (courses.gpx_target), TrueUp (trueup.DATATYPE) and
the files of Garmin Connect's queue (sync.plan_downloads: any other data type, written to the
first folder the XML gives it). Connect IQ has a rule for a hidden folder; music, courses and
TrueUp write to folders a device shows. Garmin Connect's queue has none: a known limit, kept on
purpose - it writes any data type the ordinary way, so a data type whose folder the device hides
would take the ordinary write; the queue sends FIT files only (FIT_TYPE_…), none of which goes
to a folder any of these devices hides.

The data: the made-up device of tests/data/write_routes/ always; the real devices of
testdata/write-routes/ that carry the folders of their GarminDevice.xml (made with the private
tool of the readings). Folder names only."""

import json
from pathlib import Path

import pytest
from fakes import hidden_at

from openexpress import ciq, courses, music, sync, trueup
from openexpress.device.storage import LocalStorage
from openexpress.device.xmldevice import INPUT, DeviceFile, GarminDevice

ROOT = Path(__file__).resolve().parent.parent
MADE_UP = sorted((ROOT / "tests" / "data" / "write_routes").glob("*.json"))
REAL = [p for p in sorted((ROOT / "testdata" / "write-routes").glob("*.json"))
        if "xml_targets" in json.loads(p.read_text(encoding="utf-8"))]

ORDINARY, OWN_SESSION, NOT_WRITTEN = "ordinary", "own session", "not written here"


def _device(data: dict) -> GarminDevice:
    device = GarminDevice(unit_id=0, part_number="006-B0000-00", software_version=100,
                          description="Made-up Device")
    for datatype, folders in data["xml_targets"].items():
        for folder in folders:  # in the XML's order: a module takes the first
            device.files.append(DeviceFile(datatype=datatype, direction=INPUT, path=folder))
    return device


def _storage(root: Path, data: dict, device: GarminDevice) -> LocalStorage:
    """The device as the ordinary listing shows it: every folder the XML names that it does
    not hide; openExpress's own session can be had."""
    hidden = [h["folder"] for h in data["hidden"]]
    (root / "GARMIN").mkdir(parents=True, exist_ok=True)
    for f in device.files:
        if hidden_at(f.path, hidden) is None:
            (root / f.path).mkdir(parents=True, exist_ok=True)
    storage = LocalStorage(root)
    storage.exclusive_ptp = lambda: object()
    return storage


def _queue(data: dict, device: GarminDevice) -> list:
    """What Garmin Connect's queue would send `device`: one message of each data type its
    GarminDevice.xml takes files in, as sync.plan_downloads plans it."""
    return sync.plan_downloads(device, [
        {"messageid": n, "devicexmldatatype": dt,
         "metadata": {"messagename": dt, "messageurl": "https://example.org/x"}}
        for n, dt in enumerate(data["xml_targets"])])


def routes(data: dict, root: Path) -> dict[str, dict[str, str]]:
    """Each folder the modules write to on this device: the route of each module there."""
    device = _device(data)
    storage = _storage(root, data, device)
    out: dict[str, dict[str, str]] = {}

    def route(module: str, folder: str | None, how: str) -> None:
        if folder:
            out.setdefault(folder.strip("/"), {})[module] = how

    op = ciq.Operation(device, storage)
    for datatype in [*ciq.DATATYPE_TO_APPTYPE, ciq.CONFIG_DATATYPE, ciq.SETTINGS_DATATYPE]:
        state = op.folder(datatype)
        route("Connect IQ", ciq.app_dir(device, datatype),
              ORDINARY if state == ciq.READY else
              OWN_SESSION if state == ciq.DIRECT else NOT_WRITTEN)
    for kind in music.supported_kinds(device):
        route("music", music.media_dir(device, kind), ORDINARY)
    playlist = device.targets(music.PLAYLIST)
    route("music", playlist[0].path if playlist else None, ORDINARY)
    gpx = courses.gpx_target(device)
    route("courses", gpx.path if gpx else None, ORDINARY)
    true_up = device.targets(trueup.DATATYPE)
    route("TrueUp", true_up[0].path if true_up else None, ORDINARY)
    for item in _queue(data, device):
        if item.target is not None and not item.handled_by:
            route(QUEUE, item.target.path, ORDINARY)
    return out


QUEUE = "Garmin Connect's queue"


def _wrong(data: dict, root: Path, queue: bool = True) -> list[str]:
    hidden = [h["folder"] for h in data["hidden"]]
    return [f"{folder} ({module}): the ordinary write, into {at}, which the device hides"
            for folder, by_module in sorted(routes(data, root).items())
            if (at := hidden_at(folder, hidden)) is not None
            for module, how in sorted(by_module.items())
            if how == ORDINARY and (queue or module != QUEUE)]


def queue_into_hidden(data: dict) -> list[str]:
    """The data types Garmin Connect's queue would write into a folder the device hides."""
    hidden = [h["folder"] for h in data["hidden"]]
    return sorted(item.datatype for item in _queue(data, _device(data))
                  if item.target is not None and not item.handled_by
                  and hidden_at(item.target.path, hidden) is not None)


@pytest.mark.parametrize("path", MADE_UP, ids=[p.stem for p in MADE_UP])
def test_the_made_up_device_s_xml_folders(path, tmp_path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert routes(data, tmp_path) == data["expected_xml"]


def test_a_queue_file_into_a_hidden_folder_is_caught(tmp_path):
    """The guard itself: a data type of Garmin Connect's queue whose folder the device hides
    would take the ordinary write, and the test says so."""
    data = {"hidden": [{"folder": "GARMIN/Secret", "xml": "names the folder"}],
            "xml_targets": {"FIT_TYPE_5": ["GARMIN/Secret"], "FIT_TYPE_6": ["Garmin/NewFiles"]}}
    assert _wrong(data, tmp_path) == [
        ("GARMIN/Secret (Garmin Connect's queue): the ordinary write, into GARMIN/Secret, which "
         "the device hides")]


@pytest.mark.skipif(not REAL, reason="no folders of a real device's GarminDevice.xml in "
                                     "testdata/write-routes/")
@pytest.mark.parametrize("path", REAL, ids=[p.stem for p in REAL])
def test_no_module_with_a_rule_writes_a_real_device_s_hidden_folder_the_ordinary_way(path,
                                                                                    tmp_path):
    data = json.loads(path.read_text(encoding="utf-8"))
    wrong = _wrong(data, tmp_path, queue=False)
    assert wrong == [], f"{data['device']} {data['software']}: " + "; ".join(wrong)


@pytest.mark.skipif(not REAL, reason="no folders of a real device's GarminDevice.xml in "
                                     "testdata/write-routes/")
@pytest.mark.parametrize("path", REAL, ids=[p.stem for p in REAL])
def test_a_known_limit_of_the_queue_only_data_types_it_never_sends_go_to_hidden_folders(path):
    """A fēnix 8 (24.20) gives PROMPT_FILE and TranslatedText folders it hides; the queue sends
    FIT files: none of those may go into a hidden folder."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [dt for dt in queue_into_hidden(data) if dt.startswith("FIT_TYPE_")] == []


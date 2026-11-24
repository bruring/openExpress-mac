"""A reinstall whose version on the device is higher than the one Garmin offers: decided by the file,
not by the numbers alone. The same file (size and MD5; in a folder the device hides, the size): it
may be reinstalled, and the row says both numbers. Another file: an older version, not reinstalled.
Not compared (its file not there, its folder not read): not reinstalled either.

The Emoji Font of an Edge 1050 is the real case of "the same file": its GarminDevice.xml says 2.10,
Garmin offers 2.00, and the file on the device is the server's 2.00 byte for byte (its size and MD5
below, as the device gave them back). Everything else is made up."""

import types

import pytest
from fakes import in_language

from openexpress import download, updates
from openexpress.cli import main
from openexpress.connect import GarminClient, lower_keys
from openexpress.device.storage import LocalStorage
from openexpress.device.xmldevice import UpdateFile

# The Edge 1050's Emoji Font, as its GarminDevice.xml lists it and the server offers it.
EMOJI = types.SimpleNamespace(
    part="006-D8168-19", name="Emoji Font, Twemoji Extended 20px RGB565", path="Garmin/FNT",
    filename="006-D8168-19.fnt", on_device="2.10", offered="2.00", size=3_151_801,
    md5="83fc159be5944c7e0afc23ad90582511")


def _answer(*entries):
    return lower_keys({"updates": list(entries)})


def _entry(part, name, path, filename, size, md5, display=None, semantic=None, reinstall=True):
    return {
        "partNumber": part, "productName": name, "displayVersion": display,
        "semanticVersion": semantic, "severity": "Recommended", "dataType": "Font",
        "deliverable": {"downloadUrl": "https://example.org/" + filename, "md5": md5,
                        "sizeInBytes": size},
        "installation": {"path": path, "fileName": filename, "isReinstall": reinstall,
                         "isProductInstalled": True, "order": 0},
    }


def _emoji_entry(**more):
    return _entry(EMOJI.part, EMOJI.name, EMOJI.path, EMOJI.filename, EMOJI.size,
                  EMOJI.md5.upper(), display=EMOJI.offered, **more)


def _device(*components):
    """A made-up Edge 1050 whose GarminDevice.xml lists `components` (part, version, path,
    file name)."""
    return types.SimpleNamespace(
        part_number="006-B4440-00", software_version_str="33.21", unit_id=None,
        update_files=[UpdateFile(part_number=p, version=v, path=path, filename=f)
                      for p, v, path, f in components])


EMOJI_LISTED = (EMOJI.part, EMOJI.on_device, EMOJI.path, EMOJI.filename)


@pytest.fixture
def edge(tmp_path):
    """The made-up Edge's storage, with a font file of the Emoji Font's size (its bytes are not
    Garmin's: what the device gives back is told by edge_md5)."""
    root = tmp_path / "device"
    (root / "Garmin" / "FNT").mkdir(parents=True)
    with open(root / "Garmin" / "FNT" / EMOJI.filename, "wb") as f:
        f.truncate(EMOJI.size)
    return LocalStorage(root)


@pytest.fixture
def edge_md5(monkeypatch):
    """The MD5 of a file read back from the device: the Emoji Font's, as the Edge gave it."""
    def md5_of(storage, real, size, mtime, told=None):
        return told_md5.get(real, "0" * 32)

    told_md5 = {f"{EMOJI.path}/{EMOJI.filename}": EMOJI.md5}
    monkeypatch.setattr(updates, "_md5_of", md5_of)
    return told_md5


def _offered(storage, answer, device):
    return updates.offered(storage, answer, device, forget_installed=False)


# --- the versions, by their numbers --------------------------------------------------------------

@pytest.mark.parametrize(("text", "number"), [
    ("2.40", (2, 40)), ("2.04", (2, 4)), ("2.4.0", (2, 4)), ("2.20.0", (2, 20)),
    ("2.70.9999", (2, 70)), ("43.00", (43, 0)), ("beta", None), ("", None), (None, None)])
def test_a_version_as_a_number(text, number):
    assert updates.version_number(text) == number


@pytest.mark.parametrize(("text", "shown"), [
    ("2.20.0", "2.20"), ("2.4.0", "2.04"), ("2.40", "2.40"), ("9.00", "9.00"), ("beta", "beta")])
def test_a_version_shown_as_the_device_writes_it(text, shown):
    assert updates.plain_version(text) == shown


# --- the same file, another file, not compared -------------------------------------------------------

def test_the_edges_emoji_font_is_the_same_file(edge, edge_md5):
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    assert u.is_reinstall and u.installed_version == "2.10" and u.version == "2.00"
    assert u.older_offer == updates.OLDER_SAME_FILE and not updates.reinstall_refused(u)
    assert updates.older_offer_text(u) == "On the device 2.10, Garmin offers 2.00: the same file."


def test_another_size_is_an_older_version_not_reinstalled(edge, edge_md5):
    (edge.root / "Garmin" / "FNT" / EMOJI.filename).write_bytes(b"another font")
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    assert u.older_offer == updates.OLDER_DIFFERS and updates.reinstall_refused(u)
    assert updates.older_offer_text(u) == (
        "On the device 2.10, Garmin offers 2.00, an older version: not reinstalled.")


def test_another_file_of_the_same_size_is_an_older_version(edge, edge_md5):
    edge_md5.clear()  # the device gives back other bytes
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    assert u.older_offer == updates.OLDER_DIFFERS and updates.reinstall_refused(u)


def test_an_older_version_whose_file_is_not_there_is_not_compared(edge, edge_md5):
    (edge.root / "Garmin" / "FNT" / EMOJI.filename).unlink()
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    assert u.older_offer == updates.OLDER_NOT_COMPARED and updates.reinstall_refused(u)
    assert updates.older_offer_text(u) == (
        "On the device 2.10, Garmin offers 2.00, an older version; the file on the device could "
        "not be compared: not reinstalled.")


def test_the_servers_three_part_version_is_shown_as_the_device_writes_it(edge, edge_md5):
    entry = _emoji_entry()
    entry["displayVersion"], entry["semanticVersion"] = None, "2.0.0"
    (u,) = _offered(edge, _answer(entry), _device(EMOJI_LISTED))
    assert u.version == "2.0.0" and u.older_offer == updates.OLDER_SAME_FILE
    assert updates.older_offer_text(u) == "On the device 2.10, Garmin offers 2.00: the same file."


@pytest.mark.parametrize("on_device", ["2.00", "1.90", "2.0", "beta"])
def test_the_same_or_an_older_version_on_the_device_changes_nothing(edge, edge_md5, on_device):
    listed = (EMOJI.part, on_device, EMOJI.path, EMOJI.filename)
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(listed))
    assert u.older_offer is None and not updates.reinstall_refused(u)
    assert updates.older_offer_text(u) is None


def test_an_update_offered_is_never_taken_for_an_older_version(edge, edge_md5):
    (u,) = _offered(edge, _answer(_emoji_entry(reinstall=False)), _device(EMOJI_LISTED))
    assert u.older_offer is None


def test_a_file_read_back_once_is_not_read_again(edge, edge_md5, monkeypatch):
    """The MD5 only of the files of the size offered; the device is asked once per file."""
    asked = []
    md5_of = updates._md5_of

    def counted(storage, real, size, mtime, told=None):
        asked.append(real)
        return md5_of(storage, real, size, mtime, told)

    monkeypatch.setattr(updates, "_md5_of", counted)
    _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    assert asked == [f"{EMOJI.path}/{EMOJI.filename}"]


# --- nothing older is ever written ----------------------------------------------------------------

def test_the_install_refuses_an_older_version_before_anything(edge, edge_md5, monkeypatch):
    monkeypatch.setattr(download, "fetch", lambda *a, **kw: pytest.fail("downloaded"))
    edge_md5.clear()
    (u,) = _offered(edge, _answer(_emoji_entry()), _device(EMOJI_LISTED))
    with pytest.raises(updates.UpdateError) as refused:
        updates.install([u], edge)
    assert str(refused.value) == (
        "On the device 2.10, Garmin offers 2.00, an older version: not reinstalled.")


# --- the command line -------------------------------------------------------------------------------

@pytest.fixture
def edge_cli(edge, edge_md5, monkeypatch):
    from openexpress import cli

    (edge.root / "Garmin" / "GarminDevice.xml").write_text(DEVICE_XML)
    monkeypatch.setattr(cli, "open_storage", lambda args: edge)
    monkeypatch.setattr(GarminClient, "unit_info", lambda self, unit_id, part_number: {})

    def answer(*entries):
        monkeypatch.setattr(GarminClient, "software_updates",
                            lambda self, raw, include_reinstalls=False: _answer(*entries))
    return answer


DEVICE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<Device xmlns="http://www.garmin.com/xmlschemas/GarminDevice/v2">
 <Model><PartNumber>006-B4440-00</PartNumber><SoftwareVersion>3321</SoftwareVersion>
  <Description>Edge 1050</Description></Model>
 <Id>1234</Id>
 <MassStorageMode>
  <UpdateFile><PartNumber>{EMOJI.part}</PartNumber><Version><Major>2</Major><Minor>10</Minor></Version>
   <Path>{EMOJI.path}</Path><FileName>{EMOJI.filename}</FileName></UpdateFile>
 </MassStorageMode>
</Device>
"""


def test_the_command_line_says_the_same_file_and_reinstalls_it(edge_cli, capsys):
    edge_cli(_emoji_entry())
    assert main(["updates", "--reinstalls"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "      On the device 2.10, Garmin offers 2.00: the same file." in out
    assert main(["updates", "--reinstalls", "--install", EMOJI.part]) == 0  # a dry run
    assert "To install:" in capsys.readouterr().out


def test_the_command_line_refuses_an_older_version(edge_cli, edge_md5, capsys, monkeypatch):
    monkeypatch.setattr(download, "fetch", lambda *a, **kw: pytest.fail("downloaded"))
    edge_md5.clear()
    edge_cli(_emoji_entry())
    assert main(["updates", "--reinstalls"]) == 0
    line = "On the device 2.10, Garmin offers 2.00, an older version: not reinstalled."
    assert "      " + line in capsys.readouterr().out.splitlines()
    assert main(["updates", "--reinstalls", "--install", EMOJI.part, "--apply"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert line in out and "To install:" not in out


# --- the words in Polish --------------------------------------------------------------------------

@pytest.mark.parametrize(("state", "en", "pl"), [
    ("OLDER_SAME_FILE",
     "On the device 2.10, Garmin offers 2.00: the same file.",
     "Na urządzeniu 2.10, Garmin oferuje 2.00: ten sam plik."),
    ("OLDER_SAME_SIZE",
     "On the device 2.10, Garmin offers 2.00: the same size, its contents not compared.",
     "Na urządzeniu 2.10, Garmin oferuje 2.00: ten sam rozmiar, treści nie porównano."),
    ("OLDER_DIFFERS",
     "On the device 2.10, Garmin offers 2.00, an older version: not reinstalled.",
     "Na urządzeniu 2.10, Garmin oferuje 2.00, starszą wersję: bez ponownej instalacji."),
    ("OLDER_NOT_COMPARED",
     ("On the device 2.10, Garmin offers 2.00, an older version; the file on the device could "
      "not be compared: not reinstalled."),
     ("Na urządzeniu 2.10, Garmin oferuje 2.00, starszą wersję; pliku na urządzeniu nie udało "
      "się porównać: bez ponownej instalacji.")),
])
def test_the_words_in_both_languages(state, en, pl):
    u = types.SimpleNamespace(older_offer=getattr(updates, state), installed_version="2.10",
                              version="2.00")
    assert updates.older_offer_text(u) == en
    with in_language("pl"):
        assert updates.older_offer_text(u) == pl

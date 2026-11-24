"""Garmin's listing (0x9002) joined to the ordinary one (0x1007): ptp.Session.all_children.

What Garmin's listing names in another folder is left out; at the top level a device names 0 or
ROOT as the parent there (the fēnix 8 and the Edge 1050 name 0, though the listings are asked
with ROOT). Between the two listings there is one entry for a name in any letter case: Garmin's
object takes the place of the ordinary one of that name, with its handle and description, and
is not taken for hidden, as the ordinary listing shows it. What one listing holds twice stays as
it is (a car navigator may have two folders whose names differ only in letter case). A device
without Garmin's listing gives the ordinary one alone. Everything in the tree is made up.
"""

import pytest
from ptp_fake import STORAGE, FakeDevice

from openexpress.device import ptp


@pytest.fixture
def device():
    """A watch that answers as the fēnix 8 did: Garmin's listing gives only what the ordinary
    one does not, each object in the folder asked, and the top level names 0 as its parent."""
    fake = FakeDevice()
    fake.add("GARMIN/GarminDevice.xml", b"<Device/>")
    fake.add("GARMIN/SQL/Visible.db", b"seen")
    fake.add("GARMIN/SQL/Hidden.db", b"unseen", hidden=True)
    fake.add("GARMIN/Prompts", hidden=True)
    fake.add("GARMIN/Prompts/voice.bin", b"voice", hidden=True)
    fake.add("GARMIN/Apps/OUT.BIN", b"state")
    fake.add("Otd", hidden=True)  # a folder of the top level the ordinary listing does not show
    return fake


def _parent(device, path: str) -> int:
    return device.handle(path) if path else ptp.ROOT


def _listed(session, device, path: str, at_root: bool = False) -> list[tuple]:
    return [(o.name, o.handle, o.hidden)
            for o in session.all_children(STORAGE, _parent(device, path), at_root=at_root)]


# --- as the fēnix 8 and the Edge 1050 answer: nothing in both listings ---------------------

@pytest.mark.parametrize("top_parent", [0, ptp.ROOT], ids=["top names 0", "top names ROOT"])
@pytest.mark.parametrize(("path", "at_root"), [("GARMIN/SQL", False), ("GARMIN", False),
                                               ("GARMIN/Prompts", False), ("GARMIN/Apps", False),
                                               ("", True)],
                         ids=["files of both", "a hidden folder", "only Garmin's",
                              "only ordinary", "the top level"])
def test_with_nothing_in_both_the_ordinary_objects_come_first_then_garmins(device, top_parent,
                                                                           path, at_root):
    device.top_parent = top_parent
    with ptp.Session(device) as session:
        parent = _parent(device, path)
        expected = ([(o.name, o.handle, False) for o in session.children(STORAGE, parent)]
                    + [(o.name, o.handle, True)
                       for o in session.hidden_children(STORAGE, parent)])
        listed = _listed(session, device, path, at_root)
    assert listed == expected and listed


def test_a_fenix_like_watch_lists_seen_objects_then_hidden_ones(device):
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
        prompts = _listed(session, device, "GARMIN/Prompts")
        top = _listed(session, device, "", at_root=True)
        top_ordinary = _listed(session, device, "")
    assert [(name, hidden) for name, _h, hidden in sql] == [("Visible.db", False),
                                                            ("Hidden.db", True)]
    assert [(name, hidden) for name, _h, hidden in prompts] == [("voice.bin", True)]
    assert [(name, hidden) for name, _h, hidden in top] == [("GARMIN", False), ("Otd", True)]
    assert [name for name, _h, _hidden in top_ordinary] == ["GARMIN"]  # 0x9002 not asked


def test_a_device_without_garmins_listing_gives_the_ordinary_one(device):
    device.supports_hidden = False
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
        assert session.other_parent == 0
    assert [(name, hidden) for name, _h, hidden in sql] == [("Visible.db", False)]


# --- an object in both listings ------------------------------------------------------------

def test_an_object_in_both_under_one_handle_is_one_entry_not_hidden(device):
    seen = device.handle("GARMIN/SQL/Visible.db")
    device.also_in_garmins.add(seen)
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
    assert sql == [("Visible.db", seen, False),
                   ("Hidden.db", device.handle("GARMIN/SQL/Hidden.db"), True)]


def test_an_object_in_both_under_two_handles_is_garmins_in_the_ordinary_ones_place(device):
    twin = device.add_twin("GARMIN/SQL/Visible.db", data=b"seen again")
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
        found = session.lookup(STORAGE, "garmin/sql/visible.db")
    assert sql == [("Visible.db", twin, False),
                   ("Hidden.db", device.handle("GARMIN/SQL/Hidden.db"), True)]
    assert (found.handle, found.size, found.hidden) == (twin, len(b"seen again"), False)


def test_names_that_differ_only_in_letter_case_are_one_entry_with_garmins_name(device):
    twin = device.add_twin("GARMIN/SQL/Visible.db", name="VISIBLE.DB")
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
    assert sql == [("VISIBLE.DB", twin, False),
                   ("Hidden.db", device.handle("GARMIN/SQL/Hidden.db"), True)]


def test_two_ordinary_folders_whose_names_differ_only_in_letter_case_stay_two(device):
    device.add("GARMIN/sql/Other.db", b"other")
    with ptp.Session(device) as session:
        garmin = _listed(session, device, "GARMIN")
    ordinary = [name for name, _h, hidden in garmin if not hidden]
    assert ordinary.count("SQL") == 1 and ordinary.count("sql") == 1


# --- what Garmin's listing names in another folder -----------------------------------------

def test_an_object_of_another_folder_is_left_out_and_counted(device):
    stray = device.handle("GARMIN/Prompts/voice.bin")
    device.garmins_extra[device.handle("GARMIN/SQL")] = [stray]
    with ptp.Session(device) as session:
        sql = _listed(session, device, "GARMIN/SQL")
        left_out = session.other_parent
    assert [name for name, _h, _hidden in sql] == ["Visible.db", "Hidden.db"]
    assert left_out == 1


@pytest.mark.parametrize("top_parent", [0, ptp.ROOT], ids=["top names 0", "top names ROOT"])
def test_at_the_top_level_0_and_root_both_mean_the_top(device, top_parent):
    device.top_parent = top_parent
    device.garmins_extra[ptp.ROOT] = [device.handle("GARMIN/Prompts/voice.bin")]
    with ptp.Session(device) as session:
        top = _listed(session, device, "", at_root=True)
        left_out = session.other_parent
    assert [(name, hidden) for name, _h, hidden in top] == [("GARMIN", False), ("Otd", True)]
    assert left_out == 1

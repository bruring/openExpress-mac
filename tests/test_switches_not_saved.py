"""A switch of the window whose change cannot be saved at all (a file of this computer not
writable, the device refusing the app list, the background service not turned on): the change
is tried once, the switch goes back and the window says why, once; an Adw.SwitchRow may tell
a change made by its handler again though that handler is blocked. Each refusal below has a
fuse: the tenth save goes through, so that the test ends even where the switch would go on
forever. All apps, devices and settings are made up."""

import sys
import types
import uuid

import pytest
from fakes import RunNow, apps_page, descendants, device_session, window_methods
from fakes import window as stand_in

from openexpress import ciq, satellite
from openexpress.device.storage import DeviceError
from openexpress.gui import gui_settings

UNIT = 3000000001
REFUSED = "The device did not take the app list"
# What the window says for a switch whose file cannot be written (as Keep Diagnostic Files).
NOT_SAVED = ("Setting Not Saved", "Permission denied")


def refused_file(saves: list, value) -> None:
    """A save of this computer's file noted; refused as the system refuses a file it cannot
    write, but for the fuse."""
    saves.append(value)
    if len(saves) < 10:
        raise PermissionError(13, "Permission denied", "/made-up/openexpress/state.json.tmp")


@pytest.fixture
def uncaught(monkeypatch):
    """The exceptions no handler caught, by type: PyGObject hands those of a signal's handler
    to sys.excepthook, which the window points at its log."""
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, _e, _tb: seen.append(kind))
    return seen


@pytest.fixture
def gui_json_refused(monkeypatch):
    """gui.json cannot be written: what each save would have written, noted."""
    saves = []
    monkeypatch.setattr(gui_settings, "save_gui_settings",
                        lambda data: refused_file(saves, dict(data)))
    return saves


def switch_rows(widget) -> list:
    from gi.repository import Adw

    return [w for w in descendants(widget) if isinstance(w, Adw.SwitchRow)]


# --- the switch of a built-in app (the Apps page): the device's app list --------------------------


@pytest.mark.needs_display
def test_an_app_switch_never_saved_goes_back_once_and_asks_no_more(monkeypatch):
    """The device refuses every app list: the switch goes back once and the window says it
    once."""
    said = []
    built_in = ciq.InstalledApp(app_id=uuid.UUID(f"{ciq.PRELOADED_PREFIX}-0000-0000-000000000008"),
                                app_type=8, name="Softball", disabled=True)
    _win, section = apps_page([built_in], run_task=RunNow(
        error=lambda title, text: said.append((title, text))))
    saves = []

    def refused(device, storage, app_id, enabled, app_type=None):
        saves.append(enabled)
        if len(saves) < 10:
            raise DeviceError(REFUSED)

    monkeypatch.setattr(ciq, "set_enabled", refused)
    (switch,) = switch_rows(section.stack)
    switch.set_active(True)
    assert (saves, said, switch.get_active()) == ([True], [("Change Not Saved", REFUSED)], False)


# --- the switch Satellite Data (the device page): its state file on this computer ----------------


@pytest.mark.needs_display
def test_satellite_data_never_saved_goes_back_once_and_says_why(clock, monkeypatch, uncaught):
    """The state file cannot be written: one try, the switch back, the window says why."""
    from openexpress.gui.device_page import satellite_row, show_satellite

    saves, said = [], []
    monkeypatch.setattr(satellite, "save", lambda unit_id, state: refused_file(saves, state.on))
    win = stand_in(session=device_session(device=types.SimpleNamespace(unit_id=UNIT),
                                          satellite=satellite.State()),
                   error=lambda title, text: said.append((title, text)))
    switch = win.satellite_row = satellite_row(win)
    show_satellite(win)
    switch.set_active(True)
    assert (saves, said, switch.get_active(), uncaught) == ([True], [NOT_SAVED], False, [])


# --- the switch Colours under an activity's map: gui.json -----------------------------------------


@pytest.mark.needs_display
def test_track_colours_never_saved_go_back_once_and_say_why(gui_json_refused, uncaught):
    """gui.json cannot be written: one try, the switch back, its window says why."""
    from gi.repository import Gtk

    from openexpress.gui import activity_map

    shown = activity_map.ActivityMap(
        activity_map.Track((0, 600), ((51.5, -0.16), (51.51, -0.16))))
    said = []
    window = Gtk.Window(child=shown)  # the window of the page, as the switch finds it
    window.error = lambda title, text: said.append((title, text))
    shown.legend.switch.set_active(False)
    got = (gui_json_refused, said, shown.legend.switch.get_active(), uncaught)
    window.destroy()
    assert got == ([{"track_colours": False}], [NOT_SAVED], True, [])


# --- the switch Streets and Terrain From OpenStreetMap (Preferences): gui.json --------------------


@pytest.mark.needs_display
def test_the_openstreetmap_switch_never_saved_goes_back_once_and_says_why(gui_json_refused,
                                                                        uncaught):
    """gui.json cannot be written: one try, the switch back, the window says why."""
    from openexpress.gui.preferences import maps_group

    said = []
    (switch,) = switch_rows(maps_group(stand_in(
        error=lambda title, text: said.append((title, text)))))
    switch.set_active(True)
    assert (gui_json_refused, said, switch.get_active(), uncaught) == (
        [{"map_tiles": True}], [NOT_SAVED], False, [])


# --- the switch Background Sync (the device page, Preferences): the service of the sync --------


@pytest.mark.needs_display
def test_background_sync_never_changed_goes_back_once_and_asks_no_more(monkeypatch):
    """The service cannot be turned on at all: the switch goes back once and the window says
    it once."""
    from openexpress import background

    tries = []

    def refused(enabled, done):
        tries.append(enabled)
        done(len(tries) >= 10, "Failed to enable the made-up unit")  # >= 10: the fuse

    monkeypatch.setattr(background, "is_enabled", lambda: False)
    monkeypatch.setattr(background, "set_enabled", refused)
    said = []
    win = window_methods(
        stand_in(gui_settings={}, error=lambda title, text: said.append((title, text)),
                 toast=lambda text, on_preferences=False: None),
        ("_auto_sync_row", "_background_enabled", "set_background"))
    switch = win._auto_sync_row()
    switch.set_active(True)
    assert (tries, said, switch.get_active()) == (
        [True], [("Background Sync Not Changed", "Failed to enable the made-up unit")], False)

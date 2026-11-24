"""The settings of the window (gui.json): each change reads the file again and changes only its
own keys, so the switch of the background sync keeps the start view chosen since the window
opened. The language of the interface is read and kept by one pair of functions (i18n), for
the window, the command line and the start."""

import json
import logging
import types

import pytest
from fakes import window as stand_in


def _window():
    from openexpress.gui.gui_settings import load_gui_settings

    shown = []
    win = stand_in(gui_settings=load_gui_settings(), session=None,
                   toast=shown.append, error=lambda *a: shown.append(a),
                   start=types.SimpleNamespace(view=None),
                   _render_start=lambda: None)
    return win, shown


def test_the_background_switch_keeps_the_view_chosen_since_the_window_opened(monkeypatch):
    from openexpress import background
    from openexpress.gui import app
    from openexpress.gui.gui_settings import load_gui_settings, save_gui_settings
    from openexpress.gui.start_window import StartView

    save_gui_settings({"window": {"width": 900, "height": 700, "maximized": False}})
    win, _shown = _window()  # the window opens: its settings as they were
    StartView.choose(win, "list")
    monkeypatch.setattr(background, "set_enabled", lambda enabled, done: done(True, ""))
    app.MainWindow.set_background(win, True)
    # Opened again: the view, the size and the switch, all kept.
    assert StartView.saved() == "list"
    assert load_gui_settings() == {
        "window": {"width": 900, "height": 700, "maximized": False}, "start_view": "list",
        "background": True}
    assert win.gui_settings["background"] is True  # what the window knows now


NOT_CHANGED = ("Background Sync Not Changed", "Background sync could not be turned on or off.")
NO_BACKGROUND = ("This desktop does not let Flatpak apps run in the background (its portal has "
                 "no Background service). Use the version from source to sync in the "
                 "background.")


def _portal_refusing(monkeypatch, code, words):
    """The Flatpak's portal answers the request for the background with the D-Bus error
    `code`; the window's words of it, and the log."""
    from gi.repository import Gio, GLib

    from openexpress import background
    from openexpress.gui import app

    class Bus:
        def get_unique_name(self):
            return ":1.42"

        def signal_subscribe(self, *args):
            return 7

        def signal_unsubscribe(self, subscription):
            pass

        def call_sync(self, *args):
            raise GLib.Error.new_literal(Gio.dbus_error_quark(), words, code)

    monkeypatch.setattr(background, "in_flatpak", lambda: True)
    monkeypatch.setattr(background.Gio, "bus_get_sync", lambda *args: Bus())
    win, shown = _window()
    app.MainWindow.set_background(win, True)
    return shown


def test_a_portal_that_refuses_is_said_in_the_programs_words(monkeypatch, caplog):
    """The words of D-Bus go to the log; the window says the program's."""
    from gi.repository import Gio

    with caplog.at_level(logging.INFO, logger="openexpress"):
        shown = _portal_refusing(
            monkeypatch, Gio.DBusError.ACCESS_DENIED,
            "GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: made-up refusal")
    assert shown == [NOT_CHANGED]
    assert any("AccessDenied" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("code, words", [
    ("UNKNOWN_METHOD", ("GDBus.Error:org.freedesktop.DBus.Error.UnknownMethod: No such "
                        "interface “org.freedesktop.portal.Background” on object at path "
                        "/org/freedesktop/portal/desktop")),
    ("SERVICE_UNKNOWN", ("GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name is "
                         "not activatable"))])
def test_a_desktop_without_background_for_flatpak_apps_says_so_and_what_helps(
        monkeypatch, caplog, code, words):
    """A desktop whose portal has no Background service (or no portal at all): the window says
    why and what to use instead; the words of D-Bus go to the log."""
    from gi.repository import Gio

    with caplog.at_level(logging.INFO, logger="openexpress"):
        shown = _portal_refusing(monkeypatch, getattr(Gio.DBusError, code), words)
    assert shown == [(NOT_CHANGED[0], NO_BACKGROUND)]
    assert any(words.split(":")[1] in r.getMessage() for r in caplog.records)


def test_a_background_switch_that_fails_is_said_in_the_programs_words(monkeypatch, caplog):
    """The words of an error nobody foresaw are not the user's: the log has it, with its
    traceback."""
    from openexpress import background
    from openexpress.gui import app

    def broken(enabled, done):
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(background, "set_enabled", broken)
    win, shown = _window()
    with caplog.at_level(logging.WARNING, logger="openexpress"):
        app.MainWindow.set_background(win, True)
    assert shown == [NOT_CHANGED]
    (logged,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert logged.getMessage() == "background sync not changed: RuntimeError"
    assert logged.exc_info is not None


def test_every_change_of_gui_json_keeps_the_other_keys():
    from openexpress.gui.gui_settings import (
        load_gui_settings,
        save_gui_settings,
        save_window_geometry,
        update_gui_settings,
    )
    from openexpress.gui.start_window import StartView

    save_gui_settings({"someone else's": 1})
    update_gui_settings(background=False)
    save_window_geometry(800, 600, True)
    StartView.save("grid")
    assert load_gui_settings() == {
        "someone else's": 1, "background": False, "start_view": "grid",
        "window": {"width": 800, "height": 600, "maximized": True}}


def test_one_way_for_the_language_of_the_interface(tmp_path):
    from openexpress import i18n, paths
    from openexpress.gui.preferences import load_ui_language, save_ui_language

    path = paths.config_dir() / "settings.json"
    paths.write_private(path, json.dumps({"environment": "production", "client_guid": "g"}))
    assert i18n.saved_ui_language() == ""
    i18n.save_ui_language("pl")
    assert json.loads(path.read_text()) == {"environment": "production", "client_guid": "g",
                                            "ui_language": "pl"}
    assert i18n.saved_ui_language() == "pl"
    assert load_ui_language is i18n.saved_ui_language
    assert save_ui_language is i18n.save_ui_language
    path.write_text("not json")
    assert i18n.saved_ui_language() == ""  # unreadable: the system's


def test_setup_keeps_the_language_of_the_interface(capsys):
    from openexpress import i18n
    from openexpress.cli import main

    i18n.save_ui_language("pl")
    assert main(["setup", "--environment", "production"]) == 0
    assert i18n.saved_ui_language() == "pl"
    try:
        assert main(["setup", "--ui-language", "system"]) == 0
        assert i18n.saved_ui_language() == ""
    finally:
        i18n.setup("en")

"""Tests the pages that say why they show nothing (a device in mass storage mode, not on the
account, no media): an Adw.StatusPage description is markup, so a text with "<" (an account's
name, a translation) would leave it empty; it is escaped and shown as it is. All texts are made
up."""

import types

import pytest
from fakes import window as stand_in

NETWORK = ("Could not check whether this device belongs to your Garmin account (Account device "
           "list: network error (no connection)). Nothing was synced.")
NOT_ON_THE_ACCOUNT = "This device is not registered to the Garmin account of <made> & up."


def _section(key):
    from openexpress.gui.device_page import Section

    return Section(key, key.title(), "x-symbolic", "Loading…")


def _shown(section, page="error") -> list[str]:
    """The texts of the labels of the page "error" (or "info"), as they are drawn."""
    from gi.repository import Gtk

    out, stack = [], [getattr(section, page)]
    while stack:
        widget = stack.pop()
        if isinstance(widget, Gtk.Label):
            out.append(widget.get_text())
        child = widget.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return out


@pytest.mark.needs_display
def test_the_apps_page_shows_an_ownership_error_with_brackets():
    from openexpress.connect import NotAccountDevice
    from openexpress.gui.app import MainWindow

    section = _section("apps")
    win = stand_in(sections={"apps": section}, session=types.SimpleNamespace(
        logged_in=True, ownership_error=NotAccountDevice(NOT_ON_THE_ACCOUNT)))
    MainWindow.load_apps(win)
    assert NOT_ON_THE_ACCOUNT in _shown(section)


@pytest.mark.needs_display
def test_the_apps_page_says_the_account_was_not_checked_in_the_words_of_the_banner():
    """Not the error of the sync, which was not made: the words of the banner above the page."""
    from openexpress.connect import OwnershipUnknown
    from openexpress.gui.app import MainWindow

    section = _section("apps")
    win = stand_in(
        sections={"apps": section}, _ownership_title=MainWindow._ownership_title,
        session=types.SimpleNamespace(logged_in=True, ownership_error=OwnershipUnknown(NETWORK)))
    MainWindow.load_apps(win)
    shown = _shown(section)
    assert "Could not check whether this device belongs to your Garmin account" in shown
    assert not any("Nothing was synced" in text for text in shown)


@pytest.mark.needs_display
def test_a_page_not_for_a_drive_shows_its_words_with_brackets(monkeypatch):
    """Its words as a translation gives them, "<" and "&" too."""
    from openexpress.gui import device_page
    from openexpress.gui.device_page import show_not_for_mass_storage

    words = "A drive <made up> & its words"
    monkeypatch.setattr(device_page, "_", lambda text: words)
    section = _section("maps")
    show_not_for_mass_storage(section)
    assert words in _shown(section, "info")


@pytest.mark.needs_display
def test_the_music_page_without_media_shows_its_words_with_brackets(monkeypatch):
    """Its words as a translation gives them, "<" and "&" too."""
    from openexpress import music
    from openexpress.gui import music_page
    from openexpress.gui.app import MainWindow

    words = "No media <made up> & more"
    monkeypatch.setattr(music, "supported_kinds", lambda device: [])
    monkeypatch.setattr(music_page, "_", lambda text, *args: words)
    section = _section("music")
    win = stand_in(sections={"music": section},
                   session=types.SimpleNamespace(device=None))
    MainWindow.load_music(win)
    assert words in _shown(section, "info")

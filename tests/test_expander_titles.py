"""The expandable rows of the Music and Apps pages are named by what they hold, a noun as every
other expandable row of the window ("Category Details", "Last Sync"), not by a verb that stays
the same once they are open: "Files" under the music of a kind (its group already says how many
and their size), "All Activities and Apps" with how many under it in the built-in ones. All
files and apps are made up."""

import types
import uuid

import pytest
from fakes import apps_page, descendants, device_session, in_language, page_window, window_methods

from openexpress import ciq, music
from openexpress.music import DeviceMedia

pytestmark = pytest.mark.needs_display

TRACKS = [DeviceMedia(f"Music/Someone/Album/Song {n}.mp3", 3_000_000, "track") for n in (1, 2)]


def _expander(section):
    from gi.repository import Adw

    (row,) = [w for w in descendants(section.page) if isinstance(w, Adw.ExpanderRow)]
    return row


def _music_page():
    win, section = page_window("music")
    targets = {music.KINDS["track"]: ["Music"], music.PLAYLIST: ["Music"]}
    dev = types.SimpleNamespace(targets=lambda datatype: targets.get(datatype, []), unit_id=None)
    win.session = device_session(device=dev, storage=None)
    win.choose_music = win.choose_playlist = lambda *a, **kw: None
    win.load_section = lambda key, force=False: None
    window_methods(win, ["show_music", "confirm_remove_music", "remove_music", "_removed_files"])
    win.show_music((TRACKS, []), free=None)
    return section


def _apps_page(count: int):
    _win, section = apps_page(ciq.InstalledApp(
        app_id=uuid.UUID(f"{ciq.PRELOADED_PREFIX}-0000-0000-{n:012d}"), app_type=8,
        name=f"Made-up Sport {n}", disabled=False) for n in range(1, count + 1))
    return section


@pytest.mark.parametrize(("language", "title"), [("en", "Files"), ("pl", "Pliki")])
def test_the_music_of_a_kind_is_under_files(language, title):
    with in_language(language):
        row = _expander(_music_page())
    assert (row.get_title(), row.get_subtitle()) == (title, "")


@pytest.mark.parametrize(("language", "count", "title", "subtitle"), [
    ("en", 1, "All Activities and Apps", "1 item"),
    ("en", 2, "All Activities and Apps", "2 items"),
    ("pl", 1, "Wszystkie aktywności i aplikacje", "1 element"),
    ("pl", 2, "Wszystkie aktywności i aplikacje", "2 elementy"),
    ("pl", 5, "Wszystkie aktywności i aplikacje", "5 elementów")])
def test_the_built_in_activities_and_apps_are_under_all_with_how_many(language, count, title,
                                                                      subtitle):
    with in_language(language):
        row = _expander(_apps_page(count))
    assert (row.get_title(), row.get_subtitle()) == (title, subtitle)

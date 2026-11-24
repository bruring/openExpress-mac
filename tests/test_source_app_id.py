"""The version from source has an ID of its own (paths.app_id): its window, its background
service and its menu entries never take the Flatpak's, nor hide them; the Flatpak's stay as they
were. What the version from source left under the Flatpak's ID is cleaned up at its start and by
install-desktop: only its own menu entries (their Exec= runs "-m openexpress") and icons that are
the program's own, byte for byte; an entry that runs the Flatpak and any other icon stay."""

import logging
import os
import types
from pathlib import Path

import pytest

from openexpress import cli, desktop, paths

FLATPAK_ID = "io.github.tomaszbojanowski.OpenExpress"
SOURCE_ID = FLATPAK_ID + ".Devel"
DATA = Path(desktop.__file__).parent / "data"
OURS = "Exec=env PYTHONPATH=/home/u/openExpress /usr/bin/python3 -m openexpress gui"
FLATPAK_RUN = (f"Exec=/usr/bin/flatpak run --branch=stable --arch=x86_64 --command=openexpress "
               f"{FLATPAK_ID} gui")


@pytest.fixture
def from_source(monkeypatch):
    """The version from source, as it runs: not in a Flatpak, its files below the test's own
    folders (conftest.private_dirs), no catalogs built into the source tree."""
    from openexpress import background

    monkeypatch.setattr(paths, "in_flatpak", lambda: False)
    monkeypatch.setattr(background, "in_flatpak", lambda: False)
    monkeypatch.setattr(desktop, "_source_root", lambda: None)
    monkeypatch.setattr(cli, "_build_translations", lambda: None)


def _data() -> Path:
    return paths.xdg("XDG_DATA_HOME", ".local/share")


def _put(path: Path, content: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def _entry(exec_line: str, background: bool = False) -> str:
    return (f"[Desktop Entry]\nType=Application\nName=openExpress\n{exec_line}\n"
            f"Icon={FLATPAK_ID}\n" + ("NoDisplay=true\n" if background else ""))


def _left_before(entry: str = OURS, icon: bytes | None = None) -> dict[str, Path]:
    """What the version from source wrote under the Flatpak's ID before it had an ID of its own:
    its menu entry, the entry of its background service and its two icons."""
    data = _data()
    return {
        "entry": _put(data / "applications" / f"{FLATPAK_ID}.desktop", _entry(entry)),
        "watch": _put(data / "applications" / f"{FLATPAK_ID}.Watch.desktop",
                      _entry(OURS, background=True)),
        "icon": _put(data / "icons" / "hicolor" / "scalable" / "apps" / f"{FLATPAK_ID}.svg",
                     icon if icon is not None else (DATA / f"{FLATPAK_ID}.svg").read_bytes()),
        "symbolic": _put(data / "icons" / "hicolor" / "symbolic" / "apps"
                         / f"{FLATPAK_ID}-symbolic.svg",
                         (DATA / f"{FLATPAK_ID}-symbolic.svg").read_bytes()),
    }


# --- the ID -----------------------------------------------------------------------------------


def test_the_version_from_source_has_an_id_of_its_own(monkeypatch):
    monkeypatch.setattr(paths, "in_flatpak", lambda: True)
    assert paths.app_id() == FLATPAK_ID
    monkeypatch.setattr(paths, "in_flatpak", lambda: False)
    assert paths.app_id() == SOURCE_ID


@pytest.mark.parametrize("flatpak, wanted", [(True, FLATPAK_ID), (False, SOURCE_ID)])
def test_the_window_and_the_watcher_take_the_id_of_this_install(monkeypatch, flatpak, wanted):
    from openexpress import watch
    from openexpress.gui import app

    monkeypatch.setattr(paths, "in_flatpak", lambda: flatpak)
    assert app.App().get_application_id() == wanted
    assert watch.Watcher().get_application_id() == wanted + ".Watch"


@pytest.mark.parametrize("flatpak, wanted", [(True, FLATPAK_ID), (False, SOURCE_ID)])
def test_background_sync_stops_the_watcher_of_this_install_only(monkeypatch, flatpak, wanted):
    """Turning background sync off stops the watcher of its own install only: in the Flatpak not
    that of the version from source, nor the other way round."""
    from openexpress import background

    monkeypatch.setattr(paths, "in_flatpak", lambda: flatpak)
    asked = []
    monkeypatch.setattr(background, "_watcher_actions", lambda name, path: asked.append(
        (name, path)) or types.SimpleNamespace(activate_action=lambda *_a: None))
    background.stop_watcher()
    assert asked == [(wanted + ".Watch", "/" + (wanted + ".Watch").replace(".", "/"))]


def test_the_icons_inside_the_window_keep_the_flatpaks_names():
    """They are the names of the files in openexpress/data, the same in both installs."""
    from openexpress.gui import start

    assert f"{FLATPAK_ID}-device-symbolic" == start.DEVICE_ICON
    assert (DATA / f"{start.DEVICE_ICON}.svg").is_file()


# --- the menu entries -------------------------------------------------------------------------


def test_the_files_of_the_version_from_source_take_its_own_id(from_source):
    out = {p.name: text for p, text in desktop.files().items()}
    assert sorted(out) == sorted([f"{SOURCE_ID}.desktop", f"{SOURCE_ID}.Watch.desktop",
                                  desktop.SERVICE, f"{SOURCE_ID}.svg",
                                  f"{SOURCE_ID}-symbolic.svg"])
    entry = out[f"{SOURCE_ID}.desktop"].splitlines()
    watch = out[f"{SOURCE_ID}.Watch.desktop"].splitlines()
    assert "Name=openExpress (source)" in entry and f"Icon={SOURCE_ID}" in entry
    assert "NoDisplay=true" not in entry
    assert "Name=openExpress (background)" in watch and f"Icon={SOURCE_ID}" in watch
    assert "NoDisplay=true" in watch
    # Its own icon files, the program's icons under its own name: the Flatpak's stay its own.
    assert out[f"{SOURCE_ID}.svg"] == (DATA / f"{FLATPAK_ID}.svg").read_text()
    assert out[f"{SOURCE_ID}-symbolic.svg"] == (DATA / f"{FLATPAK_ID}-symbolic.svg").read_text()


def test_the_flatpaks_menu_entry_keeps_its_name_and_icon():
    """The Flatpak's manifest builds its entry with desktop_entry(command): "openExpress" with
    the Flatpak's icon, nothing of the version from source."""
    entry = desktop.desktop_entry("openexpress gui")
    assert entry == (
        "[Desktop Entry]\nType=Application\nName=openExpress\n"
        f"{desktop._localized('GenericName', desktop.GENERIC_NAME)}\n"
        f"{desktop._localized('Comment', desktop.COMMENT)}\n"
        f"Exec=openexpress gui\nIcon={FLATPAK_ID}\nTerminal=false\nCategories=Utility;GTK;\n"
        f"{desktop._localized('Keywords', desktop.KEYWORDS)}\nStartupNotify=true\n")
    manifest = (Path(__file__).resolve().parent.parent / "packaging" / "flatpak"
                / f"{FLATPAK_ID}.yml").read_text()
    assert "desktop_entry('openexpress gui')" in manifest


def test_the_names_of_the_version_from_source_are_translated(from_source):
    from openexpress import i18n

    if "pl" not in i18n.available_languages():
        pytest.skip("the catalogs are not built (python3 -m openexpress.catalogs)")
    out = {p.name: text for p, text in desktop.files().items()}
    assert "Name[pl]=openExpress (ze źródeł)" in out[f"{SOURCE_ID}.desktop"].splitlines()
    assert "Name[pl]=openExpress (w\u00a0tle)" in out[f"{SOURCE_ID}.Watch.desktop"].splitlines()


# --- what the version from source left under the Flatpak's ID ---------------------------------


def test_its_own_old_entries_and_icons_go(from_source):
    left = _left_before()
    planned = desktop.remove_leftovers(dry_run=True)
    assert sorted(planned) == sorted(left.values())
    assert all(p.exists() for p in left.values())  # a dry run
    assert sorted(desktop.remove_leftovers(dry_run=False)) == sorted(left.values())
    assert not any(p.exists() for p in left.values())


def test_an_entry_that_runs_the_flatpak_stays(from_source):
    """E.g. one a menu editor saved under the Flatpak's ID."""
    left = _left_before(entry=FLATPAK_RUN)
    removed = desktop.remove_leftovers(dry_run=False)
    assert left["entry"] not in removed and left["entry"].exists()
    assert left["watch"] in removed


def test_an_icon_other_than_the_programs_stays(from_source):
    left = _left_before(icon=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    removed = desktop.remove_leftovers(dry_run=False)
    assert left["icon"] not in removed and left["icon"].exists()
    assert left["symbolic"] in removed


def test_nothing_else_is_looked_at(from_source):
    other = _put(_data() / "applications" / "org.example.Tool.desktop", _entry(OURS))
    assert desktop.remove_leftovers(dry_run=False) == [] and other.exists()


def test_at_the_start_an_old_entry_is_replaced_by_the_new_one(from_source, caplog):
    left = _left_before()
    with caplog.at_level(logging.INFO, logger="openexpress"):
        desktop.tidy_up()
    apps = _data() / "applications"
    assert not any(p.exists() for p in left.values())
    assert (apps / f"{SOURCE_ID}.desktop").exists()
    assert (apps / f"{SOURCE_ID}.Watch.desktop").exists()
    said = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("desktop: removed") and f"{FLATPAK_ID}.desktop" in m for m in said)
    assert any(m.startswith("desktop: wrote") and f"{SOURCE_ID}.desktop" in m for m in said)


def test_at_the_start_old_icons_alone_bring_no_entry(from_source):
    left = _left_before()
    left["entry"].unlink()
    left["watch"].unlink()
    desktop.tidy_up()
    assert not left["icon"].exists() and not left["symbolic"].exists()
    assert not (_data() / "applications" / f"{SOURCE_ID}.desktop").exists()


def test_at_the_start_with_nothing_left_nothing_is_written(from_source):
    desktop.tidy_up()
    assert not (_data() / "applications").exists()
    assert not (paths.xdg("XDG_CONFIG_HOME", ".config") / "systemd").exists()


def test_the_flatpak_tidies_nothing(from_source, monkeypatch):
    monkeypatch.setattr(paths, "in_flatpak", lambda: True)
    left = _left_before()
    desktop.tidy_up()
    assert all(p.exists() for p in left.values())


def test_a_failure_at_the_start_is_only_logged(from_source, monkeypatch, caplog):
    _left_before()

    def refused(dry_run):
        raise PermissionError("made-up: read-only folder")

    monkeypatch.setattr(desktop, "remove_leftovers", refused)
    with caplog.at_level(logging.WARNING, logger="openexpress"):
        desktop.tidy_up()  # no exception
    assert any("PermissionError" in r.getMessage() for r in caplog.records)


def test_the_window_tidies_up_before_it_starts(from_source, monkeypatch, in_the_window):
    from openexpress.gui import app

    order = []
    in_the_window("missing_display", lambda: None)
    in_the_window("install_excepthook", lambda: None)
    monkeypatch.setattr(desktop, "tidy_up", lambda: order.append("tidy up"))
    monkeypatch.setattr(app.App, "run", lambda self, argv: order.append("window") or 0)
    assert app.main([]) == 0
    assert order == ["tidy up", "window"]


def test_the_watcher_tidies_up_before_it_starts(from_source, monkeypatch):
    from openexpress import watch

    order = []
    monkeypatch.setattr(desktop, "tidy_up", lambda: order.append("tidy up"))
    monkeypatch.setattr(watch.Watcher, "run", lambda self, argv: order.append("watcher") or 0)
    assert watch.main() == 0
    assert order == ["tidy up", "watcher"]


# --- the icon folder: a cache made before must not hide a new icon ----------------------------
#
# An icon-theme.cache in the home folder's hicolor (made by gtk-update-icon-cache, not by
# openExpress) counts for GTK and GNOME Shell as long as the hicolor folder is not newer than
# it; an icon written into scalable/apps changes only that folder's time, so the cache, which
# does not have it, hides it (the desktop shows its default icon). The time of hicolor is set to
# now whenever an icon is written or removed there; the cache itself is never touched.

OLD = 1_767_225_600  # a time well before the test


def _hicolor() -> Path:
    return _data() / "icons" / "hicolor"


def _old_cache() -> tuple[Path, bytes]:
    """A cache of other apps' icons made before, and the hicolor folder as old as it, with the
    folders of its icons there already (so that writing an icon makes no new one in it)."""
    for size in ("scalable", "symbolic"):
        (_hicolor() / size / "apps").mkdir(parents=True, exist_ok=True)
    cache = _put(_hicolor() / "icon-theme.cache", b"made-up cache of other apps' icons")
    for path in (cache, _hicolor()):
        os.utime(path, (OLD, OLD))
    return cache, cache.read_bytes()


def _newer(icon: Path) -> bool:
    return _hicolor().stat().st_mtime_ns >= icon.stat().st_mtime_ns > OLD * 10**9


def test_the_icon_folder_is_newer_than_an_icon_written(from_source):
    cache, before = _old_cache()
    written = desktop.install(dry_run=False)
    icon = next(p for p in written if p.name == f"{SOURCE_ID}.svg")
    assert _newer(icon)
    assert cache.read_bytes() == before and cache.stat().st_mtime == OLD  # never ours to touch


def test_the_icon_folder_is_newer_after_icons_are_removed(from_source, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)  # no systemctl to ask
    desktop.install(dry_run=False)
    _old_cache()
    removed = desktop.uninstall(dry_run=False)
    assert any(p.suffix == ".svg" for p in removed)
    assert _hicolor().stat().st_mtime > OLD


def test_the_icon_folder_is_newer_after_the_start_tidies_up_icons(from_source):
    left = _left_before()
    left["entry"].unlink()
    left["watch"].unlink()  # the old icons alone: removed, nothing written
    _old_cache()
    desktop.tidy_up()
    assert not left["icon"].exists() and _hicolor().stat().st_mtime > OLD


def test_the_icon_folder_keeps_its_time_when_no_icon_changes(from_source, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    left = _left_before(entry=FLATPAK_RUN, icon=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    left["watch"].unlink()
    left["symbolic"].unlink()  # left: an entry that runs the Flatpak and an icon not ours
    _old_cache()
    desktop.tidy_up()  # nothing of ours under the Flatpak's ID
    desktop.uninstall(dry_run=False)  # nothing of its own written
    assert _hicolor().stat().st_mtime == OLD


def test_a_dry_run_touches_nothing_not_even_the_icon_folder(from_source, capsys):
    _left_before()
    _old_cache()
    assert cli.main(["install-desktop"]) == 0
    assert cli.main(["install-desktop", "--remove"]) == 0
    assert _hicolor().stat().st_mtime == OLD
    assert not (_data() / "applications" / f"{SOURCE_ID}.desktop").exists()


# --- install-desktop --------------------------------------------------------------------------


def test_install_desktop_goes_on_beside_the_flatpak(from_source, monkeypatch, capsys):
    """Its entry has a name of its own and a file of its own: nothing of the Flatpak's is
    hidden, so nothing is refused, and nothing asks whether the Flatpak is installed."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail(f"started {a}"))
    assert cli.main(["install-desktop"]) == 0
    assert f"Would write {_data() / 'applications' / f'{SOURCE_ID}.desktop'}" in (
        capsys.readouterr().out)


def test_install_desktop_refuses_force(from_source):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["install-desktop", "--force"])
    assert stopped.value.code == 2


def test_install_desktop_removes_the_old_entries_and_writes_the_new_ones(from_source, capsys):
    left = _left_before()
    assert cli.main(["install-desktop"]) == 0
    said = capsys.readouterr().out
    assert f"Would remove {left['entry']}" in said and all(p.exists() for p in left.values())
    assert not (_data() / "applications" / f"{SOURCE_ID}.desktop").exists()

    assert cli.main(["install-desktop", "--apply"]) == 0
    said = capsys.readouterr().out
    assert f"Removed {left['entry']}" in said and not any(p.exists() for p in left.values())
    assert f"Wrote {_data() / 'applications' / f'{SOURCE_ID}.desktop'}" in said


def test_install_desktop_remove_takes_the_old_entries_too(from_source, monkeypatch, capsys):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)  # no systemctl to ask
    written = desktop.install(dry_run=False)
    left = _left_before(entry=FLATPAK_RUN)
    assert cli.main(["install-desktop", "--remove", "--apply"]) == 0
    assert not any(p.exists() for p in written)
    assert not left["watch"].exists() and not left["icon"].exists()
    assert left["entry"].exists()  # it runs the Flatpak
    assert f"Removed {left['watch']}" in capsys.readouterr().out

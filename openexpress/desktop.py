"""The menu entries, icon and systemd user service of the version from source (home directory
only), under its own ID (paths.SOURCE_APP_ID), and the Flatpak's menu entry, which its manifest
builds with desktop_entry. What the version from source wrote under the Flatpak's ID before it
had an ID of its own is removed by install-desktop and at its start (remove_leftovers, tidy_up):
only its own menu entries and the program's own icons."""

from __future__ import annotations

import gettext
import logging
import os
import sys
from pathlib import Path

from openexpress import i18n, paths
from openexpress.i18n import N_

log = logging.getLogger(__name__)

SERVICE = "openexpress-watch.service"
SOURCE_ID = paths.SOURCE_APP_ID

NAME = "openExpress"
# TRANSLATORS: the name in the application menu of openExpress installed from its source code
# (not the Flatpak), so that it can be told apart from the Flatpak's "openExpress"; keep the
# name openExpress as it is
NAME_SOURCE = N_("openExpress (source)")
# TRANSLATORS: name of the hidden desktop entry that syncs in the background when the device is
# connected
NAME_BACKGROUND = N_("openExpress (background)")
# TRANSLATORS: generic name of the app in application menus (GenericName in the desktop file): a
# tool that syncs Garmin devices
GENERIC_NAME = N_("Garmin Device Sync")
COMMENT = N_("Sync, updates and maps for Garmin devices")
# TRANSLATORS: search keywords for the application menu; keep the ";" separators and the ending ";"
KEYWORDS = N_("Garmin;Fenix;Connect;sync;")
SERVICE_DESCRIPTION = "openExpress: sync when the watch is connected"
# The program's icons, named after the Flatpak's ID; the version from source installs them under
# its own.
ICON = Path(__file__).parent / "data" / f"{paths.APP_ID}.svg"
SYMBOLIC = ICON.with_name(f"{paths.APP_ID}-symbolic.svg")


def _localized(key: str, message: str) -> str:
    """Key=message plus Key[lang]=... lines for every available translation that differs.

    The active UI language is not changed: each catalog is loaded separately.
    """
    lines = [f"{key}={message}"]
    for lang in i18n.available_languages():
        if lang == i18n.SOURCE_LANGUAGE:
            continue
        tr = gettext.translation(i18n.DOMAIN, i18n.LOCALE_DIR, languages=[lang], fallback=True)
        value = tr.gettext(message)
        if value != message:
            lines.append(f"{key}[{lang}]={value}")
    return "\n".join(lines)


# Characters that make the desktop-entry spec require quoting an Exec argument.
_DESKTOP_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_quote(arg: str) -> str:
    """One argument of a .desktop Exec= value (quoting rules plus string escaping)."""
    if arg and not (set(arg) & _DESKTOP_RESERVED):
        quoted = arg
    else:
        quoted = '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
    # The general string escape (backslash) applies before the Exec quoting rule, and "%"
    # starts a field code.
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def systemd_quote(arg: str, exec_line: bool = False) -> str:
    """One double-quoted word for a systemd unit file (specifiers and, in Exec*, $ escaped)."""
    out = arg.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("%", "%%")
    if exec_line:
        out = out.replace("$", "$$")
    return f'"{out}"'


def _source_root() -> Path | None:
    """If openExpress runs from a source checkout, the directory to put on PYTHONPATH."""
    import site

    root = Path(__file__).resolve().parent.parent
    installed = {Path(p).resolve() for p in site.getsitepackages() + [site.getusersitepackages()]}
    return None if root in installed else root


def desktop_entry(exec_gui: str, source: bool = False, background: bool = False) -> str:
    """A .desktop launcher, with translated keys for every available language: the Flatpak's,
    as its manifest builds it; with `source` the menu entry of the version from source, with
    `background` the hidden one of its background service, each with its own name and its own
    icon."""
    if background:
        name = _localized("Name", NAME_BACKGROUND)
    elif source:
        name = _localized("Name", NAME_SOURCE)
    else:
        name = f"Name={NAME}"
    icon = SOURCE_ID if source or background else paths.APP_ID
    text = f"""[Desktop Entry]
Type=Application
{name}
{_localized("GenericName", GENERIC_NAME)}
{_localized("Comment", COMMENT)}
Exec={exec_gui}
Icon={icon}
Terminal=false
Categories=Utility;GTK;
{_localized("Keywords", KEYWORDS)}
StartupNotify=true
"""
    return text + ("NoDisplay=true\n" if background else "")


def files() -> dict[Path, str]:
    data = paths.xdg("XDG_DATA_HOME", ".local/share")
    config = paths.xdg("XDG_CONFIG_HOME", ".config")
    root = _source_root()
    env = f"PYTHONPATH={root}" if root else ""
    args = (["env", env] if env else []) + [sys.executable, "-m", "openexpress", "gui"]
    exec_gui = " ".join(desktop_exec_quote(a) for a in args)
    exec_watch = " ".join([systemd_quote(sys.executable, exec_line=True), "-m", "openexpress",
                           "watch"])
    desktop = desktop_entry(exec_gui, source=True)
    desktop_watch = desktop_entry(exec_gui, background=True)
    # systemd unit files have no localized keys: Description stays in English.
    service = f"""[Unit]
Description={SERVICE_DESCRIPTION}
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
{('Environment=' + systemd_quote(env)) if env else ''}
ExecStart={exec_watch}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
"""
    result = {
        data / "applications" / f"{SOURCE_ID}.desktop": desktop,
        data / "applications" / f"{SOURCE_ID}.Watch.desktop": desktop_watch,
        config / "systemd" / "user" / SERVICE: service,
        data / "icons" / "hicolor" / "scalable" / "apps" / f"{SOURCE_ID}.svg": ICON.read_text(),
    }
    if SYMBOLIC.is_file():
        result[data / "icons" / "hicolor" / "symbolic" / "apps" /
               f"{SOURCE_ID}-symbolic.svg"] = SYMBOLIC.read_text()
    return result


def install(dry_run: bool) -> list[Path]:
    """Returns the files written (to be written, in a dry run)."""
    written = []
    for path, content in files().items():
        written.append(path)
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if not dry_run:
        _icons_changed(written)
    return written


def _icons_changed(changed: list[Path]) -> None:
    """After an icon was written or removed below the home folder's hicolor: the time of hicolor
    set to now. An icon-theme.cache there (made by gtk-update-icon-cache, never by openExpress,
    and never touched) counts for GTK and GNOME Shell only while hicolor is not newer than it;
    an icon written into scalable/apps changes the time of that folder alone, so a cache made
    before would hide the new icon (the desktop shows its default one), and the desktop would
    not look at the theme again either."""
    hicolor = paths.xdg("XDG_DATA_HOME", ".local/share") / "icons" / "hicolor"
    if hicolor.is_dir() and any(p.is_relative_to(hicolor) for p in changed):
        os.utime(hicolor)


def leftovers() -> list[Path]:
    """What the version from source wrote under the Flatpak's ID before it had an ID of its own,
    and only what is its own: a menu entry whose Exec= runs "-m openexpress" (one that runs the
    Flatpak, e.g. saved by a menu editor, is not), and an icon that is the program's own, byte
    for byte. The Flatpak's own files are elsewhere (its exports) and never looked at."""
    data = paths.xdg("XDG_DATA_HOME", ".local/share")
    apps = data / "applications"
    icons = data / "icons" / "hicolor"
    entries = [apps / f"{paths.APP_ID}.desktop", apps / f"{paths.APP_ID}.Watch.desktop"]
    found = [entry for entry in entries if _runs_from_source(entry)]
    for icon, ours in ((icons / "scalable" / "apps" / f"{paths.APP_ID}.svg", ICON),
                       (icons / "symbolic" / "apps" / f"{paths.APP_ID}-symbolic.svg", SYMBOLIC)):
        if _same_bytes(icon, ours):
            found.append(icon)
    return found


def _runs_from_source(entry: Path) -> bool:
    try:
        lines = entry.read_text(errors="replace").splitlines()
    except OSError:  # not there, or not readable: not taken for ours
        return False
    return any(line.startswith("Exec=") and "-m openexpress" in line for line in lines)


def _same_bytes(path: Path, ours: Path) -> bool:
    try:
        return path.is_file() and path.read_bytes() == ours.read_bytes()
    except OSError:
        return False


def remove_leftovers(dry_run: bool) -> list[Path]:
    """Removes leftovers() and returns them (in a dry run: those that would be removed)."""
    found = leftovers()
    if not dry_run:
        for path in found:
            path.unlink(missing_ok=True)
        _icons_changed(found)
    return found


def tidy_up() -> None:
    """At the start of the window and of the background service of the version from source:
    its leftovers under the Flatpak's ID removed (remove_leftovers), and when one of them was a
    menu entry, its own files written at once (install), so that it keeps its menu entry. Said
    in the log; an error is said there only: the window and the service start all the same."""
    if paths.in_flatpak():
        return
    try:
        removed = remove_leftovers(dry_run=False)
        for path in removed:
            log.info("desktop: removed %s, left by the version from source under the Flatpak's ID",
                     path)
        if any(path.suffix == ".desktop" for path in removed):
            for path in install(dry_run=False):
                log.info("desktop: wrote %s", path)
    except Exception as e:  # noqa: BLE001 - never in the way of the start
        log.warning("desktop: what the version from source left under the Flatpak's ID was not "
                    "tidied up (%s: %s)", type(e).__name__, e)


def _service_wants_link() -> Path:
    config = paths.xdg("XDG_CONFIG_HOME", ".config")
    return config / "systemd" / "user" / "graphical-session.target.wants" / SERVICE


def uninstall(dry_run: bool) -> list[Path]:
    """Disables the service, then removes the launcher, icon and service written by install(),
    and what the version from source left under the Flatpak's ID (leftovers)."""
    import shutil
    import subprocess

    link = _service_wants_link()
    paths_ = [p for p in files() if p.exists()] + leftovers()
    if link.exists() or link.is_symlink():
        paths_.append(link)
    if not dry_run:
        if shutil.which("systemctl"):
            try:
                subprocess.run(["systemctl", "--user", "disable", "--now", SERVICE],
                               capture_output=True, timeout=30, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        for p in paths_:
            p.unlink(missing_ok=True)
        _icons_changed(paths_)
    return paths_

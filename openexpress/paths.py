"""Local application directories (XDG)."""

from __future__ import annotations

import configparser
import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path

from openexpress import platform

APP_NAME = "openexpress"
# The Flatpak's ID; the icons in openexpress/data and the Flatpak's folders are named after it.
APP_ID = "io.github.tomaszbojanowski.OpenExpress"
# The version from source has an ID of its own (app_id), so that its window, its background
# service and its menu entries neither take the Flatpak's nor hide them.
SOURCE_APP_ID = APP_ID + ".Devel"
FLATPAK_INFO = Path("/.flatpak-info")


def xdg(var: str, default: str) -> Path:
    """The XDG base folder in the variable `var` (e.g. XDG_DATA_HOME); `default` below the
    home folder when the variable is not set or empty."""
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / default


def _app_dir(var: str, default: str) -> Path:
    """XDG directory for the app (on macOS, without the variable, under ~/Library:
    platform.app_dir)."""
    return platform.app_dir(var) or xdg(var, default) / APP_NAME


def config_dir() -> Path:
    return _app_dir("XDG_CONFIG_HOME", ".config")


def settings_file() -> Path:
    """settings.json: the settings of the program (settings.Settings) and the language of
    the interface (i18n)."""
    return config_dir() / "settings.json"


def data_dir() -> Path:
    return _app_dir("XDG_DATA_HOME", ".local/share")


def cache_dir() -> Path:
    return _app_dir("XDG_CACHE_HOME", ".cache")


def in_flatpak() -> bool:
    return FLATPAK_INFO.exists()


def app_id() -> str:
    """The ID of this install: the Flatpak's (APP_ID), or the one of the version from source
    (SOURCE_APP_ID)."""
    return APP_ID if in_flatpak() else SOURCE_APP_ID


def session_mark_dirs() -> list[Path]:
    """Where a USB session of openExpress's own is marked for its other processes
    (device.session_mark), from this session's XDG_RUNTIME_DIR: session_mark_dirs_in."""
    return session_mark_dirs_in(os.environ.get("XDG_RUNTIME_DIR"))


def session_mark_dirs_in(runtime: str | None) -> list[Path]:
    """The folder the Flatpak shares with the host and its other instances,
    `runtime`/app/APP_ID/, when it is there (Flatpak makes it as it starts the app: the version
    from source sees it once the Flatpak has run since the system started), then always
    `runtime`/openexpress/ (the version from source's own; in the Flatpak, a folder of its
    sandbox). An empty list without a runtime folder."""
    if not runtime:
        return []
    shared = Path(runtime) / "app" / APP_ID
    return [*([shared] if shared.is_dir() else []), Path(runtime) / "openexpress"]


def flatpak_data_dir() -> Path:
    """The data folder of the Flatpak of openExpress, as the rest of this user's files see it
    (outside the sandbox)."""
    return Path.home() / ".var" / "app" / APP_ID / "data" / "openexpress"


def other_install_dir() -> Path | None:
    """The data folder of the other install of openExpress on this computer, which this one
    reads and never writes: the Flatpak's for the version from source; None in the Flatpak,
    whose sandbox hides the folders of the version from source."""
    return None if in_flatpak() else flatpak_data_dir()


def flatpak_build() -> tuple[str | None, str | None] | None:
    """(branch, the first 12 characters of the app's commit) of this Flatpak, from
    /.flatpak-info; None outside the Flatpak or when the file cannot be read."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if not parser.read(FLATPAK_INFO, encoding="utf-8") or not parser.has_section("Instance"):
            return None
        instance = parser["Instance"]
        commit = instance.get("app-commit") or None
        return instance.get("branch") or None, commit[:12] if commit else None
    except (configparser.Error, OSError, UnicodeError):
        return None


def host_command(*args: str, watch_bus: bool = False) -> list[str]:
    """A command for the host, run from the Flatpak (flatpak-spawn --host); every one is made
    here. GIO_USE_VFS=gvfs: the session helper of flatpak before 1.14.5 (1.12 on Ubuntu 22.04)
    passes on its own GIO_USE_VFS=local, and the host's gio then sees mtp:// as no file of gvfs,
    so every copy to the device fails with "Operation not supported". Where gvfs is not
    installed, GIO says so on stderr and keeps the local VFS. `watch_bus`: the command on the
    host ends when flatpak-spawn does (a copy stopped here stops there too)."""
    return ["flatpak-spawn", "--host", *(["--watch-bus"] if watch_bus else []),
            "--env=GIO_USE_VFS=gvfs", *args]


def flatpak_origin() -> str | None:
    """The remote this Flatpak was installed from ("openexpress"; a bundle gets
    "…-origin"), asked of the host; None outside the Flatpak and on any failure."""
    if not in_flatpak():
        return None
    # With two branches installed (e.g. master and test), a question without the branch is
    # ambiguous and fails: this one's branch goes with the app ID when it can be read.
    build = flatpak_build()
    ref = f"{APP_ID}//{build[0]}" if build and build[0] else APP_ID
    # Only reads the metadata of the installation; besides copying files to the device
    # (gio copy), the one use of flatpak-spawn.
    try:
        answer = subprocess.run(host_command("flatpak", "info", "--show-origin", ref),
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if answer.returncode != 0:
        return None
    return answer.stdout.strip() or None


def flatpak_source() -> tuple[str | None, str | None]:
    """(origin, short commit) of this Flatpak, for the report and the log."""
    build = flatpak_build()
    return flatpak_origin(), build[1] if build else None


def state_dir() -> Path:
    """Where the log file is kept (in the Flatpak: under ~/.var/app/…/.local/state)."""
    return _app_dir("XDG_STATE_HOME", ".local/state")


def device_dir(unit_id: int) -> Path:
    return data_dir() / "devices" / str(unit_id)


def default_backup_dir() -> Path:
    try:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        if docs:
            return Path(docs) / "Garmin" / "Backups"
    except (ImportError, ValueError):
        pass
    return Path.home() / "Documents" / "Garmin" / "Backups"


def shown_path(path: Path) -> str:
    """A path as it is shown to the user: with ~ for the home folder."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if len(home) > 1 and text.startswith(home + os.sep) else text


def default_gpx_dir() -> Path:
    """Where `activities export` writes GPX files: next to the backups, in Garmin in the
    Documents folder (which the Flatpak may write)."""
    return default_backup_dir().parent / "GPX"


PRIVATE_DIR = 0o700


def is_safe_component(name: str) -> bool:
    """Whether a name from a device or a server can be one component of a path on this
    computer: not empty, not "." or "..", no separator (so not absolute either), and no
    control character (NUL among them) - a device may give one after a bit flip."""
    if not name or name in (".", ".."):
        return False
    return not any(c in "/\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in name)


def is_inside(path: Path, folder: Path) -> bool:
    """Whether `path`, links and ".." resolved, lies within `folder` (also resolved)."""
    return path.resolve().is_relative_to(folder.resolve())


def make_dir(path: Path) -> None:
    """Makes `path` and the folders above it. The app's own folder for the settings, the data
    or the log (the login, what came from the device, what was done), when it is made here,
    is its owner's alone; the cache, the backups and the folders above the app's (~/.config,
    …) get the usual mode. Folders that exist keep theirs."""
    for root in (config_dir(), data_dir(), state_dir()):
        if path == root or root in path.parents:
            _make_private(root)
            break
    path.mkdir(parents=True, exist_ok=True)


def make_private_dir(path: Path) -> None:
    """Makes `path` as make_dir does, its owner's alone (PRIVATE_DIR) from the start, also one
    made before with the usual mode: for what nobody else may list (copies of a device's files,
    Garmin's answers about the account)."""
    make_dir(path.parent)
    path.mkdir(mode=PRIVATE_DIR, exist_ok=True)
    if path.stat().st_mode & 0o777 != PRIVATE_DIR:
        path.chmod(PRIVATE_DIR)  # made before with the usual mode, or the umask cut mkdir's


def _make_private(root: Path) -> None:
    if root.is_dir():
        return
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir(mode=PRIVATE_DIR)
    except FileExistsError:
        return  # made by another process in the meantime: left as it is
    os.chmod(root, PRIVATE_DIR)  # the umask cuts mkdir's mode; this folder was just made


@contextlib.contextmanager
def holding(f, *, shared: bool = False, wait: bool = True, busy=None):
    """The lock (fcntl.flock) of the open file `f` held for the block, shared or exclusive;
    the system lets go of it when the process ends. Without `wait` a lock another holds is
    refused at once: BlockingIOError, or the exception `busy()` returns. What the block
    raises goes on as it is."""
    try:
        fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    | (0 if wait else fcntl.LOCK_NB))
    except BlockingIOError:
        if busy is None:
            raise
        raise busy() from None
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, *, mode: str = "a", shared: bool = False, wait: bool = True, busy=None):
    """The lock file `path`, opened with `mode` ("a" makes it when it is not there), held for
    the block (holding). Its folder has to be there."""
    with open(path, mode) as f, holding(f, shared=shared, wait=wait, busy=busy):
        yield


@contextlib.contextmanager
def folder_locked(folder: Path, name: str):
    """The lock file `name` in `folder`, the folder made when it is not there, held for the
    block (locked)."""
    make_dir(folder)
    with locked(folder / name):
        yield


@contextlib.contextmanager
def settings_locked():
    """One writer of settings.json at a time, in any process and any thread (each opens the
    lock file of its own); the system lets go of it when the process ends. Reading alone
    needs no lock: the file is replaced whole (write_private)."""
    with folder_locked(config_dir(), "settings.lock"):
        yield


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically write a file readable only by its owner (0600): text in UTF-8, bytes as
    they are."""
    make_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        binary = isinstance(data, bytes)
        with (os.fdopen(fd, "wb") if binary else os.fdopen(fd, "w", encoding="utf-8")) as f:
            fd = -1
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd != -1:
            os.close(fd)
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def read_json_object(path: Path) -> dict | None:
    """What a JSON file holds, read as write_private writes it (UTF-8, whatever the locale);
    None when it does not read or holds no object (a file changed by hand)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None

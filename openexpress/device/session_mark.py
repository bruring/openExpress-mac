"""The mark of a USB session of openExpress's own for its other processes on this computer: the
window, the command line and the background watcher, of the version from source and of the
Flatpak. Each process knows its own sessions (gio_storage._quiet_until); for the others the
device's unmount and its mount again would look like an unplug and a plug-in (the watcher would
sync it, the other window would close it), so the process of the session marks it here.

One file a device's place on USB, ptp-<bus>-<address>.json, in each folder of
paths.session_mark_dirs (the one the Flatpak shares with the host when it is there, and the
version from source's own): {"usb": "<bus>-<address>", "until": null, or the time.time() its
quiet ends, "app": "flatpak" or "source"}; nothing else of the device. The process of the session
holds flock(LOCK_EX) on it for the whole session (the kernel lets go of it with the process),
and at the end writes when its quiet ends. Another process takes the session as under way while
the lock is taken or the quiet lasts; a file whose lock is free and whose quiet is over or was
never written (a process that ended in its session) means nothing, and the look that finds it
removes it. A mark that cannot be kept (no runtime folder, a folder not writable) is said once in
the log: the session goes on, known to its own process only."""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from openexpress import paths

log = logging.getLogger(__name__)

_now = time.time  # the clock of `until`, the same for every process of this computer
_said: set[str] = set()  # what could not be kept, said once a process


def _name(place: tuple[int, int]) -> str:
    return f"ptp-{place[0]}-{place[1]}.json"


def _text(place: tuple[int, int], until: float | None) -> bytes:
    # Only "until" is read back; "usb" and "app" tell whoever looks into the folder whose mark
    # it is.
    return json.dumps({"usb": f"{place[0]}-{place[1]}", "until": until,
                       "app": "flatpak" if paths.in_flatpak() else "source"}).encode()


def _not_kept(why: str) -> None:
    if why not in _said:
        _said.add(why)
        log.info("session mark: not kept (%s): other openExpress processes may take this "
                 "session for an unplug", why)


class Mark:
    """The files of one session, each held with flock until `end`."""

    def __init__(self, place: tuple[int, int] | None, held: list[int]):
        self.place, self.held = place, held

    def end(self, quiet_s: float) -> None:
        """The session is over: each file says when its quiet ends, and is let go."""
        if self.place is not None:
            text = _text(self.place, _now() + quiet_s)
            for fd in self.held:
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, text, 0)
        for fd in self.held:
            with contextlib.suppress(OSError):
                os.close(fd)
        self.held = []


def begin(place: tuple[int, int] | None) -> Mark:
    """Mark a session on the device at `place` (bus, device number) in each folder: a file made
    aside, held with flock, then put in place, so that no process sees it before it is held.
    Never raises; nothing is marked for a device whose place is not known."""
    if place is None:
        return Mark(None, [])
    folders = paths.session_mark_dirs()
    if not folders:
        _not_kept("no runtime folder")
        return Mark(None, [])
    held = []
    for folder in folders:
        try:
            if folder == folders[-1]:  # the own folder, made when it is not there
                folder.mkdir(mode=0o700, exist_ok=True)
            fd, aside = tempfile.mkstemp(prefix=".ptp-", suffix=".tmp", dir=folder)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, _text(place, None))
                os.replace(aside, folder / _name(place))
            except OSError:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.unlink(aside)
                raise
            held.append(fd)
        except OSError as e:
            _not_kept(f"{folder.name}: {type(e).__name__}")
    return Mark(place, held)


def active(place: tuple[int, int]) -> bool:
    """Whether another process (or this one) marks a session on the device at `place` now: its
    lock taken, or its quiet not over. A mark that means nothing is removed."""
    for folder in paths.session_mark_dirs():
        path = folder / _name(place)
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            if _taken(fd):
                return True
            if _quiet(fd):
                return True
            _remove_if_still(path, fd)
        finally:
            os.close(fd)
    return False


def any_marked() -> bool:
    """Whether any mark is there at all: a cheap look before the device of a mount is asked
    where it is on USB."""
    for folder in paths.session_mark_dirs():
        with contextlib.suppress(OSError):
            if any(folder.glob("ptp-*.json")):
                return True
    return False


def _taken(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def _quiet(fd: int) -> bool:
    try:
        until = json.loads(os.pread(fd, 4096, 0) or b"{}").get("until")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(until, (int, float)) and _now() < until


def _remove_if_still(path: Path, fd: int) -> None:
    """Remove the mark that means nothing, unless another process holds it now or put a new one
    in its place meanwhile."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.stat(path).st_ino == os.fstat(fd).st_ino:
            os.unlink(path)
    except OSError:
        pass

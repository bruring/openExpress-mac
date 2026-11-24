"""The mark of a USB session of openExpress's own for its other processes (device.session_mark):
one file a device's place on USB, ptp-<bus>-<address>.json, in each folder of
paths.session_mark_dirs - the folder the Flatpak shares with the host, when it is there, and
always $XDG_RUNTIME_DIR/openexpress/. The process of the session holds flock on it for the whole
session, then writes when its quiet ends; another process sees the session while the lock is
taken or the quiet lasts, and a mark of a process that died in its session means nothing. The
other process is a real one (fakes.OtherProcessSession: a subprocess with the test's runtime
folder as its XDG_RUNTIME_DIR). All places are made up."""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import OtherProcessSession

from openexpress import paths
from openexpress.device import session_mark

PLACE = (3, 14)
NAME = "ptp-3-14.json"


@pytest.fixture
def runtime(tmp_path):
    """The test's runtime folder, where conftest's private_dirs points the marks."""
    return tmp_path / "runtime"


@pytest.fixture
def shared(runtime):
    """The folder the Flatpak shares with the host, there: it has run since the system started."""
    folder = runtime / "app" / paths.APP_ID
    folder.mkdir(parents=True)
    return folder


# --- where the marks go ----------------------------------------------------------------------

def test_the_shared_folder_comes_first_when_it_is_there_and_the_own_one_always(runtime):
    assert paths.session_mark_dirs_in(str(runtime)) == [runtime / "openexpress"]
    (runtime / "app" / paths.APP_ID).mkdir(parents=True)
    assert paths.session_mark_dirs_in(str(runtime)) == [runtime / "app" / paths.APP_ID,
                                                        runtime / "openexpress"]
    assert paths.session_mark_dirs_in(None) == [] and paths.session_mark_dirs_in("") == []


def test_the_mark_says_the_place_on_usb_its_quiet_and_the_install_nothing_more(runtime,
                                                                               monkeypatch):
    monkeypatch.setattr(session_mark, "_now", lambda: 1000.0)
    mark = session_mark.begin(PLACE)
    during = json.loads((runtime / "openexpress" / NAME).read_text())
    mark.end(15)
    after = json.loads((runtime / "openexpress" / NAME).read_text())
    assert during == {"usb": "3-14", "until": None, "app": "source"}
    assert after == {"usb": "3-14", "until": 1015.0, "app": "source"}


# --- another process ---------------------------------------------------------------------------

def test_a_session_of_another_process_is_seen_while_it_lasts_and_its_quiet(tmp_path, runtime,
                                                                           monkeypatch):
    """Without the shared folder: the own one alone carries the mark."""
    other = OtherProcessSession(tmp_path, runtime)
    assert not (runtime / "app").exists()
    assert session_mark.active(PLACE)  # its lock is taken
    other.finish()
    assert session_mark.active(PLACE)  # the quiet after it
    later = time.time() + 16
    monkeypatch.setattr(session_mark, "_now", lambda: later)
    assert not session_mark.active(PLACE)  # the quiet is over
    assert not (runtime / "openexpress" / NAME).exists()  # and its mark removed


def test_a_process_that_died_in_its_session_leaves_nothing_that_counts(tmp_path, runtime):
    other = OtherProcessSession(tmp_path, runtime, how="crash")
    assert session_mark.active(PLACE)
    other.finish()
    assert not session_mark.active(PLACE)  # the lock is free, no until
    assert not (runtime / "openexpress" / NAME).exists()


def test_two_devices_have_a_mark_each(tmp_path, runtime):
    first = OtherProcessSession(tmp_path, runtime, place=(3, 14), how="crash")
    second = OtherProcessSession(tmp_path, runtime, place=(3, 15))
    assert sorted(p.name for p in (runtime / "openexpress").glob("ptp-*.json")) == [
        "ptp-3-14.json", "ptp-3-15.json"]
    first.finish()
    assert not session_mark.active((3, 14)) and session_mark.active((3, 15))
    second.finish()
    assert not session_mark.active((1, 14))  # another bus, never marked


# --- the version from source and the Flatpak -------------------------------------------------

def _flatpak_runtime(tmp_path, shared):
    """A runtime folder as the Flatpak's sandbox has it: its own, with the shared folder in it."""
    sandbox = tmp_path / "sandbox-runtime"
    (sandbox / "app").mkdir(parents=True)
    (sandbox / "app" / paths.APP_ID).symlink_to(shared, target_is_directory=True)
    return sandbox


def test_a_session_in_the_flatpak_is_seen_by_the_version_from_source(tmp_path, runtime,
                                                                     shared):
    other = OtherProcessSession(tmp_path, _flatpak_runtime(tmp_path, shared))
    assert (shared / NAME).exists() and not (runtime / "openexpress" / NAME).exists()
    assert session_mark.active(PLACE)
    other.finish()


def test_a_session_of_the_version_from_source_is_seen_in_the_flatpak(tmp_path, runtime, shared):
    mark = session_mark.begin(PLACE)
    assert (shared / NAME).exists() and (runtime / "openexpress" / NAME).exists()
    code = ("import sys; sys.path.insert(0, sys.argv[1]); "
            "from openexpress.device import session_mark; "
            "print(session_mark.active((3, 14)))")
    sandbox = _flatpak_runtime(tmp_path, shared)
    seen = subprocess.run([sys.executable, "-I", "-c", code,
                           str(Path(__file__).resolve().parent.parent)],
                          env={**os.environ, "XDG_RUNTIME_DIR": str(sandbox)},
                          capture_output=True, text=True, check=True, timeout=20)
    mark.end(15)
    assert seen.stdout.strip() == "True"


# --- no place for a mark: the session goes on, said in the log -----------------------------------

@pytest.mark.parametrize("case", ["no runtime folder", "a runtime folder not writable"])
def test_without_a_place_for_the_mark_the_session_goes_on_and_the_log_says_so(runtime,
                                                                              monkeypatch,
                                                                              caplog, case):
    if case == "no runtime folder":
        monkeypatch.setattr(paths, "session_mark_dirs", list)
    else:
        runtime.chmod(0o500)
    monkeypatch.setattr(session_mark, "_said", set())
    try:
        with caplog.at_level(logging.INFO, logger="openexpress.device.session_mark"):
            for _again in range(2):
                mark = session_mark.begin(PLACE)
                mark.end(15)
        assert not session_mark.active(PLACE)
    finally:
        if runtime.exists():
            runtime.chmod(0o700)
    said = [r.getMessage() for r in caplog.records if r.name == "openexpress.device.session_mark"]
    assert len(said) == 1 and said[0].startswith("session mark: not kept")
    assert "other openExpress processes" in said[0]

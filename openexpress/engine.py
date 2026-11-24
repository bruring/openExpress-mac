"""openexpress engine: the program behind the macOS app (docs/ENGINE.md).

The app runs `openexpress engine` as a child process and talks to it in JSON Lines: a request
per line on stdin, a response per request and any number of events on stdout. stdout carries
nothing else: what the code of the command line prints as it goes is caught on the thread of
the request that runs it and sent as `log` events (and kept for the result of a method that
answers "as the command line"); anything else that prints goes to stderr, the engine's log.

The engine adds no behaviour of its own: each method opens the device as the command line
does (cli.open_device) and calls the same modules (backup, sync through service, updates,
maps, activities, auth, settings); the methods the contract answers "as the command line"
(updates.install, maps.install/update/remove, apps.install/remove) run the command itself.

Requests run each on a thread of its own. Those for one device take their turn, in the order
they came (_Turns): one at a time, while the reader stays free for `cancel`. A request is
stopped at a safe point only: before it starts, between items, at a progress message that is
not in the middle of a file written to the device (_safe_point). A device stays open for
KEEP_OPEN_S after the last request on it (on macOS its USB session), as the app's requests come
in bursts; then it is let go, so that another program may take it. It is let go at once when it
is unplugged, when another program is seen holding it or another process of the program waits
for it, and when the engine ends.

Devices are found by polling (platform.devices(): mac_storage on macOS, gio_storage on Linux):
`device.attached` and `device.detached` when they come and go, `device.busy` when another
program holds one (on macOS the IORegistry names it, platform/mac_usb.py; read again only when
the devices on USB change, or now and then while another program holds one).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from openexpress import __version__
from openexpress.i18n import _
from openexpress.progress import Reporter, Step, TimedWait

log = logging.getLogger(__name__)

PROTOCOL = 1
# How often USB is looked at for devices that came or went.
POLL_S = 2.0
# How long a device stays open after the last request on it (the app asks several things in a
# row as a page opens).
KEEP_OPEN_S = 8.0
# How long a look at the devices waits for the probes of those that came; a probe that takes
# longer (a device still starting) goes on, and its device is told attached once it is over.
PROBE_WAIT_S = 2.0
# Looks in a row a device must be missing from to be told detached: one alone may be another
# device's USB reset (an Edge 1040 went missing for one look while a fēnix 8 beside it reset),
# and the app's automatic mode would back up and sync it again when it came back.
GONE_AFTER_LOOKS = 2
# The requests that only read the device (nothing written to it, nor on this computer) and
# are run once more when it stopped responding in the middle of them and came back: a fēnix 8
# on a USB hub broke off about one read in a hundred, then answered again (ptp.Session.open).
RETRIED_ONCE = frozenset({"device.info", "activities.list", "activities.track", "maps.list",
                          "updates.list", "apps.list"})
# How long a request waits for a device that stopped responding to come back.
COME_BACK_WAIT_S = 25.0
# How long `shutdown` waits for the requests it cancelled to stop.
SHUTDOWN_WAIT_S = 30.0
# Steps of a long task, at most this often as `progress` events.
PROGRESS_EVERY_S = 0.25
# The activity files activities.list reads, at most this often as `progress` events.
READING_EVERY_S = 0.2


class EngineError(Exception):
    """An error response: `code` for the app's logic, `message` for the user (translated)."""

    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class Cancelled(BaseException):
    """A request stopped by `cancel` (or `shutdown`) at a safe point. A BaseException, so that
    no `except Exception` of the code it runs takes it for a failure to go on after."""


# --- what is printed: caught for the request of the thread ----------------------------------

class _Routed:
    """A text stream (sys.stdout, sys.stderr) that sends each line written on the thread of a
    request to that request (`route`), and everything else to `fallback` (the engine's log)."""

    _local = threading.local()

    def __init__(self, fallback, level: str):
        self._fallback, self.level = fallback, level

    @classmethod
    @contextlib.contextmanager
    def route(cls, say: Callable[[str, str], None]):
        """Lines written on this thread go to say(level, line) during the block."""
        cls._local.say, cls._local.parts = say, {}
        try:
            yield
        finally:
            parts = cls._local.parts
            cls._local.say, cls._local.parts = None, {}
            for level, rest in parts.items():
                if rest.strip():
                    say(level, rest.rstrip())

    def write(self, text: str) -> int:
        say = getattr(self._local, "say", None)
        if say is None:
            return self._fallback.write(text)
        parts = self._local.parts
        lines = (parts.get(self.level, "") + text).replace("\r", "\n").split("\n")
        parts[self.level] = lines.pop()
        for line in lines:
            if line.strip():
                say(self.level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        with contextlib.suppress(Exception):
            self._fallback.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._fallback, name)


# --- the devices: found by polling --------------------------------------------------------

@dataclasses.dataclass
class Seen:
    """A device found on USB: `key` tells it apart while it stays plugged in (its address);
    `holder`: the program that holds it for itself, if another does."""

    key: str
    found: object  # found.FoundDevice
    holder: str | None = None


@dataclasses.dataclass
class Attached:
    """A device found, as the engine knows it: what its GarminDevice.xml says (probe), or
    nothing yet while another program holds it."""

    key: str
    found: object
    unit_id: int | None = None
    description: str = ""
    part_number: str = ""
    software: str = ""
    busy_by: str | None = None
    usb_name: str | None = None


class SystemDevices:
    """The devices of this computer, through platform.devices() (mac_storage on macOS,
    gio_storage on Linux). The tests give the engine one of their own with the same names."""

    def scan(self) -> list[Seen]:
        from openexpress import platform
        from openexpress.device.found import usb_place

        found, holders = platform.devices().poll()
        return [Seen(f.uri, f, holders.get(usb_place(f.usb))) for f in found]

    def probe(self, found):
        """What its GarminDevice.xml says (found.DeviceSummary); the device stays open, let go
        as after a request (release)."""
        from openexpress import platform

        return platform.devices().probe(found)

    def hold(self, found) -> None:
        """The device opened for a request: on macOS the session kept open since the request
        before, or opened now (what the request opens then is that one); nothing elsewhere."""
        from openexpress import platform

        platform.devices().hold(found)

    def back(self, found) -> bool | None:
        """Whether a device that stopped responding answers again (opened now, its
        transaction cancelled: ptp.Session.open); None where none comes back so (gvfs, on
        Linux, only once it is plugged in again)."""
        from openexpress import platform

        if not platform.is_mac() or getattr(found, "kind", "mtp") == "ums":
            return None  # a drive has no session to open again
        return platform.devices().hold(found) is not None

    def look_again(self) -> None:
        """The next scan looks at who holds the devices again (a request found one held)."""
        from openexpress import platform

        platform.devices().look_again()

    def wanted(self, found) -> bool:
        """Whether another process of the program waits for this device, which this one
        holds."""
        from openexpress import platform

        return platform.devices().wanted_elsewhere(found)

    def options(self, found) -> dict:
        """How the command line names the device (its options --device-uri, --path)."""
        if getattr(found, "kind", "mtp") == "ums":
            return {}  # a drive: the one there is (service.open_device_or_drive)
        return {"device_uri": found.uri}

    def release(self, found) -> None:
        """The device let go: on macOS its session closed (the camera service goes on)."""
        from openexpress import platform

        platform.devices().close_found(found)


def unit_id_by_serial(serial: str | None, known) -> int | None:
    """The unit ID of a remembered device whose USB serial number is that unit ID (in
    hexadecimal or in decimal), for a device another program holds, whose GarminDevice.xml
    cannot be read; None when none matches."""
    if not serial:
        return None
    text = serial.strip().lstrip("0").casefold()
    for unit_id in known:
        if text and text in (f"{unit_id:x}", str(unit_id)):
            return unit_id
    return None


# --- requests --------------------------------------------------------------------------

@dataclasses.dataclass
class Request:
    id: object
    method: str
    params: dict
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event)
    # Whether it writes to the device: then a step in bytes is no safe point (_safe_point).
    writes: bool = False
    lines: list[str] = dataclasses.field(default_factory=list)


class _Turns:
    """The requests of one device, one at a time in the order they came. A request waiting
    for its turn stops at once when it is cancelled."""

    def __init__(self):
        self._cond = threading.Condition()
        self._waiting: deque = deque()
        self._busy = False

    def enter(self, cancel: threading.Event) -> None:
        with self._cond:
            me = object()
            self._waiting.append(me)
            while self._busy or self._waiting[0] is not me:
                if cancel.is_set():
                    self._waiting.remove(me)
                    self._cond.notify_all()
                    raise Cancelled
                self._cond.wait(0.1)
            self._waiting.popleft()
            self._busy = True

    def try_enter(self) -> bool:
        """The turn taken now if nobody has it or waits for it; whether it was."""
        with self._cond:
            if self._busy or self._waiting:
                return False
            self._busy = True
            return True

    def leave(self) -> bool:
        """Its turn over; whether nobody waits for the next."""
        with self._cond:
            self._busy = False
            self._cond.notify_all()
            return not self._waiting

    @contextlib.contextmanager
    def turn(self, cancel: threading.Event, then: Callable[[], None] | None = None):
        """The block in its turn; `then` once it is over and nobody waits (the device let go)."""
        self.enter(cancel)
        try:
            yield
        finally:
            if self.leave() and then is not None:
                try:
                    then()
                except Exception as e:  # noqa: BLE001 - the request's answer stands
                    log.info("engine: device not let go (%s)", type(e).__name__)


# The words of a step (progress.py) each phase starts with; anything else is the method's.
_PHASES = (("downloading", "downloading_text"), ("copying", "copying_text"),
           ("writing", "writing_text"), ("checking", "checking_text"))
_METHOD_PHASES = {"device.backup": "backup", "device.sync": "sync"}


def _phase_patterns() -> list[tuple[str, re.Pattern]]:
    """The phases with a pattern of their step's words in the language of the moment."""
    from openexpress import progress

    out = []
    for phase, name in _PHASES:
        words = getattr(progress, name)("\0")
        before, _sep, after = words.partition("\0")
        out.append((phase, re.compile(re.escape(before) + "(.*)" + re.escape(after) + "$",
                                      re.DOTALL)))
    return out


def phase_of(head: str, method: str) -> tuple[str, str | None]:
    """(phase, item) of a step's head: "Downloading TopoActive" is ("downloading",
    "TopoActive"); a head of another kind the method's phase, with the head as the item."""
    for phase, pattern in _phase_patterns():
        found = pattern.match(head)
        if found:
            return phase, found.group(1)
    return _METHOD_PHASES.get(method, "working"), head


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _missing(name: str) -> EngineError:
    return EngineError("invalid_params", _("Missing parameter: {name}").format(name=name))


def _invalid(name: str) -> EngineError:
    return EngineError("invalid_params", _("Invalid value for {name}.").format(name=name))


def _flag(params: dict, name: str, default: bool = False) -> bool:
    value = params.get(name, default)
    if not isinstance(value, bool):
        raise _invalid(name)
    return value


_REQUIRED = object()


def _text(params: dict, name: str, default=_REQUIRED):
    """The non-empty string `name`. Without a `default` anything else is missing; with one, it
    is what an absent `name` gives, and anything else is invalid."""
    value = params.get(name)
    if isinstance(value, str) and value:
        return value
    if default is _REQUIRED:
        raise _missing(name)
    if value is None:
        return default
    raise _invalid(name)


def _count(params: dict, name: str, default: int) -> int:
    """The positive whole number `name` (not a bool), `default` when it is not given."""
    value = params.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _invalid(name)
    return value


def _apply_option(req) -> list[str]:
    """The request's `apply` as the command line's option; a request that applies writes to
    the device (req.writes)."""
    req.writes = _flag(req.params, "apply")
    return ["--apply"] if req.writes else []


def _names(params: dict, name: str) -> list[str]:
    value = params.get(name)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value or not all(
            isinstance(v, str) and v for v in value):
        raise _missing(name) if value is None else _invalid(name)
    return value


def error_of(e: BaseException) -> EngineError:
    """The error response of an exception, by the same classes the command line tells apart
    (cli._run): a stable code, and the message the command line would show."""
    import requests

    from openexpress import service
    from openexpress.auth import sso, store
    from openexpress.connect import ApiError, ClockSkew, Unauthorized
    from openexpress.device import found
    from openexpress.device.storage import DeviceError, DeviceNotResponding
    from openexpress.errors import OrdinaryStop, UserFacingError
    from openexpress.settings import SetupError

    if isinstance(e, EngineError):
        return e
    if isinstance(e, Cancelled):
        return EngineError("cancelled", _("The request was cancelled."))
    if isinstance(e, found.DeviceInUse):
        return EngineError("device_busy", str(e), {"program": e.holder.name})
    table = (
        (found.SeveralDevices, "several_devices"),
        ((found.MassStorageNotice, found.UnsupportedDevice), "device_unsupported"),
        (found.NotThereYet, "no_device"),
        (DeviceNotResponding, "device_not_responding"),
        (store.NotLoggedIn, "not_signed_in"),
        (SetupError, "setup_needed"),
        (sso.LoginCancelled, "invalid_ticket"),
        (service.FirstSyncChoiceNeeded, "first_sync_choice"),
        (Unauthorized, "unauthorized"),
        (ClockSkew, "clock_skew"),
        (ApiError, "server_error"),
        (OrdinaryStop, "stopped"),
        (DeviceError, "device_error"),
        (UserFacingError, "failed"),
    )
    for classes, code in table:
        if isinstance(e, classes):
            return EngineError(code, str(e))
    from openexpress import applog

    if isinstance(e, requests.RequestException):
        return EngineError("network", _("Network error: could not reach the Garmin server "
                                        "({error})").format(
            error=applog.network_error_text(e)))
    log.info("engine: unexpected error: %s", type(e).__name__, exc_info=e)
    return EngineError("internal", applog.redact(str(e) or type(e).__name__))


# --- the engine ----------------------------------------------------------------------------

class Engine:
    """The request loop (serve), the requests' threads and the polling of the devices."""

    def __init__(self, out, devices=None, poll_s: float | None = POLL_S,
                 keep_open_s: float = KEEP_OPEN_S):
        self._out = out
        self._out_lock = threading.Lock()
        self.devices = devices or SystemDevices()
        self.poll_s = poll_s
        # The devices that may be open (a request on them, or one ended less than keep_open_s
        # ago): by key, the found device and the count of the uses of it, which tells the
        # timer of an earlier use that a later one came (_let_go_later). 0: let go at once.
        self.keep_open_s = keep_open_s
        self._open: dict[str, tuple[object, int]] = {}
        self._open_lock = threading.Lock()
        self._let_go_at_end: set[str] = set()  # to let go once the request on it ends
        self._attached: dict[str, Attached] = {}
        self._missing: dict[str, int] = {}  # attached devices not seen: in how many looks
        self._probing: set[str] = set()  # devices whose probe goes on after a look
        self._seen: set[str] = set()  # what the last look saw
        self.probe_wait_s = PROBE_WAIT_S
        self._scanned = False
        self._scan_lock = threading.Lock()
        self._turns: dict[str, _Turns] = {}
        self._turns_lock = threading.Lock()
        self._account = _Turns()  # account and settings: one at a time
        # What activities.list read of each device's activity files, by unit ID (as
        # activities.DeviceActivities keeps it), loaded from disk at the first call for the
        # device and written back after each call that read files (activities.save_summaries):
        # a later call, or a later engine, reads only the files that are new or changed, and
        # a cancelled call is resumed.
        self._activities: dict[int, dict] = {}
        self._running: dict[int, tuple[Request, threading.Thread]] = {}
        self._running_lock = threading.Lock()
        self._stop = threading.Event()
        # name: (how it runs, function). "now": on the reader's thread; "free": a thread of
        # its own; "account": in the turns of the account; "device": in the device's turns.
        self.methods: dict[str, tuple[str, Callable]] = {
            "hello": ("now", self.hello),
            "cancel": ("now", self.cancel),
            "account.status": ("account", self.account_status),
            "account.setup": ("account", self.account_setup),
            "account.sign_in_url": ("account", self.account_sign_in_url),
            "account.sign_in": ("account", self.account_sign_in),
            "account.sign_out": ("account", self.account_sign_out),
            "devices.list": ("free", self.devices_list),
            "device.info": ("device", self.device_info),
            "device.backup": ("device", self.device_backup),
            "device.sync": ("device", self.device_sync),
            "updates.list": ("device", self.updates_list),
            "updates.install": ("device", self.updates_install),
            "maps.list": ("device", self.maps_list),
            "maps.install": ("device", self.maps_install),
            "maps.update": ("device", self.maps_update),
            "maps.remove": ("device", self.maps_remove),
            "maps.move": ("device", self.maps_move),
            "maps.card_reader": ("free", self.maps_card_reader),
            "apps.list": ("device", self.apps_list),
            "apps.install": ("device", self.apps_install),
            "apps.remove": ("device", self.apps_remove),
            "activities.list": ("device", self.activities_list),
            "activities.track": ("free", self.activities_track),
            "backups.list": ("free", self.backups_list),
            "account.readings": ("account", self.account_readings),
            "settings.get": ("account", self.settings_get),
            "settings.set": ("account", self.settings_set),
            "strava.status": ("account", self.strava_status),
            "strava.connect": ("account", self.strava_connect),
            "strava.disconnect": ("account", self.strava_disconnect),
            "strava.upload": ("account", self.strava_upload),
            "shutdown": ("now", None),
        }

    # --- the wire ------------------------------------------------------------------------

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False, default=str)
        with self._out_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def event(self, name: str, params: dict) -> None:
        self.send({"event": name, "params": params})

    def respond(self, request_id, result=None, error: EngineError | None = None) -> None:
        if error is not None:
            body = {"code": error.code, "message": error.message}
            if error.data:
                body["data"] = error.data
            self.send({"id": request_id, "error": body})
        else:
            self.send({"id": request_id, "result": {} if result is None else result})

    def serve(self, stdin) -> int:
        """Reads requests until `shutdown` or the end of stdin; what is printed meanwhile is
        routed (_Routed). Returns the exit code."""
        streams = sys.stdout, sys.stderr
        sys.stdout = _Routed(streams[1], "info")
        sys.stderr = _Routed(streams[1], "warning")
        poller = None
        if self.poll_s:
            poller = threading.Thread(target=self._poll, name="engine-poll", daemon=True)
            poller.start()
        try:
            for line in iter(stdin.readline, ""):
                if not line.strip():
                    continue
                if self.handle_line(line) == "shutdown":
                    return 0
            self._finish()
            return 0
        finally:
            self._stop.set()
            self._let_go_all()
            sys.stdout, sys.stderr = streams

    def handle_line(self, line: str) -> str | None:
        """One line of the app; "shutdown" once the engine is to end."""
        try:
            message = json.loads(line)
        except ValueError:
            self.respond(None, error=EngineError("parse_error",
                                                 _("The request is not valid JSON.")))
            return None
        request_id = message.get("id") if isinstance(message, dict) else None
        if (not isinstance(message, dict) or not isinstance(message.get("method"), str)
                or "id" not in message or not isinstance(message.get("params", {}), dict)):
            self.respond(request_id, error=EngineError(
                "invalid_request", _("A request needs an id, a method and its params as an "
                                     "object.")))
            return None
        req = Request(request_id, message["method"], message.get("params") or {})
        how = self.methods.get(req.method)
        if how is None:
            self.respond(req.id, error=EngineError(
                "unknown_method", _("Unknown method: {method}").format(method=req.method)))
            return None
        kind, function = how
        if req.method == "shutdown":
            self._finish(cancel=True)
            self.respond(req.id, {})
            return "shutdown"
        if kind == "now":
            self._answer(req, function)
            return None
        thread = threading.Thread(target=self._run, args=(req, kind, function),
                                  name=f"engine-{req.method}", daemon=True)
        with self._running_lock:
            self._running[id(req)] = (req, thread)
        thread.start()
        return None

    def _answer(self, req: Request, function) -> None:
        try:
            result = function(req)
        except BaseException as e:  # noqa: BLE001 - every request has its answer
            self.respond(req.id, error=error_of(e))
            return
        self.respond(req.id, result)

    def _run(self, req: Request, kind: str, function) -> None:
        from openexpress import cli

        def say(level: str, line: str) -> None:
            req.lines.append(line)
            self.event("log", {"level": level, "message": line, "request": req.id})

        token = cli.PROGRESS_TO.set(self.progress_for(req))
        try:
            with _Routed.route(say):
                if kind == "account":
                    self._answer(req, lambda r: self._in_turn(self._account, r, function))
                elif kind == "device":
                    self._answer(req, lambda r: self._on_device(r, function))
                else:
                    self._answer(req, function)
        finally:
            cli.PROGRESS_TO.reset(token)
            with self._running_lock:
                self._running.pop(id(req), None)

    @staticmethod
    def _in_turn(turns: _Turns, req: Request, function):
        with turns.turn(req.cancel):
            return function(req)

    def _finish(self, cancel: bool = False) -> None:
        """Waits for the requests still running; `cancel`: stops them first."""
        with self._running_lock:
            running = list(self._running.values())
        if cancel:
            for req, _thread in running:
                req.cancel.set()
        deadline = time.monotonic() + SHUTDOWN_WAIT_S
        for _req, thread in running:
            thread.join(max(0.0, deadline - time.monotonic()))

    # --- progress and cancelling --------------------------------------------------------

    def progress_for(self, req: Request) -> Callable[[str], None]:
        """The progress callback of a request (what cli._progress gives its command, and
        what the modules are given): steps as `progress` events, other messages as `log`
        events; a cancelled request stops at the first safe point."""

        def show(message: str) -> None:
            if isinstance(message, TimedWait):
                self.event("progress", {"request": req.id, "phase": "waiting",
                                        "done": round(message.done * 100), "total": 100,
                                        "item": str(message.short)})
            elif isinstance(message, Step):
                phase, item = phase_of(message.head, req.method)
                done, total = message.measure
                params = {"request": req.id, "phase": phase, "done": done, "total": total}
                if item:
                    params["item"] = item
                if (message.bytes_done is not None and message.bytes_total) or message.in_bytes:
                    params["unit"] = "bytes"  # done and total are bytes: the app shows a speed
                self.event("progress", params)
            else:
                req.lines.append(str(message))
                self.event("log", {"level": "info", "message": str(message),
                                   "request": req.id})

        reporter = Reporter(show, every=PROGRESS_EVERY_S)

        def progress(message: str) -> None:
            if req.cancel.is_set() and self._safe_point(req, message):
                raise Cancelled
            reporter(message)

        return progress

    @staticmethod
    def _safe_point(req: Request, message: str) -> bool:
        """Whether a request may stop at this progress message: always, unless it writes to
        the device and the message is a step in bytes of a file being written (a download is
        no write: it is stopped anywhere)."""
        if not req.writes or not isinstance(message, Step):
            return True
        if not message.in_bytes and message.bytes_done is None:
            return True
        return phase_of(message.head, req.method)[0] == "downloading"

    @staticmethod
    def checkpoint(req: Request) -> None:
        """A safe point between two items of a request."""
        if req.cancel.is_set():
            raise Cancelled

    def cancel(self, req: Request) -> dict:
        wanted = req.params.get("request")
        if wanted is None:
            raise _missing("request")
        with self._running_lock:
            found = [r for r, _t in self._running.values() if r.id == wanted]
        for r in found:
            r.cancel.set()
        return {"cancelling": bool(found)}

    # --- devices ----------------------------------------------------------------------

    def _turns_of(self, key: str) -> _Turns:
        with self._turns_lock:
            return self._turns.setdefault(key, _Turns())

    def _poll(self) -> None:
        """USB looked at every poll_s, the first time after one interval: the app asks
        devices.list as it starts, and an engine asked only `hello` touches no device."""
        while not self._stop.wait(self.poll_s):
            try:
                self.scan()
            except Exception as e:  # noqa: BLE001 - polling goes on
                log.info("engine: devices not looked at (%s)", type(e).__name__)

    def scan(self) -> list[Attached]:
        """The devices looked at again: events for those that came, went, or were taken or
        let go by another program. What is attached now."""
        from openexpress import known_devices

        with self._scan_lock:
            seen = {s.key: s for s in self.devices.scan()}
            self._seen = set(seen)
            known = None
            for key in [k for k in self._attached if k not in seen]:
                self._missing[key] = self._missing.get(key, 0) + 1
                if self._missing[key] < GONE_AFTER_LOOKS:
                    continue
                del self._missing[key]
                gone = self._attached.pop(key)
                self._let_go_now(key)
                self.event("device.detached", self.device_json(gone))
            for key in seen:
                self._missing.pop(key, None)
            for key, (found, _uses) in list(self._open.items()):
                if self.devices.wanted(found):  # that one alone: the others stay open
                    log.info("engine: another openexpress process waits for a device; let go")
                    self._let_go_now(key)
            to_probe: list[Attached] = []
            for key, s in seen.items():
                if key in self._probing:
                    continue  # told once its probe is over
                before = self._attached.get(key)
                if s.holder is not None:
                    self._let_go_now(key)  # taken from us: our session is of no use
                if before is not None and before.busy_by == s.holder:
                    continue
                a = before or Attached(key, s.found,
                                       usb_name=getattr(s.found, "name", None))
                a.found, a.busy_by = s.found, s.holder
                if s.holder is not None:
                    if a.unit_id is None:
                        known = known if known is not None else known_devices.load()
                        a.unit_id = unit_id_by_serial(
                            getattr(getattr(s.found, "usb", None), "serial", None), known)
                        remembered = known.get(a.unit_id) if a.unit_id else None
                        if remembered is not None and not a.description:
                            # Its name as when it was last read (a fēnix has no USB name).
                            a.description = remembered.description
                            a.part_number = remembered.part_number
                    self._attached[key] = a
                    self.event("device.busy", {
                        "device": str(a.unit_id) if a.unit_id else None,
                        "program": s.holder, "name": a.description or a.usb_name})
                    continue
                to_probe.append(a)
                self._probing.add(key)
            self._scanned = True
        self._probe_each(to_probe)  # without the lock: a probe over attaches with it
        with self._scan_lock:
            return list(self._attached.values())

    def _probe_each(self, devices: list[Attached]) -> None:
        """The devices that came, probed side by side, each attached and told as soon as its
        own probe is over: a device that does not answer (one still starting, or stuck after
        its USB connection reset until its transaction is cancelled) holds up no other, nor
        the look for longer than probe_wait_s; its probe goes on and tells it then."""

        def probe(a: Attached) -> None:
            try:
                self._probe(a)
            finally:
                with self._scan_lock:
                    self._probing.discard(a.key)
                    if a.key in self._seen:  # not gone while it was probed
                        self._attached[a.key] = a
                        self.event("device.attached", self.device_json(a))

        threads = [threading.Thread(target=probe, args=(a,), name=f"probe {a.key}", daemon=True)
                   for a in devices]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + self.probe_wait_s
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _probe(self, a: Attached) -> None:
        """What its GarminDevice.xml says, in the device's turn (no request is on it); the
        device stays open as after a request, for the requests the app sends as it comes."""
        with self._turns_of(a.key).turn(threading.Event(), then=lambda: self._let_go_later(a)):
            self._using(a)
            try:
                summary = self.devices.probe(a.found)
            except Exception as e:  # noqa: BLE001 - attached all the same, without details
                log.info("engine: device not probed (%s)", type(e).__name__)
                return
        if summary.unit_id:
            a.unit_id = summary.unit_id
            a.description, a.part_number = summary.description, summary.part_number
            a.software = summary.software_version

    def attached(self) -> list[Attached]:
        if not self._scanned:
            self.scan()
        with self._scan_lock:
            return list(self._attached.values())

    def resolve(self, params: dict) -> Attached:
        """The connected device a request names by its unit ID (`device`); the only one
        connected when it names none."""
        wanted = params.get("device")
        devices = self.attached()
        if wanted is None:
            if len(devices) == 1:
                return devices[0]
            if not devices:
                raise EngineError("no_device", _("No Garmin device is connected."))
            raise EngineError("several_devices", _("Several devices are connected: name one "
                                                   "with its unit ID."))
        wanted = str(wanted)
        for a in devices:
            if a.unit_id is not None and str(a.unit_id) == wanted:
                return a
        raise EngineError("no_device", _("No connected device has the unit ID "
                                         "{device}.").format(device=wanted),
                          {"device": wanted})

    def _on_device(self, req: Request, function):
        from openexpress.device.found import DeviceInUse
        from openexpress.device.storage import DeviceNotResponding

        a = self.resolve(req.params)
        if a.busy_by is not None:
            raise EngineError("device_busy", _("{program} is using the device. Quit it, then "
                                               "try again.").format(program=a.busy_by),
                              {"program": a.busy_by, "device": str(a.unit_id or "")})
        with self._turns_of(a.key).turn(req.cancel, then=lambda: self._let_go_later(a)):
            self.checkpoint(req)
            try:
                self._using(a)
                self._hold(a)
                try:
                    return function(req, a)
                except DeviceNotResponding:
                    if req.method not in RETRIED_ONCE or req.writes or not self._came_back(req, a):
                        raise
                    return function(req, a)
            except DeviceInUse:
                self.devices.look_again()  # the next scan tells it busy
                raise

    def _came_back(self, req: Request, a: Attached) -> bool:
        """Whether the device that stopped responding in the middle of `req` answers again
        within COME_BACK_WAIT_S, asked each second (cancel stops the wait)."""
        log.info("engine: the device stopped responding during %s; waiting for it", req.method)
        deadline = time.monotonic() + COME_BACK_WAIT_S
        while time.monotonic() < deadline:
            self.checkpoint(req)
            try:
                back = self.devices.back(a.found)
            except Exception as e:  # noqa: BLE001 - not back yet
                log.info("engine: the device is not back yet (%s)", type(e).__name__)
                back = False
            if back is None:
                return False  # nothing to wait for here
            if back:
                log.info("engine: the device answers again; %s once more", req.method)
                return True
            req.cancel.wait(1.0)
        return False

    def _hold(self, a: Attached) -> None:
        """The device open for the request (SystemDevices.hold): kept since the request
        before, or opened now with what the poll found. A failure is left to the request,
        which opens the device as the command line does and says why it cannot."""
        from openexpress.device.found import DeviceInUse
        from openexpress.device.storage import DeviceNotResponding

        try:
            self.devices.hold(a.found)
        except (DeviceInUse, DeviceNotResponding):
            raise
        except Exception as e:  # noqa: BLE001 - the request's own opening tells it
            log.info("engine: device not opened before the request (%s)", type(e).__name__)

    # --- the devices kept open between requests -------------------------------------------

    def _using(self, a: Attached) -> None:
        """A use of the device begins, in its turn: a timer of an earlier one lets it be."""
        with self._open_lock:
            _found, uses = self._open.get(a.key, (None, 0))
            self._open[a.key] = (a.found, uses + 1)

    def _let_go_later(self, a: Attached) -> None:
        """Once the last request on the device ended (nobody waits for its turn): let go after
        keep_open_s unless another use came; at once when that was asked meanwhile, when the
        device is gone or the engine ends, or keep_open_s is 0."""
        with self._open_lock:
            now = (not self.keep_open_s or self._stop.is_set() or a.key in self._let_go_at_end
                   or a.key not in self._open)
            self._let_go_at_end.discard(a.key)
            if now:
                self._open.pop(a.key, None)
            else:
                found, uses = self._open[a.key]
                self._open[a.key] = (found, uses + 1)
        if now:
            self._release(a.found)
            return
        timer = threading.Timer(self.keep_open_s, self._let_go_if_idle, (a.key, uses + 1))
        timer.daemon = True
        timer.start()

    def _let_go_if_idle(self, key: str, uses: int) -> None:
        """The timer of _let_go_later: the device let go if no use came since."""
        turns = self._turns_of(key)
        if not turns.try_enter():
            return  # a request has it: its end lets it go later
        try:
            with self._open_lock:
                found, now = self._open.get(key, (None, None))
                if now != uses:
                    return
                del self._open[key]
            self._release(found)
        finally:
            turns.leave()

    def _let_go_now(self, key: str) -> None:
        """The device let go now if it is open and no request is on it; else once the request
        on it ends."""
        with self._open_lock:
            if key not in self._open:
                return
        turns = self._turns_of(key)
        if not turns.try_enter():
            with self._open_lock:
                self._let_go_at_end.add(key)
            return
        try:
            with self._open_lock:
                found, _uses = self._open.pop(key, (None, 0))
            if found is not None:
                self._release(found)
        finally:
            turns.leave()

    def _let_go_all(self) -> None:
        """Every device open let go (the engine ends): those with a request still on it once
        it ends."""
        for key in list(self._open):
            self._let_go_now(key)

    def _release(self, found) -> None:
        try:
            self.devices.release(found)
        except Exception as e:  # noqa: BLE001 - what asked for it goes on
            log.info("engine: device not let go (%s)", type(e).__name__)

    def device_json(self, a: Attached, storage=None, known: dict | None = None,
                    backups: Path | None = None) -> dict:
        """A `Device` (docs/ENGINE.md, Objects). `known` (known_devices.load()) and `backups`
        (the backup folder) when the caller has them already, for a list of devices."""
        from openexpress import known_devices

        if known is None and a.unit_id:
            known = known_devices.load()
        entry = known.get(a.unit_id) if a.unit_id else None
        out = self._device_fields(a.unit_id, entry, a.description or a.usb_name or "",
                                  a.part_number, a.software, backups)
        if storage is not None:
            out["storage"] = {"free": storage.free_space(), "capacity": storage.capacity()}
        out.update(connected=True, busy_by=a.busy_by)
        return out

    def _device_fields(self, unit_id, entry, description, part_number, software,
                       backups: Path | None = None) -> dict:
        from openexpress import service
        from openexpress.backup import backups_of

        last = service.load_last_sync(unit_id) if unit_id else None
        snapshots = backups_of(backups or self._backups(), unit_id) if unit_id else []
        return {
            "unit_id": str(unit_id) if unit_id else None,
            "name": entry.shown_name if entry is not None else description,
            "part_number": part_number or (entry.part_number if entry else ""),
            "software": software or (entry.software_version if entry else ""),
            "connected": False, "busy_by": None, "storage": None,
            "last_sync": _iso(last.get("time")) if last else None,
            "last_backup": _iso(snapshots[0][0].timestamp()) if snapshots else None,
        }

    def args_for(self, a: Attached, cmd: str, **values) -> argparse.Namespace:
        """The arguments of a command of the command line on this device (cli.open_device)."""
        names = {"path": None, "device_uri": None, "no_wait": False, "cmd": cmd,
                 "apply": False, "verbose": False}
        names.update(self.devices.options(a.found))
        names.update(values)
        return argparse.Namespace(**names)

    def command_line(self, a: Attached) -> list[str]:
        """The device's options for the command line, before the command."""
        out = []
        for name, value in self.devices.options(a.found).items():
            if value:
                out += ["--" + name.replace("_", "-"), str(value)]
        return out

    def run_command(self, req: Request, argv: list[str]) -> dict:
        """A command of the command line run as it is (argv: after `openexpress`), its lines
        sent as `log` events: {"exit_code", "lines"}. An exception is the request's error."""
        from openexpress import cli

        args = cli.build_parser().parse_args(argv)
        before = len(req.lines)
        code = args.func(args)
        return {"exit_code": code, "lines": req.lines[before:]}

    # --- methods: the engine -------------------------------------------------------------

    def hello(self, req: Request) -> dict:
        return {"engine": __version__, "protocol": PROTOCOL, "platform": sys.platform,
                "methods": sorted(self.methods)}

    # --- methods: the account and the settings -------------------------------------------

    def account_status(self, req: Request) -> dict:
        from openexpress.auth import store
        from openexpress.settings import has_consumer

        try:
            auth = store.load()
        except store.NotLoggedIn:
            auth = None
        return {"signed_in": auth is not None,
                "account": auth.masked_display if auth is not None else None,
                "environment": auth.environment if auth is not None else None,
                "setup_done": has_consumer()}

    def account_setup(self, req: Request) -> dict:
        """As `setup --from-installer`: the application key read from GarminExpress.exe."""
        from openexpress.installer import consumer_from_installer
        from openexpress.settings import save_consumer

        installer = _text(req.params, "installer")
        save_consumer(consumer_from_installer(Path(installer).expanduser()))
        return {}

    def account_sign_in_url(self, req: Request) -> dict:
        from openexpress.auth import sso
        from openexpress.settings import Settings

        return {"url": sso.sign_in_url(Settings.load())}

    def account_sign_in(self, req: Request) -> dict:
        """As `login --manual`, with the address of the page after the sign-in given: its
        ticket exchanged for the token, the account's profile read and the token saved."""
        from openexpress import service
        from openexpress.auth import sso, store
        from openexpress.settings import Settings

        url = req.params.get("ticket_url")
        if not isinstance(url, str) or not url.strip():
            raise _missing("ticket_url")
        ticket = sso.ticket_from_url(url.strip())
        if ticket is None:
            raise EngineError("invalid_ticket", _("The given URL has no ticket=ST-… parameter"))
        settings = Settings.load()
        client = service.make_client(settings, need_auth=False)
        token, secret = client.exchange_ticket(ticket.ticket, ticket.service)
        auth = store.ConnectAuth(token, secret, settings.environment)
        client.auth = auth
        try:
            profile = client.user_profile()
            auth.account = {k: profile.get(k) for k in
                            ("userprofilepk", "username", "emailaddress", "garminguid")}
        except Exception as e:  # noqa: BLE001 - the token is valid even without the profile
            log.warning("engine: the profile was not read (%s)", type(e).__name__)
        store.save(auth)
        return {"account": auth.masked_display}

    def account_sign_out(self, req: Request) -> dict:
        from openexpress.auth import store

        return {"removed": store.delete()}

    # The settings, by the names of `setup`'s options: (field of Settings, kind); a field
    # None is kept beside them in the settings file (Settings keeps keys it does not know).
    SETTINGS: ClassVar[dict[str, tuple[str | None, type]]] = {
        "environment": ("environment", str),
        "locale": ("chosen_locale", str),
        "ui_language": (None, str),
        "diagnostics_copy": ("diagnostics_copy", bool),
        "copy_fit_to": ("copy_fit_to", str),
        "copy_fit_types": ("copy_fit_types", str),
        "strava_upload": ("strava_upload", bool),
        "backup_dir": (None, str),
        "backup_keep": (None, int),
    }

    @staticmethod
    def _setting(settings, key: str):
        from openexpress import i18n

        field, _kind = Engine.SETTINGS[key]
        if key == "ui_language":
            return i18n.saved_ui_language()
        if field is None:
            return settings._extra.get(key)
        return getattr(settings, field)

    def settings_get(self, req: Request) -> dict:
        from openexpress.settings import Settings

        settings = Settings.load()
        key = req.params.get("key")
        if key is None:
            return {"settings": {k: self._setting(settings, k) for k in self.SETTINGS}}
        if key not in self.SETTINGS:
            raise EngineError("unknown_setting", _("Unknown setting: {key}").format(key=key))
        return {"key": key, "value": self._setting(settings, key)}

    def settings_set(self, req: Request) -> dict:
        from openexpress import i18n
        from openexpress.settings import ENVIRONMENTS, Settings

        key = req.params.get("key")
        if key not in self.SETTINGS:
            raise EngineError("unknown_setting", _("Unknown setting: {key}").format(
                key=key)) if key is not None else _missing("key")
        if "value" not in req.params:
            raise _missing("value")
        value = req.params["value"]
        field, kind = self.SETTINGS[key]
        if value is not None and (not isinstance(value, kind) or (
                kind is int and (isinstance(value, bool) or value < 1))):
            raise _invalid(key)
        if value is None and kind is not str and field is not None:
            raise _invalid(key)
        if key == "environment" and value not in ENVIRONMENTS:
            raise _invalid(key)
        if key == "ui_language":
            i18n.save_ui_language(value or "")
            return {"key": key, "value": value or ""}
        settings = Settings.load()
        if key in ("copy_fit_to", "backup_dir") and value:
            value = str(Path(value).expanduser())
        if field is None:
            if value is None:
                settings._extra.pop(key, None)
            else:
                settings._extra[key] = value
        else:
            setattr(settings, field, value if value is not None else "")
        settings.save()
        return {"key": key, "value": self._setting(Settings.load(), key)}

    def strava_status(self, req: Request) -> dict:
        from openexpress import strava

        return dataclasses.asdict(strava.status())

    def strava_connect(self, req: Request) -> dict:
        """The app's own browser flow (strava.py): `client_id` and `client_secret` of the
        user's Strava API application saved when given; with `code` (from Strava's redirect
        to `redirect_uri`) the token asked for and saved; without it the address of Strava's
        consent page for `redirect_uri` given back (`authorize_url`), to open."""
        from openexpress import strava

        p = req.params
        client_id, secret = p.get("client_id"), p.get("client_secret")
        if client_id or secret:
            if not (isinstance(client_id, str) and client_id
                    and isinstance(secret, str) and secret):
                raise _invalid("client_id" if not client_id else "client_secret")
            strava.save_client(strava.Client(client_id, secret))
        redirect_uri = _text(p, "redirect_uri")
        code = _text(p, "code", None)
        if code is None:
            return {**dataclasses.asdict(strava.status()),
                    "authorize_url": strava.authorize_url(redirect_uri)}
        return dataclasses.asdict(strava.connect_with_code(code, redirect_uri))

    def strava_disconnect(self, req: Request) -> dict:
        from openexpress import strava

        return {"removed": strava.disconnect()}

    def strava_upload(self, req: Request) -> dict:
        """As `strava upload FILE… [--apply]`: FIT files of this computer sent to Strava."""
        from openexpress import strava

        files = _names(req.params, "files")
        apply = _flag(req.params, "apply")
        return dataclasses.asdict(strava.upload_files(files, apply=apply,
                                                      progress=self.progress_for(req)))

    @staticmethod
    def _strava_after_sync(after) -> dict | None:
        """The Strava part of service.after_sync: in a dry run how many files would be staged,
        else what was sent; None when Settings.strava_upload is off. Never an error of the
        sync (said in "error")."""
        if after.strava_error is not None:
            return {"error": error_of(after.strava_error).message}
        if after.strava is None:
            return None
        if after.strava.report is None:
            return {"files": after.strava.planned}
        return dataclasses.asdict(after.strava.report)

    # --- methods: devices ------------------------------------------------------------

    def devices_list(self, req: Request) -> dict:
        """The devices connected, then those remembered (known_devices) that are not."""
        from openexpress import known_devices

        connected = self.scan()
        # Read once for the whole list: the remembered devices and the backup folder.
        known, backups = known_devices.load(), self._backups()
        out = [self.device_json(a, known=known, backups=backups) for a in connected]
        here = {a.unit_id for a in connected if a.unit_id}
        for entry in known_devices.in_order(known):
            if entry.unit_id not in here:
                out.append(self._device_fields(entry.unit_id, entry, entry.description,
                                               entry.part_number, entry.software_version,
                                               backups))
        return {"devices": out}

    def device_info(self, req: Request, a: Attached) -> dict:
        from openexpress import cli

        st, dev = cli.open_device(self.args_for(a, "info"))
        a.description, a.part_number = dev.description, dev.part_number
        a.software, a.unit_id = dev.software_version_str, dev.unit_id
        out = self.device_json(a, st)
        out.update(storage_name=cli.storage_id(st), mass_storage=bool(st.mass_storage),
                   datatypes=dev.datatypes(), components=len(dev.update_files))
        return out

    def _backups(self) -> Path:
        from openexpress import paths
        from openexpress.settings import Settings

        chosen = Settings.load()._extra.get("backup_dir")
        return Path(chosen).expanduser() if chosen else paths.default_backup_dir()

    def device_backup(self, req: Request, a: Attached) -> dict:
        """As `backup`: a dry run counts the files; with apply they are copied."""
        from openexpress import cli
        from openexpress.backup import backup_files, run_backup
        from openexpress.settings import Settings

        apply = _flag(req.params, "apply")
        dest = _text(req.params, "dest", None)
        keep = _count(req.params, "keep", Settings.load()._extra.get("backup_keep", 3))
        st, dev = cli.open_device(self.args_for(a, "backup", apply=apply))
        root = Path(dest).expanduser() if dest else self._backups()
        if not apply:
            count = 1 + sum(1 for f in backup_files(dev) for p, _e in st.walk_files(f.path)
                            if f.matches(p))
            return {"dry_run": True, "dest": str(root), "files": count}
        res = run_backup(dev, st, root, keep=keep, progress=self.progress_for(req))
        return {"dry_run": False, "path": str(res.path), "files": res.files, "bytes": res.bytes,
                "copied": res.copied, "copied_bytes": res.copied_bytes, "linked": res.linked,
                "linked_bytes": res.linked_bytes, "removed": [str(p) for p in res.removed],
                "incomplete": res.incomplete}

    def device_sync(self, req: Request, a: Attached) -> dict:
        """As `sync`: the plan in a dry run; with apply the sync of the window and the
        command line (service.run_sync), then the FIT drop folder."""
        from openexpress import cli, last_sync, service
        from openexpress.auth import store
        from openexpress.settings import Settings

        apply = _flag(req.params, "apply")
        req.writes = apply
        types = req.params.get("types")
        if types is not None and (not isinstance(types, list)
                                  or not all(isinstance(t, str) for t in types)):
            raise _invalid("types")
        first_run = req.params.get("first_run")
        if first_run not in (None, "baseline", "all"):
            raise _invalid("first_run")
        st, dev = cli.open_device(self.args_for(a, "sync", apply=apply))
        settings = Settings.load()
        try:
            client = cli.make_client(settings, need_auth=True)
        except store.NotLoggedIn:
            if apply:
                raise
            client = cli.make_client(settings, need_auth=False)
        session = service.DeviceSession(st, dev, settings, client)
        options = service.SyncOptions(
            first_run=first_run, only_types=frozenset(types) if types else None,
            uploads=_flag(req.params, "uploads", True),
            downloads=_flag(req.params, "downloads", True), drive=st.mass_storage)
        if not apply:
            if st.mass_storage:
                service.refuse_drive_sync(session)
            plan = service.plan_sync(session, dataclasses.replace(options, preview=True))
            uploads = plan.uploads
            items = plan.queue.items if plan.queue is not None else []
            out = {"dry_run": True, "signed_in": client.auth is not None,
                   "first_sync": session.first_sync,
                   "uploads": None if uploads is None else {
                       "files": len(uploads.candidates), "already_done": uploads.already_done,
                       "bytes": sum(c.size for c in uploads.candidates),
                       "skipped_types": uploads.skipped_types},
                   "upload_error": plan.upload_error,
                   "uploads_allowed": plan.gate.allowed if plan.gate is not None else None,
                   "downloads": sum(1 for i in items if not i.unsupported
                                    and not service.stays_in_queue(i, st.mass_storage)),
                   "drop_folder": None}
            after = service.after_sync(session, apply=False)
            if after.drop_error is not None:
                raise after.drop_error
            if after.drop_folder is not None:
                out["drop_folder"] = {"path": str(after.drop_folder), "files": after.drop_planned}
            out["strava"] = self._strava_after_sync(after)
            return out
        progress = self.progress_for(req)
        summary = service.run_sync(session, progress=progress, options=options)
        told = last_sync.describe(summary.record(dev.unit_id))
        out = {"dry_run": False, "uploaded": summary.uploaded,
               "downloaded": summary.downloaded, "skipped_known": summary.skipped_known,
               "rejected": summary.rejected, "errors": summary.errors,
               "not_confirmed": summary.not_confirmed,
               "steps": [text for text, _warning in told.steps], "sentence": told.sentence,
               "drop_folder": None, "strava": None}
        # Before the drive is ejected: the device must still be open to read from.
        after = service.after_sync(session, apply=True, progress=progress)
        if after.drop_error is not None:
            raise after.drop_error
        dropped = after.dropped
        if dropped is not None:
            out["drop_folder"] = {"path": str(after.drop_folder), "copied": dropped.copied,
                                  "bytes": dropped.bytes, "incomplete": dropped.incomplete}
        out["strava"] = self._strava_after_sync(after)
        if st.mass_storage and summary.wrote_to_device:
            out["ejected"] = service.eject_drive(st, files=True)
        return out

    def updates_list(self, req: Request, a: Attached) -> dict:
        """As `updates`: what Garmin's update server offers the device, compared with it."""
        from openexpress import cli
        from openexpress import updates as upd
        from openexpress.connect import GarminClient
        from openexpress.settings import Settings

        st, dev = cli.open_device(self.args_for(a, "updates"))
        resp = GarminClient(Settings.load()).software_updates(dev.raw)
        items = upd.offered(st, resp, dev)
        if cli._read_hidden(st, dev, upd.hidden_needed(items), "updates go there"):
            items = upd.offered(st, resp, dev)
        items = [u for u in items if not u.is_reinstall]
        staged, untaken = upd.staged(items), upd.not_installed(items)
        new = upd.pending(items)

        def update(u, waiting: bool) -> dict:
            return {"part": u.part_number, "name": u.title, "version": u.version,
                    "installed": upd.the_device_version(u, dev), "size": u.size,
                    "beta": u.is_beta, "waiting_on_device": waiting,
                    "not_installed": any(u is n for n in untaken),
                    "system": u.is_system, "critical": u.severity.lower() == "critical",
                    "metadata": u.is_metadata, "changes": u.changes, "eula": u.eula_url or None,
                    "unreadable_folder": u.hidden_folder is not None and not u.hidden_read,
                    "older": upd.older_offer_text(u)}

        listed = [update(u, False) for u in new] + [update(u, True) for u in staged]
        listed += [update(u, False) for u in untaken]
        return {"updates": listed, "beta_offered": upd.beta_offered(resp),
                "state": upd.state(new, staged, untaken),
                "add_ons": [update(u, False) for u in upd.add_ons(items)]}

    def updates_install(self, req: Request, a: Attached) -> dict:
        """As `updates --install PART… [--apply]`."""
        apply = _apply_option(req)
        parts = req.params.get("parts")
        parts = ["all"] if parts == "all" else _names(req.params, "parts")
        return self.run_command(req, [*self.command_line(a), "updates", *apply, "--install",
                                      *parts])

    def maps_list(self, req: Request, a: Attached) -> dict:
        """As `maps [--all]`: Garmin's maps for the device, and the maps of your own."""
        from openexpress import cli, ownmaps
        from openexpress import maps as mp
        from openexpress.connect import GarminClient
        from openexpress.settings import Settings

        every = _flag(req.params, "all")
        st, dev, all_maps, listed = cli._load_maps(self.args_for(a, "maps"),
                                                   GarminClient(Settings.load()))
        free = st.free_space()
        storages = map_storages(st, dev)
        has_card = len(storages) > 1
        if not listed:
            return {"maps": [], "own_maps": [], "free": free, "storages": storages,
                    "message": mp.no_maps_message()}
        on_device = [m for m in all_maps if m.listed or m.waiting]
        shown = all_maps if every else on_device

        def entry(m) -> dict:
            status = ("update" if m.update_available else "up to date" if m.installed
                      else "available")
            return {"name": m.name, "version": m.version, "size": m.size,
                    "installed": m.installed, "status": status,
                    "installed_version": (m.version if status == "up to date"
                                          else installed_map_version(st, m, dev)
                                          if status == "update" else None),
                    "status_text": m.status,
                    "region": m.region or None, "type": m.map_type, "waiting": m.waiting,
                    "incomplete": m.incomplete, "restricted": m.restricted is True,
                    "can_install": mp.can_install(m),
                    "can_remove": m.can_uninstall and mp.removal_refused(m) is None,
                    "location": m.location,
                    "can_move": has_card and mp.move_refused(m) is None,
                    "move_size": mp.move_size(m, all_maps) if has_card else None,
                    # what an install would not download again, a move not copy anew
                    "cached_size": sum(f.size for f in m.files if mp.in_cache(f)),
                    "move_cached": mp.move_cached(m, all_maps) if has_card else None}

        maps = [entry(m) for m in sorted(
            shown, key=lambda m: (m not in on_device, m.map_type, m.name))]
        own = [{"file": o.filename, "size": o.size, "title": o.title, "path": o.path}
               for o in ownmaps.list_own(dev, st, all_maps)]
        return {"maps": maps, "own_maps": own, "free": free, "storages": storages,
                "more_available": len(all_maps) - len(on_device),
                "download_speed": mp.download_speed()}

    def _each_map(self, req: Request, a: Attached, command: str) -> dict:
        """maps.install, maps.remove: as `maps COMMAND NAME` for each map named, one after the
        other, with Garmin's maps for the device asked once (cli._load_maps) for all of them;
        before each map but the first, what the ones before changed on the device is looked
        at again (maps.mark_waiting), as a run of its own would. A map refused is said in its
        entry, and the next is done; the lines of the shared start are in each entry's."""
        from openexpress import cli
        from openexpress import maps as mp
        from openexpress.connect import GarminClient
        from openexpress.settings import Settings

        apply = _apply_option(req)
        names = _names(req.params, "maps")
        # maps.install: "internal" (the default) or "card"; maps.move: where the map goes
        to = req.params.get("to")
        if command == "move" and to is None:
            raise _missing("to")
        if (command == "remove" and to is not None) or to not in (None, "internal", "card"):
            raise _invalid("to")
        where = ["--to", to] if to else []
        # maps.install, maps.move: the device's card in this computer's card reader
        reader = req.params.get("card_reader")
        if reader is not None and (command == "remove" or not isinstance(reader, str)
                                   or not reader.strip()):
            raise _invalid("card_reader")
        if reader:
            where += ["--card-reader", reader]
        # maps.move: "device" (the default) or "download": where its files come from
        source = req.params.get("source")
        if source not in (None, "device", "download") or (source and command != "move"):
            raise _invalid("source")
        if source == "download":
            where.append("--download")
        args = cli.build_parser().parse_args([*self.command_line(a), "maps", command, *apply,
                                              *where, "--", names[0]])
        start = len(req.lines)
        try:
            client = GarminClient(Settings.load())
            st, dev, all_maps, listed = cli._load_maps(args, client)
            if not listed:
                raise mp.MapError(mp.no_maps_message())
            cli.print_map_cleanup(st, dev, all_maps)
        except Exception as e:  # noqa: BLE001 - the error of every map named
            error = error_of(e)
            return {"dry_run": not req.writes, "maps": [
                {"name": name, "done": False, "code": error.code, "reason": error.message}
                for name in names]}
        shared = req.lines[start:]
        out = []
        for n, name in enumerate(names):
            self.checkpoint(req)
            before = len(req.lines)
            try:
                if n:
                    mp.mark_waiting(st, all_maps, dev)
                code = cli.change_map(args, client, st, dev, all_maps, mp.find(all_maps, name))
            except Exception as e:  # noqa: BLE001 - the error of this map, then the next
                error = error_of(e)
                out.append({"name": name, "done": False, "code": error.code,
                            "reason": error.message})
                continue
            out.append({"name": name, "done": code == 0, "exit_code": code,
                        "lines": shared + req.lines[before:]})
        return {"dry_run": not req.writes, "maps": out}

    def maps_install(self, req: Request, a: Attached) -> dict:
        return self._each_map(req, a, "install")

    def maps_remove(self, req: Request, a: Attached) -> dict:
        return self._each_map(req, a, "remove")

    def maps_card_reader(self, req: Request) -> dict:
        """The memory cards in this computer's card reader (mounted volumes with a Garmin
        folder), each with whether it is this device's (`device`: its unit ID) and why not.
        Nothing is opened on the device: the app asks it while it waits for the card."""
        from openexpress.device import card as cards

        wanted = req.params.get("device")
        if not (isinstance(wanted, (str, int)) and str(wanted).isdigit()):
            raise _invalid("device")
        unit_id = int(wanted)
        return {"volumes": [{"path": str(path), "name": path.name, "is_card": not why,
                             "reason": why or None}
                            for path, why in cards.reader_volumes(unit_id)],
                "remembered": cards.remembered(unit_id) is not None}

    def maps_move(self, req: Request, a: Attached) -> dict:
        """As `maps move NAME --to internal|card` for each map named."""
        return self._each_map(req, a, "move")

    def maps_update(self, req: Request, a: Attached) -> dict:
        """As `maps update [--apply]`: every installed map with an update."""
        return self.run_command(req, [*self.command_line(a), "maps", "update",
                                      *_apply_option(req)])

    def apps_list(self, req: Request, a: Attached) -> dict:
        """As `apps [--builtin]`: what it prints, read from where it reads it (cli.read_apps),
        as data."""
        from openexpress import ciq, cli

        builtin = _flag(req.params, "builtin")
        _st, dev, _client, api, locale, state, account = cli.read_apps(
            self.args_for(a, "apps", apps_cmd=None, builtin=builtin))
        updates = {u.app_id: u for u in api.updates(dev, state.store_apps, locale)}

        def kind(app_type) -> dict:
            return {"kind": ciq.app_kind(app_type),
                    "kind_name": ciq.app_type_name(app_type) or str(app_type)}

        def update(app) -> dict | None:
            u = updates.get(app.app_id)
            return None if u is None else {"version": u.version_name, "size": u.size,
                                           "permissions_changed": u.permissions_changed}

        apps = [{"id": str(app.app_id), "name": app.name, **kind(app.app_type),
                 "version": app.version, "enabled": not app.disabled, "update": update(app),
                 "waiting": ("install" if app.pending else "removal" if app.pending_removal
                             else None),
                 "waiting_text": cli.app_waiting_text(app), "removable": app.removable}
                for app in state.store_apps]
        installed = {app.app_id for app in state.apps}
        result = {
            "summary": {"store_apps": len(state.store_apps), "free_slots": state.available_slots,
                        "free_bytes": state.available_space},
            "apps": apps,
            "account": [{"id": str(s.app_id), "name": s.name, **kind(s.app_type),
                         "version": s.version_name, "size": s.size, "developer": s.developer,
                         "permissions_changed": s.permissions_changed}
                        for s in account if s.app_id not in installed],
            "message": ciq.no_apps_text() if state.no_app_list else None,
            "warning": cli.apps_unreadable_text(state.unreadable) if state.unreadable else None,
            "note": cli.apps_store_note()}
        if builtin:
            result["builtin"] = [{"id": str(app.app_id), "name": app.name, **kind(app.app_type),
                                  "enabled": not app.disabled}
                                 for app in state.apps if app.preloaded]
        return result

    def _app_command(self, req: Request, a: Attached, command: str, flag: str) -> dict:
        """As `apps COMMAND NAME [--FLAG] [--apply]`, with the request's `flag` (a parameter
        named as the option, with _ for -)."""
        options = _apply_option(req)
        name = _text(req.params, "name")
        if _flag(req.params, flag):
            options.append("--" + flag.replace("_", "-"))
        return self.run_command(req, [*self.command_line(a), "apps", command, *options,
                                      "--", name])

    def apps_install(self, req: Request, a: Attached) -> dict:
        return self._app_command(req, a, "install", "allow_permissions")

    def apps_remove(self, req: Request, a: Attached) -> dict:
        return self._app_command(req, a, "remove", "forget")

    def activities_list(self, req: Request, a: Attached) -> dict:
        """As `activities`: the newest activities of the device and of its backups."""
        from openexpress import activities, cli, fit

        limit = _count(req.params, "limit", 20)
        st, dev = cli.open_device(self.args_for(a, "activities"))
        if not activities.keeps_activities(dev):
            return {"activities": [], "total": 0, "only_in_backups": 0}
        kept = self._activities.get(dev.unit_id)
        if kept is None:  # the first call for the device: what was kept on disk
            kept = self._activities[dev.unit_id] = activities.load_summaries(dev.unit_id)
        found = activities.DeviceActivities(dev, st, self._backups(), kept=kept)
        found.list_files()
        reading = self._reading_activities(req)
        try:
            read = found.read_next(min(limit, len(found.listing)), reading=reading)
        finally:  # also when it was cancelled: what it read is kept
            if found.newly_read:
                activities.save_summaries(dev.unit_id, found)
        reading.done()
        out = []
        for r in read:  # `start`: the file's own time where the file does not tell it
            act, summary = r.activity, r.summary
            entry = {"name": act.name, "source": act.source.value, "path": act.path,
                     "size": act.size,
                     "backup": act.backup.isoformat() if act.backup else None}
            if summary is None:
                entry.update(start=_iso(act.mtime), sport=None, sport_id=None, distance=None,
                             duration=None, ascent=None, average_heart_rate=None, calories=None,
                             state="unreadable")
            else:
                state = summary.state
                entry.update(
                    start=_iso(summary.start or summary.created or act.mtime),
                    sport=(activities.sport_name(summary.sport)
                           if summary.sport is not None else None),
                    sport_id=summary.sport, distance=summary.distance,
                    duration=summary.timer_time, ascent=summary.ascent,
                    average_heart_rate=summary.avg_heart_rate, calories=summary.calories,
                    state=("still_being_written" if state is fit.FileState.STILL_BEING_WRITTEN
                           else "damaged" if state is fit.FileState.DAMAGED else "complete"))
            out.append(entry)
        return {"activities": out, "total": len(found.listing),
                "only_in_backups": found.only_in_backups}

    def _reading_activities(self, req: Request):
        """The hook of DeviceActivities.read_next: a `progress` event per file read (phase
        "reading_activities", the file's name as the item), the first and the last at once
        and the others at most every READING_EVERY_S; a cancelled request stops before the
        next file, what was read kept for the next call."""
        engine = self

        class Reading:
            last, total = float("-inf"), 0

            def __call__(self, n: int, total: int, activity) -> None:
                engine.checkpoint(req)
                self.total = total
                now = time.monotonic()
                if n == 1 or now - self.last >= READING_EVERY_S:
                    self.last = now
                    engine.event("progress", {
                        "request": req.id, "phase": "reading_activities", "done": n - 1,
                        "total": total, "item": activity.name})

            def done(self) -> None:
                """The last event: every file read (none when none had to be)."""
                if self.total:
                    engine.event("progress", {"request": req.id, "phase": "reading_activities",
                                              "done": self.total, "total": self.total})

        return Reading()

    def activities_track(self, req: Request) -> dict:
        """The track of an activity (`fit`): a FIT file of this computer (a copy in a
        backup, an absolute path within the backup folder), else the path of the file on the
        device that activities.list gave (`device`, or the only one connected), read in its
        turn."""
        from openexpress import paths

        path = _text(req.params, "fit")
        local = Path(path).expanduser()
        if local.is_absolute():
            # Only a copy in a backup: a path that came from a device's listing (a name
            # beginning with "/") must not read any file of this computer.
            if not paths.is_inside(local, self._backups()):
                raise _invalid("fit")
            try:
                data = local.read_bytes()
            except OSError as e:
                raise EngineError("not_found", _("The file cannot be read: {error}").format(
                    error=e.strerror or type(e).__name__)) from e
            return track(data)

        def on_device(req: Request, a: Attached) -> dict:
            from openexpress import cli

            st, _dev = cli.open_device(self.args_for(a, "activities"))
            return track(st.read(path))

        return self._on_device(req, on_device)

    def backups_list(self, req: Request) -> dict:
        """The backups of a device (`device`: its unit ID, connected or not; else the only
        one connected) in the backup folder, the newest first."""
        from openexpress.backup import backup_sizes, backups_of

        wanted = req.params.get("device")
        if wanted is None:
            unit_id = self.resolve(req.params).unit_id
        elif isinstance(wanted, (str, int)) and str(wanted).isdigit():
            unit_id = int(wanted)
        else:
            raise _invalid("device")
        found = backups_of(self._backups(), unit_id)
        sizes = backup_sizes([folder for _when, folder in found])
        return {"backups": [{"path": str(folder), "date": _iso(when.timestamp()),
                             "files": files, "bytes": size}
                            for (when, folder), (files, size) in zip(found, sizes)]}

    # --- methods: the readings of the account ----------------------------------------

    def account_readings(self, req: Request) -> dict:
        """The readings of the Garmin Connect account beside a device (`device`: its unit ID,
        connected or not; else the only one connected), as the Linux window's device page has
        them (gui/account_readings.py, docs/design/odczyty-z-konta.md): the sensors on the
        account with their batteries, and the activity time the sun gained the device when it
        charges from the sun. Neither for a device off the signed-in account. Each answer is
        kept a while (answers.SENSORS, answers.SOLAR); `fresh` forgets them first (Refresh)."""
        from openexpress import answers, service
        from openexpress.settings import Settings

        unit_id, description = self._device_of(req.params)
        if _flag(req.params, "fresh"):
            answers.ANSWERS.forget((answers.SENSORS, answers.SOLAR))
        client = service.make_client(Settings.load())
        signed_in = client.auth is not None
        entry, listed = None, False
        if signed_in:
            try:
                entry = next((d for d in client.account_devices() if isinstance(d, dict)
                              and d.get("unitid") == unit_id), None)
                listed = True
            except Exception as e:  # noqa: BLE001 - not known: the readings are still asked
                log.info("engine: the account's devices not read (%s)", type(e).__name__)
        if listed and entry is None:
            return {"signed_in": True, "on_account": False, "sensors": None, "solar": None}
        return {"signed_in": signed_in, "on_account": True if entry is not None else None,
                "sensors": _sensors_reading(client) if signed_in else {"state": "signed_out"},
                "solar": _solar_reading(client, entry, description, signed_in)}

    def _device_of(self, params: dict) -> tuple[int, str]:
        """The unit ID `device` names (connected or remembered; else the only one connected)
        and the device's model as it describes itself ("" when not known)."""
        from openexpress import known_devices

        wanted = params.get("device")
        if wanted is None:
            a = self.resolve(params)
            return a.unit_id, a.description or ""
        if not (isinstance(wanted, (str, int)) and str(wanted).isdigit()):
            raise _invalid("device")
        unit_id = int(wanted)
        a = next((a for a in self.attached() if a.unit_id == unit_id), None)
        if a is not None and a.description:
            return unit_id, a.description
        entry = known_devices.load().get(unit_id)
        return unit_id, (entry.description if entry is not None else "") or ""


# The days of Solar Charging: today and the six before it (gui/account_readings.SOLAR_DAYS).
SOLAR_DAYS = 7


def _sensors_reading(client) -> dict | None:
    """Account Sensors: {"state": "read", "summary", "low", "sensors": [{"name", "detail",
    "low"}]}, low batteries first, or {"state": "failed", "message"}; None when the account
    has no sensor (the row is not shown)."""
    from openexpress import readings
    from openexpress.i18n import caption, npgettext

    try:
        sensors = readings.in_order(client.account_sensors())
    except Exception as e:  # noqa: BLE001 - said in the row, as in the window
        return {"state": "failed", "message": error_of(e).message}
    if not sensors:
        return None
    low = sum(1 for sensor in sensors if readings.is_low(sensor))
    # TRANSLATORS: subtitle of the row Account Sensors: how many sensors the account has
    parts = [npgettext("account sensors", "{count} sensor", "{count} sensors",
                       len(sensors)).format(count=len(sensors))]
    if low:
        # TRANSLATORS: subtitle of the row Account Sensors, after "3 sensors · "
        parts.append(npgettext("account sensors", "{count} with a low battery",
                               "{count} with a low battery", low).format(count=low))
    at = readings.now()
    return {"state": "read", "summary": caption(*parts), "low": low, "sensors": [
        {"name": readings.sensor_name(sensor),
         "detail": caption(readings.battery_text(sensor),
                           readings.last_connected_text(sensor, at)),
         "low": readings.is_low(sensor)} for sensor in sensors]}


def _solar_reading(client, entry: dict | None, description: str,
                   signed_in: bool) -> dict | None:
    """Solar Charging, for a device that charges from the sun (its entry on the account; the
    account's list not known, or not signed in, by its model's name): {"state": "read",
    "summary", "today_ms", "week_ms"}, {"state": "signed_out"} or {"state": "failed",
    "message"}; None for any other device."""
    from openexpress import readings

    if entry is not None:
        if not readings.charges_from_sun(entry):
            return None
    elif "solar" not in description.casefold():
        return None
    if not signed_in:
        return {"state": "signed_out"}
    device_id = entry.get("deviceid") if entry is not None else None
    if not isinstance(device_id, int) or isinstance(device_id, bool):
        return {"state": "failed", "message": None}
    start, end = readings.period(SOLAR_DAYS, readings.now().date())
    try:
        days = readings.solar_days(client.solar_gain(device_id, start, end), start, end)
    except Exception as e:  # noqa: BLE001 - said in the row, as in the window
        return {"state": "failed", "message": error_of(e).message}
    week = sum(ms for _day, ms in days)
    return {"state": "read", "summary": _solar_text(days), "today_ms": days[0][1] if days else 0,
            "week_ms": week}


def _solar_text(days: list) -> str:
    """The words of Solar Charging, as gui/account_readings.solar_text: today's activity time
    gained, then the week's; a week under a minute (a winter one) in a sentence."""
    from openexpress import readings
    from openexpress.i18n import caption

    week = sum(ms for _day, ms in days)
    if not readings.minutes(week):
        # TRANSLATORS: subtitle of the row Solar Charging when the sun gained the device less
        # than a minute of activity time in the last 7 days, today among them
        return _("Activity time gained: under a minute in the last 7\u00a0days")
    today = days[0][1] if days else 0
    return caption(
        # TRANSLATORS: subtitle of the row Solar Charging, before "last 7 days …"
        _("Activity time gained: today {today}").format(today=readings.gained_text(today)),
        # TRANSLATORS: subtitle of the row Solar Charging, after "Activity time gained: today …"
        _("last 7\u00a0days {week}").format(week=readings.gained_text(week)))


# The version at the end of a map's name in its file's header ("TopoActive Europe, West
# 2026.11"), written as Garmin's server writes a version.
_HEADER_VERSION = re.compile(r"\s(\d+\.\d+)$")


def map_storages(storage, device) -> list[dict]:
    """The storages maps may be on: the device's own (internal), then its memory card when it
    has one (openexpress.device.card), each with its name, free space and capacity."""
    from openexpress.device import card as cards

    def entry(kind: str, st) -> dict:
        return {"id": kind, "name": st.description or None, "free": st.free_space(),
                "capacity": st.capacity(), "removable": kind == cards.CARD}

    out = [entry(cards.INTERNAL, storage)]
    card = cards.card_of(storage, device)
    if card is not None:
        out.append(entry(cards.CARD, card))
    return out


def installed_map_version(storage, m, device=None) -> str | None:
    """The version of map `m` the device has, for a map with an update: Garmin's answer gives
    only the new one, so it is read from the name in the header of its files on the device
    (ownmaps.read_title; the first bytes of each .img file of it, read only), on its memory
    card when the map is there. None when no header ends in a version as the server writes one
    (CourseView has none; a base map writes "v9")."""
    from openexpress import ownmaps
    from openexpress.device import card as cards
    from openexpress.device.storage import DeviceNotResponding

    card = cards.card_of(storage, device) if m.location == cards.CARD else None
    for f in [f for f in m.files if f.device_path.lower().endswith(".img")][:3]:
        where, path = storage, f.device_path
        if card is not None:
            where, path = card, next(iter(cards.card_paths(f.device_path, device)),
                                     f.device_path)
        try:
            title = ownmaps.read_title(where.read_start(path, ownmaps.HEADER_SIZE))
        except DeviceNotResponding:
            raise
        except Exception as e:  # noqa: BLE001 - a version not known is no failed list
            log.info("engine: map header not read (%s)", type(e).__name__)
            continue
        found = _HEADER_VERSION.search(title or "")
        if found:
            return found.group(1)
    return None


# The most points a track gives: the app draws a line, not every second of it.
TRACK_POINTS = 2000


def track(data: bytes) -> dict:
    """The points of an activity's FIT file that have a position, in their order (time, and
    heart rate, altitude, speed where the record gives them), at most TRACK_POINTS: every n-th
    one then, the last one always kept."""
    from openexpress import fit

    try:
        samples = [s for s in fit.activity_data(data).samples
                   if s.latitude is not None and s.longitude is not None]
    except fit.FitError as e:
        raise EngineError("not_fit", str(e)) from e
    if len(samples) > TRACK_POINTS:
        step = -(-len(samples) // TRACK_POINTS)  # rounded up
        last = samples[-1]
        samples = samples[::step]
        if samples[-1] is not last:
            samples[-1] = last
    points = []
    for s in samples:
        point = {"lat": round(s.latitude, 7), "lon": round(s.longitude, 7), "time": _iso(s.time)}
        for key, value in (("hr", s.heart_rate), ("alt", s.altitude), ("speed", s.speed)):
            if value is not None:
                point[key] = value
        points.append(point)
    return {"points": points}


def main(args) -> int:
    """`openexpress engine`: the protocol on the process's own stdin and stdout. The file
    descriptor of stdout is kept for the protocol alone; whatever writes to descriptor 1
    afterwards (a program started without its output caught) writes to stderr."""
    from openexpress import applog

    applog.setup("engine", getattr(args, "verbose", False))
    sys.stdout.flush()
    protocol = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    os.dup2(2, 1)
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)  # noqa: SIM115
    log.info("engine: started (protocol %d)", PROTOCOL)
    try:
        return Engine(protocol).serve(stdin)
    finally:
        with contextlib.suppress(OSError):
            protocol.close()

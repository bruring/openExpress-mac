"""The readings of the Garmin Connect account beside a device: the batteries of the account's
sensors and the activity time the sun gained a device that charges from the sun
(docs/design/odczyty-z-konta.md). What the answers say, in words; the requests are the
client's (GarminClient.account_sensors, solar_gain), each answer kept for a while (answers).
Nothing here asks a server.

A sensor's last connection comes without a time zone and is in UTC (the account names its local
times so: "…Local").
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from openexpress.i18n import _, duration_text, relative_time

# The battery states of a sensor that ask for a new battery or a charge: first in the list.
LOW = frozenset({"LOW", "CRITICAL"})
# The days of the solar gain told by default, today among them: the window's row Solar
# Charging and `openexpress solar`.
SOLAR_DAYS = 7
# A sensor's last connection as the account writes it: 2026-10-21T09:41:07.0
_MOMENT = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?")


def now() -> datetime:
    """This computer's local time, naive: "today" and "2 days ago" are told by it."""
    return datetime.now()


# --- sensors ----------------------------------------------------------------------------------

def last_connected(sensor: dict) -> datetime | None:
    """When the account last heard of a sensor, in this computer's local time (naive); None
    when the answer says nothing it can read."""
    text = sensor.get("lastconnected")
    found = _MOMENT.fullmatch(text) if isinstance(text, str) else None
    if found is None:
        return None
    try:
        moment = datetime(*(int(part) for part in found.groups()), tzinfo=UTC)
    except ValueError:  # a month 13, a day 32
        return None
    return moment.astimezone().replace(tzinfo=None)


def is_low(sensor: dict) -> bool:
    """Whether the account says the sensor's battery is low or critical."""
    return sensor.get("batterystatus") in LOW


def in_order(sensors: list[dict]) -> list[dict]:
    """Low and critical batteries first; then by the last connection, the newest first, a
    sensor never heard of last."""
    def key(sensor: dict) -> tuple:
        moment = last_connected(sensor)
        return (not is_low(sensor), moment is None,
                -moment.timestamp() if moment is not None else 0.0)

    return sorted(sensors, key=key)


def sensor_name(sensor: dict) -> str:
    """A sensor as the account names it; its type when it has no name."""
    for field in ("devicename", "sensortype"):
        value = sensor.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "?"


def battery_text(sensor: dict) -> str:
    """A sensor's battery: in per cent when the account gives a number, else its state."""
    level = sensor.get("batterylevel")
    if isinstance(level, int) and not isinstance(level, bool) and 0 <= level <= 100:
        # TRANSLATORS: a sensor's battery, in per cent ("Battery 80%")
        return _("Battery {percent}%").format(percent=level)
    status = sensor.get("batterystatus")
    words = {
        # TRANSLATORS: a sensor's battery as the account tells it, without a percentage
        "GOOD": _("Battery good"),
        # TRANSLATORS: a sensor's battery as the account tells it, without a percentage
        "NEW": _("Battery new"),
        # TRANSLATORS: a sensor's battery as the account tells it, without a percentage
        "OK": _("Battery OK"),
        # TRANSLATORS: a sensor's battery as the account tells it, without a percentage
        "LOW": _("Battery low"),
        # TRANSLATORS: a sensor's battery as the account tells it, without a percentage
        "CRITICAL": _("Battery critical"),
        # TRANSLATORS: a sensor's battery being charged
        "CHARGING": _("Charging"),
    }
    if status in words:
        return words[status]
    if isinstance(status, str) and status.strip() and status.strip() != "UNKNOWN":
        # TRANSLATORS: a sensor's battery in a word of Garmin's that openExpress does not know,
        # shown as it comes ("Battery DEPLETED")
        return _("Battery {status}").format(status=status.strip())
    # TRANSLATORS: a sensor whose battery the account does not tell
    return _("Battery status unknown")


def last_connected_text(sensor: dict, at: datetime) -> str:
    """When a sensor was last connected, told against `at` (local); "" when not known."""
    moment = last_connected(sensor)
    if moment is None:
        return ""
    # TRANSLATORS: when a sensor was last connected, after its battery ("Battery 80% · last
    # connected 2 days ago"); {when}: "2 days ago", "yesterday, 23:41"…
    return _("last connected {when}").format(when=relative_time(moment, at))


# --- solar charging ---------------------------------------------------------------------------

def charges_from_sun(device: dict) -> bool:
    """Whether a device of the account charges from the sun: its entry says so
    (`solarpanelutilizationcapable`); an entry without that flag, by its model's name."""
    flag = device.get("solarpanelutilizationcapable")
    if isinstance(flag, bool):
        return flag
    name = device.get("productdisplayname")
    return isinstance(name, str) and "solar" in name.casefold()


def period(days: int, today: date) -> tuple[date, date]:
    """The first and the last day of the `days` days that end today."""
    return today - timedelta(days=days - 1), today


def solar_days(entries: list[dict], start: date, end: date) -> list[tuple[date, int]]:
    """Each day from `end` back to `start` with the activity time the sun gained it, in ms;
    a day the answer does not name, or names without a number, gained 0."""
    gained: dict[str, int] = {}
    for entry in entries:
        day, ms = entry.get("localconnectdate"), entry.get("totalactivitytimegainedms")
        if (isinstance(day, str) and isinstance(ms, (int, float)) and not isinstance(ms, bool)
                and ms > 0):
            gained[day] = gained.get(day, 0) + int(ms)
    out, day = [], end
    while day >= start:
        out.append((day, gained.get(day.isoformat(), 0)))
        day -= timedelta(days=1)
    return out


def minutes(ms: int) -> int:
    """Activity time gained in whole minutes, as it is shown."""
    return round(ms / 60000)


def gained_text(ms: int) -> str:
    """Activity time gained, to the minute: "0 min", "35 min", "4 h 10 min", with hard spaces
    only, so that a caption that wraps never splits it."""
    if not minutes(ms):
        # TRANSLATORS: a length of time in minutes
        return _("{minutes}\u00a0min").format(minutes=0)
    return duration_text(minutes(ms) * 60).replace(" ", "\u00a0")

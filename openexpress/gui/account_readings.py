"""The rows of the device page with the readings of the Garmin Connect account
(docs/design/odczyty-z-konta.md), in the group of the account under its rows, for they come from
the account: "Account Sensors", the sensors on the account and their batteries, low ones first;
under it "Solar Charging", the activity time the sun gained this device today and in the last 7
days. Each is asked off the window's thread when the device page is made (in_background); the
answers are kept a while (answers.SENSORS, answers.SOLAR), and Refresh, a sync and another
sign-in ask them anew.

Has: sensors_row (Account Sensors while there is no list to open), sensors_list (Account Sensors
with the sensors, to open) and solar_row, the rows of the device page shown
(MainWindow._init_state).
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib  # noqa: E402 - imports must follow gi.require_version

from openexpress import readings  # noqa: E402
from openexpress.gui.window_tasks import row_label  # noqa: E402
from openexpress.i18n import _, caption, npgettext  # noqa: E402

log = logging.getLogger(__name__)


def account_entry(s) -> dict | None:
    """The entry of the open device in the account's list the ownership check read; None when
    the list is not known or the device is not on it."""
    devices = getattr(s, "account_devices", None)
    if not isinstance(devices, list):
        return None
    return next((d for d in devices if isinstance(d, dict)
                 and d.get("unitid") == s.device.unit_id), None)


def add_rows(win, s, group) -> None:
    """The readings of the account, under the rows of the account in `group`: Account Sensors,
    then Solar Charging (in the same place when the account has no sensor). Neither on the page
    of a device off the account: they are the account's, of its devices."""
    win.sensors_row = win.sensors_list = win.solar_row = None  # those of the page before
    if off_the_account(s):
        return
    for row in (*sensors_rows(win, s), solar_row(win, s)):
        if row is not None:
            group.add(row)


def off_the_account(s) -> bool:
    """Signed in, the account's list known, and the open device not on it."""
    return (s.logged_in and isinstance(getattr(s, "account_devices", None), list)
            and account_entry(s) is None)


# --- Account Sensors --------------------------------------------------------------------------

def sensors_rows(win, s) -> tuple[Adw.ActionRow, Adw.ExpanderRow]:
    """The row Account Sensors, as two rows in one place, one of them shown: a plain row, with
    nothing to open, while not signed in ("Sign in to see"), while the account is asked
    ("Checking…") or when it could not be ("Could not check"); and, once the sensors come, a row
    that opens on them, closed (its subtitle tells of a low battery). No sensor: neither."""
    # TRANSLATORS: row in the group of the Garmin account on the device page, which opens on the
    # sensors on the account (a heart rate strap, a Varia, a power meter…) and their batteries
    title = _("Account Sensors")
    row, listed = Adw.ActionRow(title=title), Adw.ExpanderRow(title=title, visible=False)
    win.sensors_row, win.sensors_list = row, listed
    if not s.logged_in:
        row.set_subtitle(_("Sign in to see"))
        return row, listed
    row.set_subtitle(_("Checking…"))

    def read_sensors():
        return readings.in_order(s.client.account_sensors())

    def done(sensors):
        if win.sensors_list is listed:
            row.set_visible(False)
            show_sensors(listed, sensors)

    def failed(_e):
        if win.sensors_row is row:
            row.set_subtitle(_("Could not check"))

    win.in_background(read_sensors, done, failed)
    return row, listed


def show_sensors(row: Adw.ExpanderRow, sensors: list[dict]) -> None:
    """The sensors under the row, in the order given, and the row shown, closed; how many and
    how many with a low battery in its subtitle; a low or critical battery in the warning
    colour. No sensor: the row stays hidden."""
    if not sensors:
        return
    low = sum(1 for sensor in sensors if readings.is_low(sensor))
    # TRANSLATORS: subtitle of the row Account Sensors: how many sensors the account has
    parts = [npgettext("account sensors", "{count} sensor", "{count} sensors",
                       len(sensors)).format(count=len(sensors))]
    if low:
        # TRANSLATORS: subtitle of the row Account Sensors, after "3 sensors · "
        parts.append(npgettext("account sensors", "{count} with a low battery",
                               "{count} with a low battery", low).format(count=low))
    row.set_subtitle(GLib.markup_escape_text(caption(*parts)))
    at = readings.now()
    for sensor in sensors:
        # A sensor's name is the account's: text, not markup.
        child = Adw.ActionRow(
            title=GLib.markup_escape_text(readings.sensor_name(sensor)),
            subtitle=GLib.markup_escape_text(caption(readings.battery_text(sensor),
                                                     readings.last_connected_text(sensor, at))))
        if readings.is_low(sensor):
            label = row_label(child, "subtitle")
            if label is not None:
                label.add_css_class("warning")
        row.add_row(child)
    row.set_visible(True)


# --- Solar Charging ---------------------------------------------------------------------------

def solar_row(win, s) -> Adw.ActionRow | None:
    """The row Solar Charging, under Account Sensors, for a device that charges from the sun (its
    entry on the account, readings.charges_from_sun; not signed in, or the account's list not
    known, by its model's name); None for any other. "Sign in to see" while not signed in,
    "Checking…" while asked, then today's and the week's activity time gained, or "Could not
    check"."""
    entry = account_entry(s)
    if off_the_account(s):
        return None
    if s.logged_in and entry is not None:
        if not readings.charges_from_sun(entry):
            return None
    elif "solar" not in (s.device.description or "").casefold():
        return None
    # TRANSLATORS: row in the group of the Garmin account on the device page: the activity time
    # the sun gained a device that charges from the sun
    row = Adw.ActionRow(title=_("Solar Charging"))
    win.solar_row = row
    device_id = entry.get("deviceid") if entry is not None else None
    if not s.logged_in:
        row.set_subtitle(_("Sign in to see"))
        return row
    if not isinstance(device_id, int) or isinstance(device_id, bool):
        row.set_subtitle(_("Could not check"))
        return row
    row.set_subtitle(_("Checking…"))
    start, end = readings.period(readings.SOLAR_DAYS, readings.now().date())

    def read_solar():
        return readings.solar_days(s.client.solar_gain(device_id, start, end), start, end)

    def done(days):
        if win.solar_row is row:
            row.set_subtitle(GLib.markup_escape_text(solar_text(days)))

    def failed(_e):
        if win.solar_row is row:
            row.set_subtitle(_("Could not check"))

    win.in_background(read_solar, done, failed)
    return row


def solar_text(days: list) -> str:
    """The subtitle of Solar Charging: today's activity time gained, then the week's; a week
    under a minute (a winter one) in a sentence, not as two zeros."""
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

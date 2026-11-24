"""The readings of the Garmin Connect account: the sensors and their batteries, and the activity
time the sun gained a device that charges from the sun (docs/design/odczyty-z-konta.md, the
command line). One request each, never asked again after an error or HTTP 429, kept a while, in
memory and on disk only the fields read; the log names no sensor and no value, and not the
address of the solar request, which names the device. All answers, devices and sensors here are
made up."""

import logging
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import NOON
from fakes import RecordingSession, make_client, response

from openexpress import answers, cli, i18n, readings, service
from openexpress.answers import ANSWERS, Disk
from openexpress.cli import main
from openexpress.connect import SENSORS_PATH, ApiError

NB = "\u00a0"
DEVICE_ID = 91234567  # a made-up Garmin Connect ID of a device
OTHER_ID = 91234568
SOLAR_PATH = "device-service/devicemonitoring/solargain/"
DEVICES_PATH = "device-service/deviceregistration/devices"


def utc_text(local: datetime) -> str:
    """A moment of this computer's local time as the account writes a last connection: UTC,
    without a zone, tenths of a second."""
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0")


def sensors_answer():
    """The sensors of a made-up account, in the shape the account answers."""
    return [
        {"deviceName": "Made-up Strap", "sensorType": "HEART_RATE", "batteryLevel": 80,
         "batteryStatus": "OK", "lastConnected": utc_text(NOON - timedelta(hours=3)),
         "serialNumber": 123456789, "partNumber": "000-X0000-00", "softwareVersion": 1.5,
         "imageUrl": "https://example.invalid/strap.png", "rechargeableSensorCapable": False},
        {"deviceName": "Made-up Light", "sensorType": "BIKE_LIGHT_MAIN", "batteryLevel": None,
         "batteryStatus": "LOW", "lastConnected": utc_text(NOON - timedelta(days=2)),
         "serialNumber": 223456789, "lastLowBatteryNotification": "2026-10-19T08:00:00.123"},
        {"deviceName": "Made-up Speed", "sensorType": "BIKE_SPEED", "batteryLevel": None,
         "batteryStatus": "GOOD", "lastConnected": utc_text(NOON - timedelta(days=1, hours=1))},
    ]


def solar_answer(start: date, gained: dict[int, int]):
    """A made-up solar gain of seven days from `start`: {day number: ms}, the others 0."""
    return {"deviceId": DEVICE_ID, "userProfilePk": 1234567, "solarGainDailyDetailDTOList": [
        {"localConnectDate": (start + timedelta(days=n)).isoformat(),
         "totalActivityTimeGainedMs": gained.get(n, 0)} for n in range(7)]}


def account_devices():
    return [
        {"unitId": 3000000001, "deviceId": DEVICE_ID, "productDisplayName": "Made-up 8 Solar",
         "solarPanelUtilizationCapable": True, "solarChargeCapable": False},
        {"unitId": 3000000002, "deviceId": OTHER_ID, "productDisplayName": "Made-up Edge",
         "solarPanelUtilizationCapable": False, "solarChargeCapable": False},
    ]


# --- the requests ----------------------------------------------------------------------------

def test_the_sensors_are_asked_once_and_kept_with_only_the_fields_read(clock):
    client = make_client(**{SENSORS_PATH: response(body=sensors_answer())})
    first = client.account_sensors()
    assert client.account_sensors() == first
    assert client.http.paths() == [SENSORS_PATH]
    assert {k for s in first for k in s} <= answers.SENSOR_FIELDS
    assert first[0] == {"devicename": "Made-up Strap", "sensortype": "HEART_RATE",
                        "batterylevel": 80, "batterystatus": "OK",
                        "lastconnected": utc_text(NOON - timedelta(hours=3)),
                        "rechargeablesensorcapable": False}


def test_the_sensors_are_asked_again_after_half_an_hour(clock):
    client = make_client(**{SENSORS_PATH: response(body=sensors_answer())})
    client.account_sensors()
    clock.current += timedelta(minutes=29)
    client.account_sensors()
    clock.current += timedelta(minutes=2)
    client.account_sensors()
    assert client.http.paths() == [SENSORS_PATH, SENSORS_PATH]


@pytest.mark.parametrize("answer, sensors", [
    (response(204, b""), []), (response(200, b""), []), (response(body=[]), []),
    (response(body=[{"deviceName": "Made-up Strap"}, "text"]), [{"devicename": "Made-up Strap"}]),
], ids=["204", "empty", "none", "one and a stray"])
def test_no_sensors(answer, sensors, clock):
    assert make_client(**{SENSORS_PATH: answer}).account_sensors() == sensors


@pytest.mark.parametrize("status", [429, 500, 404])
def test_a_refusal_is_an_error_and_is_not_asked_again(status, clock):
    client = make_client(**{SENSORS_PATH: response(status, b"")})
    with pytest.raises(ApiError) as error:
        client.account_sensors()
    said = f"server error (HTTP {status})" if status >= 500 else f"HTTP {status}"
    assert str(error.value) == f"Account sensors: {said}"
    assert client.http.paths() == [SENSORS_PATH]


def test_an_answer_that_is_no_list_is_an_error(clock):
    client = make_client(**{SENSORS_PATH: response(body={"sensors": []})})
    with pytest.raises(ApiError) as error:
        client.account_sensors()
    assert str(error.value).startswith("Account sensors: Garmin's server gave an answer")


def test_the_solar_gain_of_a_device_and_its_period(clock, caplog):
    start, end = date(2026, 10, 15), date(2026, 10, 21)
    client = make_client(**{SOLAR_PATH: response(body=solar_answer(start, {6: 2_100_000}))})
    with caplog.at_level(logging.INFO, logger="openexpress"):
        days = client.solar_gain(DEVICE_ID, start, end)
        assert client.solar_gain(DEVICE_ID, start, end) == days
    assert client.http.paths() == [f"{SOLAR_PATH}{DEVICE_ID}/2026-10-15/2026-10-21"]
    assert days[6] == {"localconnectdate": "2026-10-21", "totalactivitytimegainedms": 2_100_000}
    assert {k for d in days for k in d} == answers.SOLAR_FIELDS
    # Its address names the device: the log has the host only.
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("HTTP GET")]
    assert len(lines) == 1 and str(DEVICE_ID) not in lines[0] and "solargain" not in lines[0]


@pytest.mark.parametrize("body", [[{"localConnectDate": "2026-10-21"}], {"other": []}, "text"],
                         ids=["list", "no days", "text"])
def test_a_solar_answer_without_its_days_is_an_error(body, clock):
    client = make_client(**{SOLAR_PATH: response(body=body)})
    with pytest.raises(ApiError) as error:
        client.solar_gain(DEVICE_ID, date(2026, 10, 15), date(2026, 10, 21))
    assert str(error.value).startswith("Solar charging: Garmin's server gave an answer")


def test_the_readings_are_forgotten_with_the_login_and_kept_on_disk_without_a_serial(tmp_path,
                                                                                     clock):
    ANSWERS.disk = Disk(tmp_path / "answers")
    try:
        client = make_client(**{SENSORS_PATH: response(body=sensors_answer())})
        client.account_sensors()
        text = " ".join(p.read_text() for p in (tmp_path / "answers").iterdir())
        for private in ("123456789", "000-X0000", "strap.png", "serialnumber",
                        "lastlowbatterynotification"):
            assert private not in text, private
        assert "Made-up Strap" in text
    finally:
        ANSWERS.disk = None
    assert answers.SENSORS in answers.ACCOUNT_KINDS and answers.SOLAR in answers.ACCOUNT_KINDS


# --- the words -------------------------------------------------------------------------------

def test_a_device_charges_from_the_sun_by_its_flag_else_by_its_name():
    assert readings.charges_from_sun({"solarpanelutilizationcapable": True})
    assert not readings.charges_from_sun({"solarpanelutilizationcapable": False,
                                          "productdisplayname": "Made-up Solar"})
    assert readings.charges_from_sun({"productdisplayname": "Made-up 2X Solar"})
    assert not readings.charges_from_sun({"productdisplayname": "Made-up Edge"})
    assert not readings.charges_from_sun({})


def test_every_day_of_the_period_newest_first_a_day_not_named_gained_nothing():
    start, end = readings.period(7, date(2026, 10, 21))
    assert (start, end) == (date(2026, 10, 15), date(2026, 10, 21))
    days = readings.solar_days([{"localconnectdate": "2026-10-21",
                                 "totalactivitytimegainedms": 2_100_000},
                                {"localconnectdate": "2026-10-19",
                                 "totalactivitytimegainedms": "x"},
                                {"localconnectdate": "2026-10-18",
                                 "totalactivitytimegainedms": 3_900_000}], start, end)
    assert days == [(date(2026, 10, 21), 2_100_000), (date(2026, 10, 20), 0),
                    (date(2026, 10, 19), 0), (date(2026, 10, 18), 3_900_000),
                    (date(2026, 10, 17), 0), (date(2026, 10, 16), 0), (date(2026, 10, 15), 0)]


@pytest.mark.parametrize("ms, text", [
    (0, f"0{NB}min"), (20_000, f"0{NB}min"), (2_100_000, f"35{NB}min"),
    (3_600_000, f"1{NB}h"), (15_000_000, f"4{NB}h{NB}10{NB}min")])
def test_the_time_gained_to_the_minute(ms, text):
    assert readings.gained_text(ms) == text


@pytest.mark.parametrize("seconds, text", [
    (45, f"45{NB}s"), (200, f"3{NB}min 20{NB}s"), (3599, f"59{NB}min 59{NB}s"),
    (3600, f"1{NB}h"), (3659, f"1{NB}h"), (15_000, f"4{NB}h 10{NB}min")])
def test_a_length_of_time_learns_hours(seconds, text):
    assert i18n.duration_text(seconds) == text


@pytest.mark.parametrize("sensor, text", [
    ({"batterylevel": 80, "batterystatus": "LOW"}, "Battery 80%"),
    ({"batterylevel": None, "batterystatus": "GOOD"}, "Battery good"),
    ({"batterystatus": "NEW"}, "Battery new"), ({"batterystatus": "OK"}, "Battery OK"),
    ({"batterystatus": "LOW"}, "Battery low"), ({"batterystatus": "CRITICAL"}, "Battery critical"),
    ({"batterystatus": "CHARGING"}, "Charging"), ({"batterystatus": "DEPLETED"}, "Battery DEPLETED"),
    ({"batterystatus": "UNKNOWN"}, "Battery status unknown"), ({}, "Battery status unknown"),
    ({"batterylevel": 140}, "Battery status unknown"), ({"batterylevel": True}, "Battery status unknown"),
])
def test_a_sensors_battery(sensor, text):
    assert readings.battery_text(sensor) == text


@pytest.mark.parametrize("text, local", [
    ("2026-10-21T10:00:00.0", datetime(2026, 10, 21, 10, 0, tzinfo=UTC)),
    ("2026-10-21T10:00:00", datetime(2026, 10, 21, 10, 0, tzinfo=UTC)),
    ("2026-10-21T10:00:00.123", datetime(2026, 10, 21, 10, 0, tzinfo=UTC)),
    ("2026-13-21T10:00:00.0", None), ("yesterday", None), (None, None), (5, None)])
def test_a_last_connection_is_read_as_utc(text, local):
    got = readings.last_connected({"lastconnected": text})
    assert got == (None if local is None else local.astimezone().replace(tzinfo=None))


def test_low_batteries_first_then_the_newest_connection(clock):
    sensors = readings.in_order([{"devicename": "a", "lastconnected": utc_text(NOON)},
                                 {"devicename": "b"},
                                 {"devicename": "c", "batterystatus": "CRITICAL"},
                                 {"devicename": "d",
                                  "lastconnected": utc_text(NOON - timedelta(days=3))},
                                 {"devicename": "e", "batterystatus": "LOW",
                                  "lastconnected": utc_text(NOON)}])
    assert [s["devicename"] for s in sensors] == ["e", "c", "a", "d", "b"]


def test_a_sensor_without_a_name_is_named_by_its_type():
    assert readings.sensor_name({"devicename": " ", "sensortype": "BIKE_POWER"}) == "BIKE_POWER"
    assert readings.sensor_name({}) == "?"


# --- the command line ------------------------------------------------------------------------

@pytest.fixture
def signed_in(monkeypatch, xdg):
    """`openexpress` with a made-up client of a made-up account (fakes.RecordingSession)."""
    session = RecordingSession(account_devices())
    client = make_client(account_devices())
    client.http = session

    def made(settings, need_auth):
        assert need_auth
        return client

    monkeypatch.setattr(cli, "make_client", made)
    return session


SENSORS_EN = [
    "3 sensors on the Garmin account · 1 with a low battery",
    "  Made-up Light: Battery low · last connected 2 days ago",
    "  Made-up Strap: Battery 80% · last connected 3 h ago",
    "  Made-up Speed: Battery good · last connected yesterday, 11:00",
]
SENSORS_PL = [
    "3 czujniki na koncie Garmin · 1 ze słabą baterią",
    "  Made-up Light: Bateria słaba · ostatnio połączony 2 dni temu",
    "  Made-up Strap: Bateria 80% · ostatnio połączony 3 godz. temu",
    "  Made-up Speed: Bateria dobra · ostatnio połączony wczoraj, 11:00",
]


@pytest.mark.parametrize("lang, lines", [("en", SENSORS_EN), ("pl", SENSORS_PL)])
def test_openexpress_sensors(lang, lines, signed_in, clock, capsys):
    signed_in.routes[SENSORS_PATH] = response(body=sensors_answer())
    try:
        assert main(["--lang", lang, "sensors"]) == 0
    finally:
        i18n.setup("en")
    assert capsys.readouterr().out.splitlines() == lines
    assert signed_in.paths() == [SENSORS_PATH]


def test_openexpress_sensors_without_any(signed_in, clock, capsys):
    signed_in.routes[SENSORS_PATH] = response(body=[])
    assert main(["sensors"]) == 0
    assert capsys.readouterr().out == "No sensors on this Garmin account.\n"


def test_openexpress_sensors_logs_one_line_and_no_sensor(signed_in, clock, caplog, capsys):
    signed_in.routes[SENSORS_PATH] = response(body=sensors_answer())
    with caplog.at_level(logging.DEBUG, logger="openexpress"):
        assert main(["sensors"]) == 0
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert len([r for r in caplog.records if r.getMessage().startswith("HTTP GET")]) == 1
    for private in ("Made-up", "LOW", "HEART_RATE"):
        assert private not in log, private
    # The battery level as a number of its own: never inside another (a process number).
    assert not re.search(r"(?<![\d.])80(?![\d.])", log), log


def test_openexpress_sensors_after_a_refusal_says_it_and_asks_no_more(signed_in, clock, capsys):
    signed_in.routes[SENSORS_PATH] = response(429, b"")
    assert main(["sensors"]) == 1
    assert capsys.readouterr().err.strip() == "Server error: Account sensors: HTTP 429"
    assert signed_in.paths() == [SENSORS_PATH]


SOLAR_EN = [
    "Made-up 8 Solar",
    "  Activity time gained: today 35 min · last 7 days 1 h 40 min",
    "  2026-10-21  35 min",
    "  2026-10-20  0 min",
    "  2026-10-19  1 h 5 min",
    "  2026-10-18  0 min",
    "  2026-10-17  0 min",
    "  2026-10-16  0 min",
    "  2026-10-15  0 min",
]
SOLAR_PL = [
    "Made-up 8 Solar",
    "  Zyskany czas aktywności: dziś 35 min · ostatnie 7 dni 1 h 40 min",
    "  21.10.2026  35 min",
    "  20.10.2026  0 min",
    "  19.10.2026  1 h 5 min",
    "  18.10.2026  0 min",
    "  17.10.2026  0 min",
    "  16.10.2026  0 min",
    "  15.10.2026  0 min",
]


@pytest.mark.parametrize("lang, lines", [("en", SOLAR_EN), ("pl", SOLAR_PL)])
def test_openexpress_solar(lang, lines, signed_in, clock, capsys):
    signed_in.routes[SOLAR_PATH] = response(body=solar_answer(date(2026, 10, 15),
                                                              {6: 2_100_000, 4: 3_900_000}))
    try:
        assert main(["--lang", lang, "solar"]) == 0
    finally:
        i18n.setup("en")
    assert capsys.readouterr().out.splitlines() == lines
    assert signed_in.paths() == [DEVICES_PATH, f"{SOLAR_PATH}{DEVICE_ID}/2026-10-15/2026-10-21"]


def test_openexpress_solar_asks_the_days_given(signed_in, clock, capsys):
    signed_in.routes[SOLAR_PATH] = response(body=solar_answer(date(2026, 10, 20), {1: 60_000}))
    assert main(["solar", "--days", "2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["Made-up 8 Solar", "  Activity time gained: today 1 min · last 2 days 1 min",
                   "  2026-10-21  1 min", "  2026-10-20  0 min"]
    assert signed_in.paths()[-1] == f"{SOLAR_PATH}{DEVICE_ID}/2026-10-20/2026-10-21"


@pytest.mark.parametrize("lang, days, gained, line", [
    ("en", "7", {}, "  Activity time gained: under a minute in the last 7 days"),
    ("en", "7", {2: 12, 6: 20_000}, "  Activity time gained: under a minute in the last 7 days"),
    ("en", "1", {}, "  Activity time gained: under a minute in the last 1 day"),
    ("pl", "7", {2: 12}, "  Zyskany czas aktywności: poniżej minuty w ostatnich 7 dniach"),
    ("pl", "1", {}, "  Zyskany czas aktywności: poniżej minuty w ostatnim 1 dniu"),
    ("pl", "2", {}, "  Zyskany czas aktywności: poniżej minuty w ostatnich 2 dniach")],
    ids=["nothing", "12 ms and 20 s", "one day", "pl 7", "pl 1", "pl 2"])
def test_openexpress_solar_a_period_under_a_minute_in_one_line(lang, days, gained, line,
                                                                signed_in, clock, capsys):
    first = date(2026, 10, 22) - timedelta(days=int(days))
    signed_in.routes[SOLAR_PATH] = response(body=solar_answer(first, gained))
    try:
        assert main(["--lang", lang, "solar", "--days", days]) == 0
    finally:
        i18n.setup("en")
    assert capsys.readouterr().out.splitlines() == ["Made-up 8 Solar", line]


@pytest.mark.parametrize("days", ["0", "32", "week"])
def test_openexpress_solar_days_out_of_range(days, signed_in, clock, capsys):
    with pytest.raises(SystemExit) as stop:
        main(["solar", "--days", days])
    assert stop.value.code == 2
    assert f"a whole number from 1 to 31, not {days}" in capsys.readouterr().err
    assert signed_in.sent == []


def test_openexpress_solar_without_a_device_of_the_sun(signed_in, clock, capsys):
    signed_in.routes[DEVICES_PATH] = response(body=account_devices()[1:])
    assert main(["solar"]) == 0
    assert capsys.readouterr().out == "No device on this Garmin account charges from the sun.\n"
    assert signed_in.paths() == [DEVICES_PATH]


def test_openexpress_sensors_and_solar_need_the_sign_in(xdg, capsys):
    from openexpress.settings import Consumer, save_consumer

    save_consumer(Consumer("made-up-key", "made-up-secret"))
    for command in ("sensors", "solar"):
        assert main([command]) == 1
        assert "Not signed in. Run: openexpress login" in capsys.readouterr().err


# --- a sync asks them anew -------------------------------------------------------------------

def test_a_sync_forgets_the_readings(device_root, xdg, clock):
    from fakes import SyncAccount

    from openexpress.device.storage import LocalStorage
    from openexpress.device.xmldevice import parse
    from openexpress.settings import Settings

    def session(root, account):
        st = LocalStorage(root)
        return service.DeviceSession(st, parse(st.read("GARMIN/GarminDevice.xml")), Settings(),
                                     account)

    ANSWERS.ask(answers.SENSORS, ("scope",), lambda: [{"devicename": "Made-up Strap"}])
    ANSWERS.ask(answers.SOLAR, ("scope", DEVICE_ID), list)
    ANSWERS.ask(answers.ACCOUNT_COUNTRY, ("scope",), lambda: "PL")
    account = SyncAccount(verified_unit_id=None)
    s = session(device_root, account)
    service.apply_sync(s, service.plan_sync(s, service.SyncOptions(first_run="baseline")))
    assert ANSWERS.kept(answers.SENSORS, ("scope",)) is None
    assert ANSWERS.kept(answers.SOLAR, ("scope", DEVICE_ID)) is None
    assert ANSWERS.kept(answers.ACCOUNT_COUNTRY, ("scope",)) == "PL"

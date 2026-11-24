"""Network errors say neither the address nor the device: a requests error names the whole
address, and the Connect IQ Store is asked with the unit ID and part number in the query, a
download with a signed address (garmindlm). Both go the way of the client: ApiError
(DownloadError), the address left out, filtered as the log. The user is told "no connection"
when no server answered, and the status of an answer with an error; the words of requests go
to the log only. All made up."""

import logging
import types
from urllib.parse import urlsplit

import pytest
import requests

from openexpress import applog, netstatus
from openexpress.connect import ApiError, GarminClient
from openexpress.settings import Settings

UNIT = 3000000101
SIGNATURE = "ab" * 16


def _refused_like_requests(url):
    """The error requests raises when nothing answers: its text names the path and query."""
    where = urlsplit(url)
    return requests.ConnectionError(
        f"HTTPSConnectionPool(host='{where.hostname}', port=443): Max retries exceeded with "
        f"url: {where.path}?{where.query} (Caused by NewConnectionError('<urllib3.connection."
        f"HTTPSConnection object at 0x7f0000000000>: Failed to establish a new connection: "
        f"[Errno 111] Connection refused'))")


def test_a_network_error_of_the_store_names_neither_the_unit_id_nor_the_address(
        monkeypatch, network_state, caplog):
    from openexpress import ciq

    client = GarminClient(Settings())
    client.require_verified = lambda unit_id: None
    client._sign = lambda method, url, *a, **kw: "OAuth x"

    def request(self, method, url, **kw):
        raise _refused_like_requests(url)

    monkeypatch.setattr(requests.Session, "request", request)
    device = types.SimpleNamespace(unit_id=UNIT, part_number="006-B4532-00")
    with caplog.at_level(logging.INFO, logger="openexpress"), \
            pytest.raises(ApiError) as said:
        ciq.StoreApi(client).account_apps(device, "en_US")
    assert str(said.value) == "Connect IQ apps: network error (no connection)"
    # The log file has the words of requests, without the address they name.
    (logged,) = [r.getMessage() for r in caplog.records if "HTTPSConnectionPool" in r.getMessage()]
    assert "Connection refused" in logged and "<address>" in logged
    assert str(UNIT) not in logged and "unitId" not in logged and "sku" not in logged
    assert network_state.counted()[0][netstatus.CONNECT].kind == netstatus.NETWORK


def test_a_failed_download_does_not_show_the_signed_address(tmp_path):
    from openexpress import download

    url = f"https://download.garmin.com/a.img?garmindlm={SIGNATURE}&id=1"

    class Broken:
        def get(self, url, **kw):
            raise _refused_like_requests(url)

    with pytest.raises(download.DownloadError) as said:
        download.fetch([url], tmp_path / "a", None, "0" * 32, session=Broken())
    text = str(said.value)
    assert "https://download.garmin.com/a.img: " in text  # which file, without its query
    assert SIGNATURE not in text and "garmindlm" not in text and "?" not in text


def test_a_failed_download_says_each_address_once(tmp_path):
    """Each address is asked twice, and a map has an address in reserve: the error names each
    address once."""
    from openexpress import download

    urls = ["https://download.garmin.com/a.img", "https://reserve.garmin.example/a.img"]

    class Broken:
        def get(self, url, **kw):
            raise _refused_like_requests(url)

    with pytest.raises(download.DownloadError) as said:
        download.fetch(urls, tmp_path / "a", None, "0" * 32, session=Broken())
    assert str(said.value).splitlines() == [
        "Could not download the file:",
        "  https://download.garmin.com/a.img: no connection",
        "  https://reserve.garmin.example/a.img: no connection"]


@pytest.mark.parametrize("error", [
    _refused_like_requests(f"https://connectapi.garmin.com/x?unitId={UNIT}"),
    requests.ConnectTimeout("HTTPSConnectionPool(host='connectapi.garmin.com', port=443): Max "
                            "retries exceeded with url: /x (Caused by ConnectTimeoutError(<urllib3"
                            ".connection.HTTPSConnection object at 0x7f0000000000>, 'Connection "
                            "to connectapi.garmin.com timed out. (connect timeout=30)'))"),
    requests.ReadTimeout("HTTPSConnectionPool(host='connectapi.garmin.com', port=443): Read "
                         "timed out. (read timeout=30)"),
], ids=["refused", "connect timeout", "read timeout"])
def test_no_answer_is_told_as_no_connection(error):
    """The words of the state of Garmin's servers: no class names, no object in memory."""
    assert applog.network_error_text(error) == "no connection"
    detail = applog.network_error_detail(error)  # what the log is told
    assert detail.startswith("HTTPSConnectionPool(host='connectapi.garmin.com'")
    assert str(UNIT) not in detail


def test_an_answer_with_an_error_is_told_by_its_status():
    answer = requests.Response()
    answer.status_code = 404
    error = requests.HTTPError("404 Client Error: Not Found for url: "
                               f"https://download.garmin.com/a.img?garmindlm={SIGNATURE}",
                               response=answer)
    assert applog.network_error_text(error) == "server error (HTTP 404)"


def test_a_refused_certificate_keeps_its_own_words():
    """A network that answers in Garmin's stead (the sign-in page of a hotel's network) is not
    "no connection": its words are said, without the address."""
    error = requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='connectapi.garmin.com', port=443): Max retries exceeded with "
        f"url: /x?unitId={UNIT} (Caused by SSLError(SSLCertVerificationError(1, '[SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed')))")
    text = applog.network_error_text(error)
    assert "CERTIFICATE_VERIFY_FAILED" in text and str(UNIT) not in text


def test_the_command_line_says_a_request_without_an_answer_is_a_network_error(monkeypatch,
                                                                              capsys):
    """Every request to Garmin goes through the client, which raises its own error: told as a
    network error, not as an answer of a server."""
    from openexpress import cli

    def request(self, method, url, **kw):
        raise _refused_like_requests(url)

    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(cli, "cmd_info", lambda args: GarminClient(Settings()).upload_settings(UNIT))
    assert cli.main(["info"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[-1] == "Network error: could not reach the Garmin server (no connection)"
    assert not any("HTTPSConnectionPool" in line for line in err)  # nor in a warning


def _a_failed_download_on_the_command_line(monkeypatch, tmp_path, *before):
    """`openexpress info`, whose command downloads from an address nothing answers at."""
    from openexpress import cli, download

    class Broken:
        def get(self, url, **kw):
            raise _refused_like_requests(url)

    def fetch(args):
        download.fetch([f"https://download.garmin.com/a.img?garmindlm={SIGNATURE}"],
                       tmp_path / "a", None, "0" * 32, session=Broken())

    monkeypatch.setattr(cli, "cmd_info", fetch)
    return cli.main([*before, "info"])


def test_the_command_line_says_a_failed_download_once(monkeypatch, tmp_path, capsys):
    """The error at the end names each address: no warning with the words of requests before
    it, once for each time the address was asked."""
    assert _a_failed_download_on_the_command_line(monkeypatch, tmp_path) == 1
    assert capsys.readouterr().err.splitlines() == [
        "Error: Could not download the file:",
        "  https://download.garmin.com/a.img: no connection"]


def test_a_failed_download_stays_in_the_log_file_and_under_verbose(monkeypatch, tmp_path, capsys,
                                                                  log_file):
    assert _a_failed_download_on_the_command_line(monkeypatch, tmp_path, "-v") == 1
    err = capsys.readouterr().err
    logged = log_file.read_text(encoding="utf-8")
    for said in (err, logged):
        assert said.count("Download failed (") == 2 and "<address>" in said
        assert SIGNATURE not in said


def test_the_command_line_says_a_network_error_without_its_address(monkeypatch, capsys):
    from openexpress import cli

    def broken(args):
        raise _refused_like_requests(
            f"https://apps.garmin.com/express/appstore/rest/apps/downloads?unitId={UNIT}")

    monkeypatch.setattr(cli, "cmd_info", broken)
    assert cli.main(["info"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Network error: could not reach the Garmin server (")
    assert str(UNIT) not in err and "unitId" not in err and "/express/" not in err

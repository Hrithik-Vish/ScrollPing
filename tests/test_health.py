"""The health endpoint: real HTTP requests against a real server on a free local port."""

import urllib.error
import urllib.request

import pytest

from src import health


@pytest.fixture
def served():
    status = health.BotStatus()
    server = health.start_health_server(status, 0, host="127.0.0.1")
    port = server.server_address[1]
    yield status, f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def request(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def test_reports_503_until_the_bot_is_running(served):
    status, base = served
    code, body, _ = request(base + "/health")
    assert code == 503 and b"not running" in body


def test_reports_200_while_the_bot_is_running(served):
    status, base = served
    status.set_running(True)
    code, body, headers = request(base + "/health")
    assert code == 200 and body == b"ok\n"
    assert headers["Cache-Control"] == "no-store"


def test_root_path_works_too(served):
    status, base = served
    status.set_running(True)
    assert request(base + "/")[0] == 200


def test_head_requests_work_and_have_no_body(served):
    status, base = served
    status.set_running(True)
    code, body, headers = request(base + "/health", method="HEAD")
    assert code == 200 and body == b""
    assert headers["Content-Length"] == "3"


def test_query_strings_are_ignored(served):
    status, base = served
    status.set_running(True)
    assert request(base + "/health?source=monitor")[0] == 200


def test_other_paths_are_404_and_reveal_nothing(served):
    status, base = served
    status.set_running(True)
    for path in ("/admin", "/users", "/.env", "/health/extra", "/metrics"):
        code, body, _ = request(base + path)
        assert code == 404 and body == b"not found\n"


def test_status_follows_the_bot_going_down_again(served):
    status, base = served
    status.set_running(True)
    assert request(base + "/health")[0] == 200
    status.set_running(False)
    assert request(base + "/health")[0] == 503


def test_post_requests_are_not_accepted(served):
    status, base = served
    status.set_running(True)
    code, _, _ = request(base + "/health", method="POST")
    assert code == 501


def test_responses_do_not_advertise_python_or_its_version(served):
    status, base = served
    _, _, headers = request(base + "/health")
    assert "Python" not in (headers["Server"] or "")


def test_requests_are_not_logged(served, capsys):
    status, base = served
    request(base + "/health")
    assert capsys.readouterr().err == ""
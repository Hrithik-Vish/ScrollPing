"""Tests for fetching.fetch_html and how main.retry() works with it.

requests.get is replaced by FakeGet, so nothing touches the network.
"""

import types

import pytest
import requests

import main
from src import fetching

URL = "https://example.org/"


def make_response(status=200, text="<html>ok</html>"):
    return types.SimpleNamespace(ok=status < 400, status_code=status, text=text)


class FakeGet:
    """Plays back outcomes in order (an Exception is raised, anything else is returned).
    The last outcome repeats forever, so ["always fails"] needs only one entry."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def fake_get(monkeypatch):
    def install(*outcomes):
        fake = FakeGet(outcomes)
        monkeypatch.setattr(fetching.requests, "get", fake)
        return fake

    return install


@pytest.fixture
def no_sleep(monkeypatch):
    """retry() sleeps 5s between attempts; skip that in tests."""
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)


# ---------------------------------------------------------------- fetch_html on its own

def test_fetch_html_returns_text_on_success(fake_get):
    fake_get(make_response(200, "<html>hello</html>"))
    assert fetching.fetch_html(URL) == "<html>hello</html>"


def test_fetch_html_sends_user_agent_and_timeouts(fake_get):
    fake = fake_get(make_response())
    fetching.fetch_html(URL)

    url, kwargs = fake.calls[0]
    assert url == URL
    assert kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert kwargs["timeout"] == (5, 30)


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectionError("down"),
        requests.exceptions.ConnectTimeout("slow connect"),
        requests.exceptions.ReadTimeout("slow read"),
    ],
    ids=["connection-error", "connect-timeout", "read-timeout"],
)
def test_fetch_html_lets_network_errors_propagate(fake_get, error):
    fake_get(error)
    with pytest.raises(requests.exceptions.RequestException):
        fetching.fetch_html(URL)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_fetch_html_raises_on_temporary_http_errors(fake_get, status):
    fake_get(make_response(status))
    with pytest.raises(requests.exceptions.HTTPError, match=str(status)):
        fetching.fetch_html(URL)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_fetch_html_returns_empty_string_on_permanent_http_errors(fake_get, status, capsys):
    fake_get(make_response(status))
    assert fetching.fetch_html(URL) == ""
    assert str(status) in capsys.readouterr().out


# ---------------------------------------------------------------- together with main.retry

def test_retry_recovers_after_two_network_failures(fake_get, no_sleep):
    fake = fake_get(
        requests.exceptions.ConnectionError("down"),
        requests.exceptions.ConnectionError("still down"),
        make_response(200, "<html>back</html>"),
    )
    assert main.retry(fetching.fetch_html, 3, URL) == "<html>back</html>"
    assert len(fake.calls) == 3


def test_retry_recovers_after_a_503(fake_get, no_sleep):
    fake = fake_get(make_response(503), make_response(200, "<html>back</html>"))
    assert main.retry(fetching.fetch_html, 3, URL) == "<html>back</html>"
    assert len(fake.calls) == 2


def test_retry_gives_up_after_max_attempts(fake_get, no_sleep):
    fake = fake_get(requests.exceptions.ConnectionError("down"))
    assert main.retry(fetching.fetch_html, 3, URL) == []
    assert len(fake.calls) == 3  # the original bug: this used to be 1


def test_retry_does_not_repeat_a_permanent_403(fake_get, no_sleep):
    fake = fake_get(make_response(403))
    assert main.retry(fetching.fetch_html, 3, URL) == ""
    assert len(fake.calls) == 1

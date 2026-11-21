"""Which failures count as transient (and so get a second attempt)."""

import requests

from core.retry import backoff_delay, is_transient
from tools.telescope import TelescopeError


def _http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


def test_link_and_network_errors_are_transient():
    assert is_transient(ConnectionResetError("reset"))
    assert is_transient(TimeoutError())
    assert is_transient(requests.ConnectionError())
    assert is_transient(requests.Timeout())
    assert is_transient(TelescopeError("dropped", transient=True))


def test_device_errors_and_bugs_are_not_transient():
    assert not is_transient(TelescopeError("fail to operate", code=207))
    assert not is_transient(ValueError("bug"))
    assert not is_transient(KeyError("x"))


def test_http_status_decides():
    assert is_transient(_http_error(503)) and is_transient(_http_error(429))
    assert not is_transient(_http_error(404)) and not is_transient(_http_error(400))


def test_the_cause_chain_is_followed():
    try:
        try:
            raise ConnectionError("down")
        except ConnectionError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as exc:
        assert is_transient(exc)


def test_backoff_doubles():
    assert [backoff_delay(i, 5.0) for i in (1, 2, 3)] == [5.0, 10.0, 20.0]

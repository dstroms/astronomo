"""One retry policy for transient failures, applied at node level (see ``core.graph._instrument``).

Only failures that a second attempt can plausibly cure are retried: a dropped telescope
connection, a timeout, a connection reset, a 5xx or 429 from a web service.  Anything else
(a bug, a bad parameter, a device that said "no") fails the mission on the first attempt.
"""

from __future__ import annotations

import socket

import requests

from tools.telescope import TelescopeError

_TRANSIENT_TYPES = (ConnectionError, TimeoutError, socket.timeout, requests.ConnectionError, requests.Timeout)


def is_transient(exc: BaseException) -> bool:
    """True if ``exc`` (or anything it was raised from) looks like a temporary network or link problem."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, TelescopeError):
            return exc.transient
        if isinstance(exc, requests.HTTPError):
            status = getattr(exc.response, "status_code", None)
            return status == 429 or (status is not None and status >= 500)
        if isinstance(exc, _TRANSIENT_TYPES):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def backoff_delay(attempt: int, base_s: float) -> float:
    """Delay before retry number ``attempt`` (1-based): ``base, 2*base, 4*base, ...``."""
    return base_s * 2 ** (attempt - 1)

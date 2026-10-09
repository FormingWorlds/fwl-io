"""Tell a transient HTTP failure from a permanent one, for reads: the fetch and the pin check.

A Dataverse write keeps the narrower retry set in :mod:`fwl_io.mirror`, since a write that
failed with a 500 or a timeout may have been processed.
"""

from __future__ import annotations

import ssl

import requests

# Transport failures worth a retry: a timeout, a refused, reset or dropped connection, a cut
# or corrupt body, and a non-JSON body where JSON was due (a Zenodo DOI read during an outage).
TRANSIENT_EXC = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
    requests.exceptions.JSONDecodeError,
    ConnectionError,
    TimeoutError,
)


def is_transient_status(status) -> bool:
    """Return whether an HTTP status is a transient fault: 408, 429, or a 5xx other than
    501 (not implemented) and 505 (HTTP version not supported)."""
    return isinstance(status, int) and (
        status in (408, 429) or (500 <= status < 600 and status not in (501, 505))
    )


def is_cert_failure(exc: BaseException) -> bool:
    """Return whether a certificate verification failure is anywhere in the exception chain."""
    todo, seen = [exc], set()
    while todo:
        e = todo.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            return True
        links = (e.__cause__, e.__context__, getattr(e, 'reason', None), *e.args)
        todo += [x for x in links if isinstance(x, BaseException)]
    return False


def is_transient(exc: BaseException) -> bool:
    """Return whether a failed request is worth a retry.

    An error that carries a response is decided by its status (:func:`is_transient_status`),
    any other by its type (``TRANSIENT_EXC``), which includes a certificate failure. A checksum
    mismatch (a plain ``ValueError``) and an ``HTTPError`` without a response are permanent.
    """
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(status, int):
        return is_transient_status(status)
    return isinstance(exc, TRANSIENT_EXC)

"""Zenodo DOI parsing.

A leaf module with no intra-package imports, so ``manifest``, ``sync``, and
``fetch`` can all derive a record id from a pinned version DOI without
importing one another. The record id keys both the versioned data directory
and the Zenodo API query.

The pattern ends in ``\\Z`` rather than ``$`` so that it matches the whole
string under ``match`` as well as ``fullmatch``: ``$`` also matches before a
terminal newline, which would let a DOI carrying a line break reach a
request URL.
"""

from __future__ import annotations

import re

ZENODO_DOI_PATTERN = re.compile(r'^(doi:)?10\.5281/zenodo\.(\d+)\Z')


def zenodo_record_id(doi: str) -> str:
    """Return the numeric record id of a Zenodo version DOI.

    Parameters
    ----------
    doi : str
        A Zenodo DOI of the form ``10.5281/zenodo.<record-id>`` (an optional
        ``doi:`` prefix is accepted).

    Returns
    -------
    str
        The trailing record-id digits.

    Raises
    ------
    ValueError
        When the string is not a Zenodo DOI.
    """
    match = ZENODO_DOI_PATTERN.fullmatch(doi.strip())
    if not match:
        raise ValueError(f'{doi!r} is not a Zenodo DOI of the form 10.5281/zenodo.<id>')
    return match.group(2)

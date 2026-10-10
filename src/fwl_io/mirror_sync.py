"""Create a draft mirror for a record that has none, and pin a published mirror
(``fwl-io mirror-sync``, ``fwl-io mirror-pin``).

Neither command publishes or deletes a dataset. ``mirror-sync`` creates at most one draft
per run, for a person to review and publish; ``mirror-pin`` edits a manifest file, for a
person to commit.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import requests

from fwl_io.doi import zenodo_record_id
from fwl_io.manifest import _discover_all, load_manifest, shared_manifest_path
from fwl_io.mirror import (
    DataverseClient,
    _listing,
    checksum_algorithm,
    descriptions,
    mirror_to_dataverse,
    names_source,
    source_record,
)
from fwl_io.mirror_status import mirror_status
from fwl_io.pins import dataverse_server, pin_problem
from fwl_io.sync import ZENODO_API, _extract_files, fetch_zenodo_record, select_files


def collection_mirrors(client: DataverseClient, collection: str) -> dict[str, list[tuple]]:
    """Return the datasets of a collection by the Zenodo record id their description names,
    each as ``(version state, persistent id)``; drafts are listed when the client holds a
    token. The search index can lag a dataset created seconds ago."""
    found: dict[str, list[tuple]] = {}
    start = 0

    def page(start: int) -> dict:
        params = {'q': '*', 'subtree': collection, 'type': 'dataset', 'per_page': 100}
        return client._retry(
            lambda: client._request('GET', '/api/search', params={**params, 'start': start}),
            f'listing of {collection}',
        )['data']

    while True:
        data = page(start)
        for item in data['items']:
            if recid := source_record(item.get('description') or ''):
                found.setdefault(recid, []).append((item.get('versionState'), item['global_id']))
        start += 100
        if start >= data['total_count']:
            return found


def verify_draft(
    client: DataverseClient,
    persistent_id: str,
    zenodo_doi: str,
    files: list[str] | None = None,
    api_base: str = ZENODO_API,
) -> list[str]:
    """Return why a draft does not hold the files of its Zenodo record; empty when it does.

    The draft must be a DRAFT with a license, name the record in its description, and hold
    exactly the record's files (those in ``files`` when given). Each file is downloaded
    from Dataverse and hashed: against the Zenodo checksum, and against the checksum
    Dataverse lists for it.
    """
    registry = _extract_files(fetch_zenodo_record(zenodo_doi, api_base=api_base))
    if files is not None:
        registry = select_files(registry, files, source=zenodo_doi)
    version = (
        client._retry(
            lambda: client._request(
                'GET',
                '/api/datasets/:persistentId/versions/:draft',
                params={'persistentId': persistent_id},
            ),
            f'draft {persistent_id}',
        ).get('data')
        or {}
    )
    problems = []
    if version.get('versionState') != 'DRAFT':
        problems.append(f'state is {version.get("versionState")!r}, not DRAFT')
    if not (version.get('license') or {}).get('name'):
        problems.append('no license')
    if not names_source(descriptions(version), f'10.5281/zenodo.{zenodo_record_id(zenodo_doi)}'):
        problems.append('the description does not name the Zenodo record')
    listed = _listing(client, persistent_id)
    problems += [f'{name} missing' for name in sorted(set(registry) - set(listed))]
    problems += [f'{name} is not in the record' for name in sorted(set(listed) - set(registry))]
    for name in sorted(set(registry) & set(listed)):
        entry = listed[name]
        algorithm, _, digest = registry[name].partition(':')
        theirs = checksum_algorithm(entry)
        hashes = {a: hashlib.new(a) for a in {algorithm, theirs} - {None}}
        with requests.get(
            f'{client.base_url}/api/access/datafile/{entry.get("id")}',
            headers=client._headers,
            stream=True,
            timeout=client.timeout,
        ) as response:
            if response.status_code != 200:
                problems.append(f'{name} cannot be downloaded (HTTP {response.status_code})')
                continue
            for chunk in response.iter_content(1 << 20):
                for running in hashes.values():
                    running.update(chunk)
        if hashes[algorithm].hexdigest() != digest.lower():
            problems.append(f'{name} differs from Zenodo ({algorithm})')
        listed_value = str((entry.get('checksum') or {}).get('value')).lower()
        if theirs is None or hashes[theirs].hexdigest() != listed_value:
            problems.append(f'{name} differs from the checksum Dataverse lists')
    return problems


def mirror_sync(
    collection: str,
    *,
    dataverse_url: str,
    token: str,
    contact_name: str,
    contact_email: str,
    dry_run: bool = False,
) -> tuple[list[str], int]:
    """Create a draft mirror for the first unpinned record that has no dataset yet.

    A record that a dataset of ``collection`` names, in any state, or that another
    installed dataset pins, gets no draft, only a line saying what it waits for (a publish,
    or ``fwl-io mirror-pin``); so does a dataset whose Zenodo record has a newer version
    or could not be read.
    One draft per run, never published; it holds the files every dataset of the record
    asks for, and is verified with :func:`verify_draft`.

    Returns
    -------
    tuple
        The report lines, and the exit code: 0 when there was nothing to create or the new
        draft is verified, 1 when a manifest was left out or the draft is not verified, 3
        when nothing failed but a Zenodo record could not be read.
    """
    discovery = _discover_all()
    if discovery.errors:
        return [f'{p}: manifest left out, {e.message}' for p, e in discovery.errors.items()], 1
    datasets = [ds for group in discovery.found.values() for ds in group]
    status = mirror_status(datasets)
    lines = [status.summary()]
    client = DataverseClient(dataverse_url, token)
    mirrors = collection_mirrors(client, collection)
    pinned = {zenodo_record_id(ds.zenodo): ds.dataverse for ds in datasets if ds.dataverse}
    todo: dict[str, list[str]] = {}
    for key, doi in sorted(status.unpinned.items()):
        recid = zenodo_record_id(doi)
        held = mirrors.get(recid, [])
        released = [pid.removeprefix('doi:') for state, pid in held if state == 'RELEASED']
        if key in status.stale:
            lines.append(f'SKIPPED {key}: pin the newest Zenodo version first')
        elif key in status.unreadable:
            lines.append(f'SKIPPED {key}: its Zenodo record could not be read')
        elif published := pinned.get(recid) or next(iter(released), None):
            lines.append(f'PIN MISSING {key}: run fwl-io mirror-pin doi:{published}')
        elif held:
            states = ', '.join(f'{pid} ({state})' for state, pid in held)
            lines.append(f'WAITING {key}: the collection holds {states}')
        else:
            todo.setdefault(recid, []).append(key)
    unread = 3 if status.unreadable else 0
    if not todo:
        return [*lines, 'no draft to create'], unread
    recid, keys = next(iter(todo.items()))
    doi = f'10.5281/zenodo.{recid}'
    wanted = [ds.files for ds in datasets if zenodo_record_id(ds.zenodo) == recid]
    files = None if None in wanted else sorted({name for names in wanted for name in names})
    lines.append(f'records without a mirror: {len(todo)}; this run takes Zenodo {recid}')
    if dry_run:
        return [*lines, f'WOULD CREATE a draft for {doi} ({", ".join(keys)})'], unread
    persistent_id = mirror_to_dataverse(
        doi,
        dataverse_url=dataverse_url,
        collection=collection,
        token=token,
        contact_name=contact_name,
        contact_email=contact_email,
        publish=False,
        files=files,
    )
    try:
        problems = verify_draft(client, persistent_id, doi, files)
    except Exception as exc:  # noqa: BLE001 -- the draft exists: its id must reach the report
        problems = [f'the check failed: {exc}']
    verdict = 'verified' if not problems else 'NOT verified: ' + '; '.join(problems)
    lines.append(f'CREATED draft {persistent_id} for {doi} ({", ".join(keys)}): {verdict}')
    return lines, 1 if problems else unread


def write_pin(manifest: Path, key: str, pin: str) -> None:
    """Set the ``dataverse`` pin of the ``[key]`` table of a manifest, after its ``zenodo``
    line, and check that the manifest then loads with that pin.

    Raises
    ------
    ValueError
        If the manifest has no ``[key]`` table with a ``zenodo`` line, or does not load
        with the pin afterwards; the file is left as it was.
    """
    before = manifest.read_text()
    lines = before.splitlines(keepends=True)
    try:
        start = lines.index(f'[{key}]\n')
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith('[')), len(lines)
        )
        body = [line for line in lines[start + 1 : end] if not line.startswith('dataverse')]
        at = next(i for i, line in enumerate(body) if line.startswith('zenodo')) + 1
    except (ValueError, StopIteration):
        raise ValueError(f'{manifest} has no [{key}] table with a zenodo line') from None
    body.insert(at, f'dataverse = "{pin}"\n')
    manifest.write_text(''.join(lines[: start + 1] + body + lines[end:]))
    try:
        written = {ds.key: ds.dataverse for ds in load_manifest(manifest)}
    except Exception:
        written = {}
    if written.get(key) != pin:
        manifest.write_text(before)
        raise ValueError(f'{manifest} does not load with the pin of {key}; left unchanged')


def mirror_pin(
    persistent_id: str,
    *,
    manifest: Path | None = None,
    client: DataverseClient | None = None,
) -> tuple[list[str], int]:
    """Pin a published mirror in every dataset of its Zenodo record.

    The dataset must be released and name its Zenodo record. Each installed dataset of
    that record is checked with :func:`fwl_io.pins.pin_problem`; one that the mirror
    serves gets the pin written into ``manifest`` (the shared manifest by default) when
    that file declares it, and otherwise a line with the pin to add in its own package.

    Returns
    -------
    tuple
        The report lines, and the exit code: 0 when every dataset of the record is served,
        1 otherwise.
    """
    pin = persistent_id.removeprefix('doi:')
    client = client or DataverseClient(dataverse_server(pin), '')
    body = client._retry(
        lambda: client._request(
            'GET', '/api/datasets/:persistentId', params={'persistentId': f'doi:{pin}'}
        ),
        f'state of doi:{pin}',
    )
    version = (body.get('data') or {}).get('latestVersion') or {}
    if version.get('versionState') != 'RELEASED':
        return [f'doi:{pin} is not published'], 1
    recid = source_record(descriptions(version))
    if recid is None:
        return [f'doi:{pin} does not name a Zenodo record as its source'], 1
    manifest = Path(manifest or shared_manifest_path())
    own = load_manifest(manifest)
    discovery = _discover_all()
    datasets = {ds.key: ds for group in [*discovery.found.values(), own] for ds in group}
    lines = [f'{p}: manifest left out, {e.message}' for p, e in discovery.errors.items()]
    code = 1 if lines else 0
    for ds in sorted(datasets.values(), key=lambda ds: ds.key):
        old = (ds.dataverse or '').removeprefix('doi:')
        if zenodo_record_id(ds.zenodo) != recid:
            continue
        if problem := pin_problem(replace(ds, dataverse=pin), client):
            lines.append(f'FAIL {ds.key}: {problem}')
            code = 1
        elif old == pin:
            lines.append(f'{ds.key}: already pinned')
        elif any(ds.key == mine.key for mine in own):
            write_pin(manifest, ds.key, pin)
            lines.append(f'PINNED {ds.key} in {manifest.name}' + (f' (was {old})' if old else ''))
        else:
            lines.append(f'{ds.key} is declared by another package; add there: dataverse = "{pin}"')
    if len(lines) == len(discovery.errors):
        return [*lines, f'no installed dataset pins Zenodo {recid}'], 1
    return lines, code

"""Command-line interface: ``fwl-io sync | list | fetch | check | relocate | mirror``.

Failures from the package's own error types exit with status 1 and a
one-line message on stderr instead of a traceback.
"""

from __future__ import annotations

import argparse
import os
import sys

from fwl_io import __version__


def _cmd_sync(args: argparse.Namespace) -> int:
    from fwl_io.sync import ZENODO_API, sync_manifest

    for registry in sync_manifest(args.manifest, api_base=args.api_base or ZENODO_API):
        print(f'wrote {registry}')
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from fwl_io.manifest import _discover

    found, errors = _discover()
    for provider, datasets in sorted(found.items()):
        print(f'[{provider}]')
        for ds in datasets:
            consumers = ', '.join(ds.required_by) or '-'
            registry_note = (
                '' if ds.registry_path and ds.registry_path.is_file() else '  [NO REGISTRY]'
            )
            print(f'  {ds.key:50s} required_by: {consumers}{registry_note}')
    for provider, message in sorted(errors.items()):
        print(f'[{provider}] FAILED TO LOAD: {message}', file=sys.stderr)
    return 1 if errors else 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    from fwl_io.manifest import fetch_for

    fetched = fetch_for(args.model, data_root=args.data_root)
    if not fetched:
        print(f'no datasets declare required_by = {args.model!r}', file=sys.stderr)
        return 1
    for key, paths in sorted(fetched.items()):
        print(f'{key}: {len(paths)} file(s)')
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from fwl_io.check import check_for

    report = check_for(args.model, data_root=args.data_root)
    if not (report.datasets or report.manifest_errors or report.dataset_errors):
        print(f'no datasets declare required_by = {args.model!r}', file=sys.stderr)
        return 1
    # The summary goes to stdout whatever the verdict: a caller running this to
    # find out what is wrong needs the detail, not just the exit code.
    print(report.summary())
    return 0 if report.ok else 1


def _cmd_relocate(args: argparse.Namespace) -> int:
    from fwl_io.relocate import relocate

    report = relocate(data_root=args.data_root, dry_run=args.dry_run)
    print(report.summary())
    # A tree that was already tidy is a success, so only a legacy tree that
    # could not be moved fails the command. Nothing was changed in that case,
    # which is what the exit code has to make actionable.
    return 1 if report.faults else 0


def _cmd_mirror(args: argparse.Namespace) -> int:
    from fwl_io.mirror import mirror_to_dataverse

    token = os.environ.get('DATAVERSE_TOKEN', '')
    if not token and not args.dry_run:
        print('fwl-io: set DATAVERSE_TOKEN to mirror (or pass --dry-run)', file=sys.stderr)
        return 1
    persistent_id = mirror_to_dataverse(
        args.zenodo_doi,
        dataverse_url=args.dataverse_url,
        collection=args.collection,
        token=token,
        contact_name=args.contact_name,
        contact_email=args.contact_email,
        subject=args.subject,
        publish=not args.no_publish,
        dry_run=args.dry_run,
    )
    if persistent_id is None:
        print(f'dry run complete for {args.zenodo_doi} (no Dataverse changes)')
    else:
        print(f'mirrored to {persistent_id}')
        print(f'add this to the manifest:  dataverse = "{persistent_id.removeprefix("doi:")}"')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fwl-io',
        description='Manifest-driven data management for the PROTEUS ecosystem.',
    )
    parser.add_argument('--version', action='version', version=f'fwl-io {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    p_sync = sub.add_parser('sync', help='regenerate committed registries from the Zenodo API')
    p_sync.add_argument('manifest', help='path to a manifest.toml')
    p_sync.add_argument('--api-base', default=None, help=argparse.SUPPRESS)
    p_sync.set_defaults(func=_cmd_sync)

    p_list = sub.add_parser('list', help='list datasets from all installed manifests')
    p_list.set_defaults(func=_cmd_list)

    p_fetch = sub.add_parser('fetch', help='fetch every dataset a model requires')
    p_fetch.add_argument('model', help='model name matched against required_by')
    p_fetch.add_argument('--data-root', default=None, help='override the FWL_DATA root')
    p_fetch.set_defaults(func=_cmd_fetch)

    p_check = sub.add_parser(
        'check',
        help='report whether a model has its data, without downloading (hashes what it can)',
    )
    p_check.add_argument('model', help='model name matched against required_by')
    p_check.add_argument('--data-root', default=None, help='override the FWL_DATA root')
    p_check.set_defaults(func=_cmd_check)

    p_relocate = sub.add_parser(
        'relocate', help='move data left by the previous layout into the current one'
    )
    p_relocate.add_argument('--data-root', default=None, help='override the FWL_DATA root')
    p_relocate.add_argument(
        '--dry-run', action='store_true', help='report what would move without moving it'
    )
    p_relocate.set_defaults(func=_cmd_relocate)

    p_mirror = sub.add_parser('mirror', help='mirror a Zenodo deposit to a Dataverse collection')
    p_mirror.add_argument('zenodo_doi', help='Zenodo version DOI to mirror')
    p_mirror.add_argument('--collection', required=True, help='target Dataverse collection alias')
    p_mirror.add_argument(
        '--dataverse-url', default='https://dataverse.nl', help='Dataverse base URL'
    )
    p_mirror.add_argument('--contact-name', default='PROTEUS Framework', help='dataset contact')
    p_mirror.add_argument(
        '--contact-email',
        default='',
        help='dataset contact email (required to create; only --dry-run is exempt)',
    )
    p_mirror.add_argument(
        '--subject',
        default='Astronomy and Astrophysics',
        help='Dataverse citation subject (the server rejects a value outside its vocabulary)',
    )
    p_mirror.add_argument('--no-publish', action='store_true', help='create a draft only')
    p_mirror.add_argument(
        '--dry-run', action='store_true', help='download and map metadata only; no Dataverse writes'
    )
    p_mirror.set_defaults(func=_cmd_mirror)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: message, not traceback
        print(f'fwl-io: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

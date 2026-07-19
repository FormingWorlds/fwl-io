"""Command-line interface: ``fwl-io sync | list | fetch``.

Failures from the package's own error types exit with status 1 and a
one-line message on stderr instead of a traceback.
"""

from __future__ import annotations

import argparse
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
            print(f'  {ds.key:50s} {ds.subdir:45s} required_by: {consumers}{registry_note}')
    for provider, message in sorted(errors.items()):
        print(f'[{provider}] FAILED TO LOAD: {message}', file=sys.stderr)
    return 1 if errors else 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    from fwl_io.manifest import fetch_for

    fetched = fetch_for(args.module, data_root=args.data_root)
    if not fetched:
        print(f'no datasets declare required_by = {args.module!r}', file=sys.stderr)
        return 1
    for key, paths in sorted(fetched.items()):
        print(f'{key}: {len(paths)} file(s)')
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
    p_fetch.add_argument('module', help='model name matched against required_by')
    p_fetch.add_argument('--data-root', default=None, help='override the FWL_DATA root')
    p_fetch.set_defaults(func=_cmd_fetch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: message, not traceback
        print(f'fwl-io: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

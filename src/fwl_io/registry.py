"""Reading and writing of committed file registries.

A registry is a plain-text file mapping file names to checksums, one entry
per line, in the same ``<name> <hash>`` format that pooch uses natively.
Hashes carry an algorithm prefix (for example ``md5:`` as provided by the
Zenodo API, or ``sha256:``); pooch verifies against whichever algorithm the
prefix names. Registries are committed to the repository of whichever
package owns the dataset and are regenerated with ``fwl-io sync`` rather
than edited by hand.
"""

from __future__ import annotations

from pathlib import Path


def load_registry(path: str | Path) -> dict[str, str]:
    """Load a registry file into a name-to-hash mapping.

    Lines that are empty or start with ``#`` are ignored. File names with
    spaces are not supported, matching the pooch registry format.
    """
    entries: dict[str, str] = {}
    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f'{path}:{lineno}: malformed registry line: {line!r}')
        name, digest = parts
        entries[name] = digest
    return entries


def write_registry(path: str | Path, entries: dict[str, str]) -> None:
    """Write a registry file with deterministic (sorted) entry order."""
    lines = [f'{name} {digest}' for name, digest in sorted(entries.items())]
    Path(path).write_text('\n'.join(lines) + '\n')

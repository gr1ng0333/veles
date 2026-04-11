from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .db import EXPECTED_TABLES, get_schema_version, initialize_database, list_user_tables
from .ingest import import_archive


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pdf-variant-bot', description='PDF assignment import scaffold')
    subparsers = parser.add_subparsers(dest='command', required=True)

    init_parser = subparsers.add_parser('init-db', help='Create or migrate the SQLite database scaffold')
    init_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')

    import_parser = subparsers.add_parser('import-archive', help='Register an archive and unpack it into ingest storage')
    import_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    import_parser.add_argument('archive_path', type=Path, help='Path to .zip/.tar archive with source PDFs')
    import_parser.add_argument('--slug', required=True, help='Stable slug for this source set (e.g. eg-variant-bank)')
    import_parser.add_argument('--title', default='', help='Optional human-readable title for the source set')
    import_parser.add_argument('--notes', default='', help='Optional notes stored with the source set')
    import_parser.add_argument('--storage-root', type=Path, default=None, help='Optional directory for copied archives and extracted files')
    return parser


def cmd_init_db(db_path: Path) -> int:
    conn = initialize_database(db_path)
    summary = {
        'db_path': str(db_path),
        'schema_version': get_schema_version(conn),
        'table_count': len(list_user_tables(conn)),
        'expected_tables': list(EXPECTED_TABLES),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    conn.close()
    return 0


def cmd_import_archive(
    db_path: Path,
    archive_path: Path,
    *,
    slug: str,
    title: str,
    notes: str,
    storage_root: Path | None,
) -> int:
    summary = import_archive(
        db_path,
        archive_path,
        set_slug=slug,
        title=title,
        notes=notes,
        storage_root=storage_root,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'init-db':
        return cmd_init_db(args.db_path)
    if args.command == 'import-archive':
        return cmd_import_archive(
            args.db_path,
            args.archive_path,
            slug=args.slug,
            title=args.title,
            notes=args.notes,
            storage_root=args.storage_root,
        )
    parser.error(f'Unknown command: {args.command}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())

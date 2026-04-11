from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .answers_parser import parse_answers_for_source
from .db import EXPECTED_TABLES, get_schema_version, initialize_database, list_user_tables
from .ingest import import_archive
from .reporting import inspect_database, inspect_set, list_import_issues, validate_set
from .tasks_parser import parse_tasks_for_source


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

    parse_tasks_parser = subparsers.add_parser('parse-tasks', help='Parse one registered tasks PDF into task blocks, tasks, and page assets')
    parse_tasks_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    parse_tasks_parser.add_argument('--source-file-id', required=True, type=int, help='source_files.id for a tasks_pdf record')
    parse_tasks_parser.add_argument('--storage-root', type=Path, default=None, help='Optional directory overriding storage_root from source_files metadata')

    parse_answers_parser = subparsers.add_parser('parse-answers', help='Parse one registered answers PDF and match answers to tasks by block/task number')
    parse_answers_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    parse_answers_parser.add_argument('--source-file-id', required=True, type=int, help='source_files.id for an answers_pdf record')
    parse_answers_parser.add_argument('--storage-root', type=Path, default=None, help='Optional directory overriding storage_root from source_files metadata')

    inspect_db_parser = subparsers.add_parser('inspect-db', help='Print database-level counts and a headline for each source set')
    inspect_db_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')

    inspect_set_parser = subparsers.add_parser('inspect-set', help='Print detailed counts for one source set and its files')
    inspect_set_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    inspect_set_parser.add_argument('--slug', required=True, help='source_sets.slug to inspect')

    list_issues_parser = subparsers.add_parser('list-issues', help='List recorded import issues for manual review')
    list_issues_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    list_issues_parser.add_argument('--slug', default=None, help='Optional source_sets.slug filter')
    list_issues_parser.add_argument('--severity', default=None, help='Optional severity filter (info/warning/error)')
    list_issues_parser.add_argument('--limit', type=int, default=50, help='Maximum number of issues to return')

    validate_set_parser = subparsers.add_parser('validate-set', help='Run lightweight quality checks for one imported source set')
    validate_set_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
    validate_set_parser.add_argument('--slug', required=True, help='source_sets.slug to validate')
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


def cmd_import_archive(db_path: Path, archive_path: Path, *, slug: str, title: str, notes: str, storage_root: Path | None) -> int:
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


def cmd_parse_tasks(db_path: Path, *, source_file_id: int, storage_root: Path | None) -> int:
    summary = parse_tasks_for_source(db_path, source_file_id=source_file_id, storage_root=storage_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_parse_answers(db_path: Path, *, source_file_id: int, storage_root: Path | None) -> int:
    summary = parse_answers_for_source(db_path, source_file_id=source_file_id, storage_root=storage_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_inspect_db(db_path: Path) -> int:
    summary = inspect_database(db_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_inspect_set(db_path: Path, *, slug: str) -> int:
    summary = inspect_set(db_path, set_slug=slug)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_list_issues(db_path: Path, *, slug: str | None, severity: str | None, limit: int) -> int:
    summary = list_import_issues(db_path, set_slug=slug, severity=severity, limit=limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_validate_set(db_path: Path, *, slug: str) -> int:
    summary = validate_set(db_path, set_slug=slug)
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
    if args.command == 'parse-tasks':
        return cmd_parse_tasks(args.db_path, source_file_id=args.source_file_id, storage_root=args.storage_root)
    if args.command == 'parse-answers':
        return cmd_parse_answers(args.db_path, source_file_id=args.source_file_id, storage_root=args.storage_root)
    if args.command == 'inspect-db':
        return cmd_inspect_db(args.db_path)
    if args.command == 'inspect-set':
        return cmd_inspect_set(args.db_path, slug=args.slug)
    if args.command == 'list-issues':
        return cmd_list_issues(args.db_path, slug=args.slug, severity=args.severity, limit=args.limit)
    if args.command == 'validate-set':
        return cmd_validate_set(args.db_path, slug=args.slug)
    parser.error(f'Unknown command: {args.command}')
    return 2


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())

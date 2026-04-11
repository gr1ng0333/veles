from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .db import EXPECTED_TABLES, get_schema_version, initialize_database, list_user_tables



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pdf-variant-bot', description='PDF assignment import scaffold')
    subparsers = parser.add_subparsers(dest='command', required=True)

    init_parser = subparsers.add_parser('init-db', help='Create or migrate the SQLite database scaffold')
    init_parser.add_argument('db_path', type=Path, help='Path to SQLite database file')
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



def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'init-db':
        return cmd_init_db(args.db_path)
    parser.error(f'Unknown command: {args.command}')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import get_schema_version, initialize_database


class ReportingError(RuntimeError):
    """Raised when inspection/validation cannot be completed."""


COUNT_QUERIES = {
    'source_sets': 'SELECT COUNT(*) FROM source_sets',
    'source_files': 'SELECT COUNT(*) FROM source_files',
    'task_blocks': 'SELECT COUNT(*) FROM task_blocks',
    'tasks': 'SELECT COUNT(*) FROM tasks',
    'task_assets': 'SELECT COUNT(*) FROM task_assets',
    'answers': 'SELECT COUNT(*) FROM answers',
    'import_issues': 'SELECT COUNT(*) FROM import_issues',
}


def inspect_database(db_path: Path) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT id, slug, title, source_kind, notes, created_at FROM source_sets ORDER BY id'
        ).fetchall()
        return {
            'db_path': str(Path(db_path)),
            'schema_version': get_schema_version(conn),
            'totals': {name: _scalar(conn, query) for name, query in COUNT_QUERIES.items()},
            'source_sets': [_build_set_headline(conn, row) for row in rows],
        }
    finally:
        conn.close()


def inspect_set(db_path: Path, *, set_slug: str) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        set_row = _get_set_row(conn, set_slug)
        file_rows = conn.execute(
            '''
            SELECT id, relative_path, file_kind, sha256, size_bytes, page_count, status, created_at
            FROM source_files
            WHERE set_id = ?
            ORDER BY id
            ''',
            (set_row['id'],),
        ).fetchall()
        block_counts = _count_map(
            conn,
            '''
            SELECT source_file_id, COUNT(*)
            FROM task_blocks
            WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
            GROUP BY source_file_id
            ''',
            (set_row['id'],),
        )
        task_counts = _count_map(
            conn,
            '''
            SELECT tb.source_file_id, COUNT(*)
            FROM tasks t
            JOIN task_blocks tb ON tb.id = t.block_id
            JOIN source_files sf ON sf.id = tb.source_file_id
            WHERE sf.set_id = ?
            GROUP BY tb.source_file_id
            ''',
            (set_row['id'],),
        )
        asset_counts = _count_map(
            conn,
            '''
            SELECT source_file_id, COUNT(*)
            FROM task_assets
            WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
            GROUP BY source_file_id
            ''',
            (set_row['id'],),
        )
        answer_counts = _count_map(
            conn,
            '''
            SELECT source_file_id, COUNT(*)
            FROM answers
            WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
            GROUP BY source_file_id
            ''',
            (set_row['id'],),
        )
        unmatched_answer_counts = _count_map(
            conn,
            '''
            SELECT source_file_id, COUNT(*)
            FROM answers
            WHERE task_id IS NULL
              AND source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
            GROUP BY source_file_id
            ''',
            (set_row['id'],),
        )
        issue_counts = _count_map(
            conn,
            '''
            SELECT source_file_id, COUNT(*)
            FROM import_issues
            WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
            GROUP BY source_file_id
            ''',
            (set_row['id'],),
        )
        files = []
        for row in file_rows:
            files.append(
                {
                    'id': row['id'],
                    'relative_path': row['relative_path'],
                    'file_kind': row['file_kind'],
                    'status': row['status'],
                    'sha256': row['sha256'],
                    'size_bytes': row['size_bytes'],
                    'page_count': row['page_count'],
                    'created_at': row['created_at'],
                    'task_blocks': block_counts.get(row['id'], 0),
                    'tasks': task_counts.get(row['id'], 0),
                    'task_assets': asset_counts.get(row['id'], 0),
                    'answers': answer_counts.get(row['id'], 0),
                    'unmatched_answers': unmatched_answer_counts.get(row['id'], 0),
                    'issues': issue_counts.get(row['id'], 0),
                }
            )

        counts = _build_set_counts(conn, set_row['id'])
        answers = _build_answer_summary(conn, set_row['id'])
        issues = _build_issue_summary(conn, set_row['id'])
        return {
            'db_path': str(Path(db_path)),
            'schema_version': get_schema_version(conn),
            'source_set': {
                'id': set_row['id'],
                'slug': set_row['slug'],
                'title': set_row['title'],
                'source_kind': set_row['source_kind'],
                'notes': set_row['notes'],
                'created_at': set_row['created_at'],
            },
            'counts': counts,
            'answers': answers,
            'issues': issues,
            'files': files,
        }
    finally:
        conn.close()


def list_import_issues(
    db_path: Path,
    *,
    set_slug: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        conditions: list[str] = []
        params: list[Any] = []
        if set_slug:
            conditions.append('ss.slug = ?')
            params.append(set_slug)
        if severity:
            conditions.append('ii.severity = ?')
            params.append(severity)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ''
        rows = conn.execute(
            f'''
            SELECT
                ii.id,
                ii.run_id,
                ii.source_file_id,
                ii.task_id,
                ii.severity,
                ii.issue_type,
                ii.message,
                ii.context_json,
                ii.created_at,
                sf.relative_path,
                sf.file_kind,
                ss.slug AS set_slug
            FROM import_issues ii
            LEFT JOIN source_files sf ON sf.id = ii.source_file_id
            LEFT JOIN source_sets ss ON ss.id = sf.set_id
            {where_sql}
            ORDER BY ii.id DESC
            LIMIT ?
            ''',
            (*params, int(limit)),
        ).fetchall()
        return {
            'db_path': str(Path(db_path)),
            'filters': {'set_slug': set_slug, 'severity': severity, 'limit': int(limit)},
            'issues': [
                {
                    'id': row['id'],
                    'run_id': row['run_id'],
                    'source_file_id': row['source_file_id'],
                    'task_id': row['task_id'],
                    'set_slug': row['set_slug'],
                    'relative_path': row['relative_path'],
                    'file_kind': row['file_kind'],
                    'severity': row['severity'],
                    'issue_type': row['issue_type'],
                    'message': row['message'],
                    'context': json.loads(row['context_json'] or '{}'),
                    'created_at': row['created_at'],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()


def validate_set(db_path: Path, *, set_slug: str) -> dict[str, Any]:
    summary = inspect_set(db_path, set_slug=set_slug)
    files = summary['files']
    counts = summary['counts']
    answers = summary['answers']
    issues = summary['issues']

    checks: list[dict[str, Any]] = []

    def add_check(name: str, status: str, details: dict[str, Any]) -> None:
        checks.append({'name': name, 'status': status, 'details': details})

    task_files = [item for item in files if item['file_kind'] == 'tasks_pdf']
    answer_files = [item for item in files if item['file_kind'] == 'answers_pdf']
    parsed_task_files = sum(1 for item in task_files if item['status'] == 'parsed_tasks')
    parsed_answer_files = sum(1 for item in answer_files if item['status'] == 'parsed_answers')

    if not task_files:
        add_check('tasks_pdf_registered', 'fail', {'expected_minimum': 1, 'actual': 0})
    else:
        add_check('tasks_pdf_registered', 'pass', {'count': len(task_files)})

    if task_files:
        task_parse_status = 'pass' if parsed_task_files == len(task_files) else 'warn'
        add_check(
            'tasks_pdf_parsed',
            task_parse_status,
            {'expected': len(task_files), 'parsed': parsed_task_files},
        )

    if not answer_files:
        add_check('answers_pdf_registered', 'warn', {'expected_minimum': 1, 'actual': 0})
    else:
        add_check('answers_pdf_registered', 'pass', {'count': len(answer_files)})
        answer_parse_status = 'pass' if parsed_answer_files == len(answer_files) else 'warn'
        add_check(
            'answers_pdf_parsed',
            answer_parse_status,
            {'expected': len(answer_files), 'parsed': parsed_answer_files},
        )

    add_check(
        'task_blocks_present',
        'pass' if counts['task_blocks'] > 0 else 'fail',
        {'count': counts['task_blocks']},
    )
    add_check(
        'tasks_present',
        'pass' if counts['tasks'] > 0 else 'fail',
        {'count': counts['tasks']},
    )
    add_check(
        'answers_unmatched',
        'pass' if answers['unmatched'] == 0 else 'warn',
        {'count': answers['unmatched']},
    )
    add_check(
        'import_issues_present',
        'pass' if issues['total'] == 0 else 'warn',
        {
            'count': issues['total'],
            'by_severity': issues['by_severity'],
            'by_type': issues['by_type'],
        },
    )

    return {
        'db_path': str(Path(db_path)),
        'set_slug': set_slug,
        'ok': not any(item['status'] == 'fail' for item in checks),
        'checks': checks,
        'counts': counts,
        'answers': answers,
        'issues': issues,
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = initialize_database(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _get_set_row(conn: sqlite3.Connection, set_slug: str) -> sqlite3.Row:
    row = conn.execute(
        'SELECT id, slug, title, source_kind, notes, created_at FROM source_sets WHERE slug = ?',
        (set_slug,),
    ).fetchone()
    if row is None:
        raise ReportingError(f'source set not found: {set_slug}')
    return row


def _scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(query, params).fetchone()[0])


def _count_map(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[int, int]:
    return {int(key): int(value) for key, value in conn.execute(query, params).fetchall()}


def _text_count_map(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> dict[str, int]:
    return {str(key): int(value) for key, value in conn.execute(query, params).fetchall()}


def _build_set_headline(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    counts = _build_set_counts(conn, row['id'])
    answers = _build_answer_summary(conn, row['id'])
    issues = _build_issue_summary(conn, row['id'])
    return {
        'id': row['id'],
        'slug': row['slug'],
        'title': row['title'],
        'source_kind': row['source_kind'],
        'created_at': row['created_at'],
        'counts': counts,
        'answers': answers,
        'issues': issues,
    }


def _build_set_counts(conn: sqlite3.Connection, set_id: int) -> dict[str, Any]:
    return {
        'source_files': _scalar(conn, 'SELECT COUNT(*) FROM source_files WHERE set_id = ?', (set_id,)),
        'file_kinds': _text_count_map(
            conn,
            'SELECT file_kind, COUNT(*) FROM source_files WHERE set_id = ? GROUP BY file_kind',
            (set_id,),
        ),
        'file_statuses': _text_count_map(
            conn,
            'SELECT status, COUNT(*) FROM source_files WHERE set_id = ? GROUP BY status',
            (set_id,),
        ),
        'task_blocks': _scalar(
            conn,
            '''
            SELECT COUNT(*)
            FROM task_blocks tb
            JOIN source_files sf ON sf.id = tb.source_file_id
            WHERE sf.set_id = ?
            ''',
            (set_id,),
        ),
        'tasks': _scalar(
            conn,
            '''
            SELECT COUNT(*)
            FROM tasks t
            JOIN task_blocks tb ON tb.id = t.block_id
            JOIN source_files sf ON sf.id = tb.source_file_id
            WHERE sf.set_id = ?
            ''',
            (set_id,),
        ),
        'task_assets': _scalar(
            conn,
            'SELECT COUNT(*) FROM task_assets WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)',
            (set_id,),
        ),
    }


def _build_answer_summary(conn: sqlite3.Connection, set_id: int) -> dict[str, int]:
    total = _scalar(
        conn,
        'SELECT COUNT(*) FROM answers WHERE source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)',
        (set_id,),
    )
    unmatched = _scalar(
        conn,
        '''
        SELECT COUNT(*)
        FROM answers
        WHERE task_id IS NULL
          AND source_file_id IN (SELECT id FROM source_files WHERE set_id = ?)
        ''',
        (set_id,),
    )
    return {
        'total': total,
        'matched': total - unmatched,
        'unmatched': unmatched,
    }


def _build_issue_summary(conn: sqlite3.Connection, set_id: int) -> dict[str, Any]:
    by_severity = _text_count_map(
        conn,
        '''
        SELECT ii.severity, COUNT(*)
        FROM import_issues ii
        JOIN source_files sf ON sf.id = ii.source_file_id
        WHERE sf.set_id = ?
        GROUP BY ii.severity
        ''',
        (set_id,),
    )
    by_type = _text_count_map(
        conn,
        '''
        SELECT ii.issue_type, COUNT(*)
        FROM import_issues ii
        JOIN source_files sf ON sf.id = ii.source_file_id
        WHERE sf.set_id = ?
        GROUP BY ii.issue_type
        ''',
        (set_id,),
    )
    return {
        'total': sum(by_type.values()),
        'by_severity': by_severity,
        'by_type': by_type,
    }

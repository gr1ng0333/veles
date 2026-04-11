from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .db import initialize_database

ANSWER_HINTS = (
    'answer',
    'answers',
    'solution',
    'solutions',
    'ответ',
    'ответы',
    'решение',
    'решения',
)

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.svg'}


class ArchiveImportError(RuntimeError):
    """Raised when archive import cannot be completed."""


def import_archive(
    db_path: Path,
    archive_path: Path,
    *,
    set_slug: str,
    title: str = '',
    notes: str = '',
    storage_root: Path | None = None,
) -> dict[str, Any]:
    db_path = Path(db_path)
    archive_path = Path(archive_path)
    if not archive_path.exists() or not archive_path.is_file():
        raise FileNotFoundError(f'Archive not found: {archive_path}')

    conn = initialize_database(db_path)
    storage_root = (storage_root or db_path.parent / 'pdf_variant_bot_storage').resolve()
    storage_root.mkdir(parents=True, exist_ok=True)

    archive_sha = sha256_file(archive_path)
    stamp = archive_sha[:12]
    archive_rel = Path('archives') / set_slug / stamp / archive_path.name
    stored_archive = storage_root / archive_rel
    extracted_rel = Path('extracted') / set_slug / stamp
    extracted_root = storage_root / extracted_rel

    set_id = None
    archive_file_id = None
    run_id = None

    try:
        with conn:
            set_id = ensure_source_set(conn, slug=set_slug, title=title, notes=notes)
            copy_once(archive_path, stored_archive)
            archive_file_id, archive_created = ensure_source_file(
                conn,
                set_id=set_id,
                relative_path=archive_rel.as_posix(),
                file_kind='archive',
                sha256=archive_sha,
                size_bytes=archive_path.stat().st_size,
                page_count=0,
                status='stored',
                metadata={
                    'original_name': archive_path.name,
                    'import_scope': 'archive',
                    'storage_root': str(storage_root),
                },
            )
            run_id = create_import_run(
                conn,
                {
                    'import_kind': 'archive_scan',
                    'source_path': str(archive_path),
                    'status': 'running',
                    'started_at': utc_now(),
                    'stats_json': json.dumps(
                        {
                            'set_id': set_id,
                            'source_file_id': archive_file_id,
                            'archive_file_id': archive_file_id,
                            'set_slug': set_slug,
                            'stored_archive': archive_rel.as_posix(),
                            'storage_root': str(storage_root),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    'notes': notes,
                },
            )

        extracted_now = unpack_once(stored_archive, extracted_root)
        summary = register_extracted_files(
            conn,
            set_id=set_id,
            run_id=run_id,
            archive_sha=archive_sha,
            storage_root=storage_root,
            extracted_root=extracted_root,
        )
        summary['registered_files']['new'] += 1 if archive_created else 0
        summary['registered_files']['existing'] += 0 if archive_created else 1
        summary['file_kinds']['archive'] = 1

        with conn:
            finalize_import_run(
                conn,
                run_id,
                status='completed',
                payload={
                    'finished_at': utc_now(),
                    'stats_json': json.dumps(
                        {
                            **summary,
                            'set_slug': set_slug,
                            'archive_sha256': archive_sha,
                            'stored_archive': archive_rel.as_posix(),
                            'extracted_root': extracted_rel.as_posix(),
                            'extracted_now': extracted_now,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
    except Exception as exc:
        with conn:
            if run_id is not None:
                finalize_import_run(
                    conn,
                    run_id,
                    status='failed',
                    payload={
                        'finished_at': utc_now(),
                        'stats_json': json.dumps(
                            {
                                'set_slug': set_slug,
                                'archive_path': str(archive_path),
                                'error': str(exc),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                )
            if run_id is not None:
                create_import_issue(
                    conn,
                    {
                        'run_id': run_id,
                        'source_file_id': archive_file_id,
                        'severity': 'error',
                        'issue_type': 'archive-import-failed',
                        'message': str(exc),
                        'context_json': json.dumps(
                            {'archive_path': str(archive_path)},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                )
        conn.close()
        if isinstance(exc, shutil.ReadError):
            raise ArchiveImportError(f'Unsupported archive format: {archive_path}') from exc
        raise

    result = {
        'db_path': str(db_path),
        'set_slug': set_slug,
        'set_id': set_id,
        'run_id': run_id,
        'archive_sha256': archive_sha,
        'storage_root': str(storage_root),
        'stored_archive': archive_rel.as_posix(),
        'extracted_root': extracted_rel.as_posix(),
        'extracted_now': extracted_now,
        **summary,
    }
    conn.close()
    return result


def register_extracted_files(
    conn: sqlite3.Connection,
    *,
    set_id: int,
    run_id: int | None,
    archive_sha: str,
    storage_root: Path,
    extracted_root: Path,
) -> dict[str, Any]:
    file_kinds: dict[str, int] = {}
    new_count = 0
    existing_count = 0
    seen_files = 0

    if not extracted_root.exists():
        raise ArchiveImportError(f'Extracted directory is missing: {extracted_root}')

    with conn:
        for path in sorted(extracted_root.rglob('*')):
            if not path.is_file():
                continue
            seen_files += 1
            sha = sha256_file(path)
            kind = classify_file_kind(path)
            rel = path.resolve().relative_to(storage_root).as_posix()
            file_id, created = ensure_source_file(
                conn,
                set_id=set_id,
                relative_path=rel,
                file_kind=kind,
                sha256=sha,
                size_bytes=path.stat().st_size,
                page_count=0,
                status='registered',
                metadata={
                    'archive_sha256': archive_sha,
                    'import_scope': 'extracted',
                    'run_id': run_id,
                },
            )
            if created:
                new_count += 1
            else:
                existing_count += 1
            file_kinds[kind] = file_kinds.get(kind, 0) + 1
            if run_id is not None:
                attach_file_to_run(conn, run_id, file_id)

    return {
        'registered_files': {
            'new': new_count,
            'existing': existing_count,
            'total_seen': seen_files + 1,
        },
        'file_kinds': file_kinds,
    }


def ensure_source_set(conn: sqlite3.Connection, *, slug: str, title: str, notes: str) -> int:
    row = conn.execute('SELECT id, title, notes FROM source_sets WHERE slug = ?', (slug,)).fetchone()
    if row:
        updates: list[str] = []
        params: list[Any] = []
        if title and not row['title']:
            updates.append('title = ?')
            params.append(title)
        if notes and not row['notes']:
            updates.append('notes = ?')
            params.append(notes)
        if updates:
            params.append(row['id'])
            conn.execute(f"UPDATE source_sets SET {', '.join(updates)} WHERE id = ?", params)
        return int(row['id'])

    cursor = conn.execute(
        'INSERT INTO source_sets (slug, title, source_kind, notes) VALUES (?, ?, ?, ?)',
        (slug, title, 'archive', notes),
    )
    return int(cursor.lastrowid)


def ensure_source_file(
    conn: sqlite3.Connection,
    *,
    set_id: int,
    relative_path: str,
    file_kind: str,
    sha256: str,
    size_bytes: int,
    page_count: int,
    status: str,
    metadata: dict[str, Any],
) -> tuple[int, bool]:
    row = conn.execute(
        'SELECT id FROM source_files WHERE relative_path = ? AND sha256 = ?',
        (relative_path, sha256),
    ).fetchone()
    if row:
        return int(row['id']), False

    cursor = conn.execute(
        '''
        INSERT INTO source_files (
            set_id,
            relative_path,
            file_kind,
            sha256,
            size_bytes,
            page_count,
            status,
            metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            set_id,
            relative_path,
            file_kind,
            sha256,
            size_bytes,
            page_count,
            status,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ),
    )
    return int(cursor.lastrowid), True


def copy_once(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    shutil.copy2(src, dst)


def unpack_once(archive_path: Path, extracted_root: Path) -> bool:
    if extracted_root.exists() and any(extracted_root.iterdir()):
        return False
    extracted_root.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(str(archive_path), str(extracted_root))
    return True


def classify_file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix == '.pdf':
        if any(hint in name for hint in ANSWER_HINTS):
            return 'answers_pdf'
        return 'tasks_pdf'
    if suffix in IMAGE_SUFFIXES:
        return 'image'
    return 'other'


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def create_import_run(conn: sqlite3.Connection, payload: dict[str, Any]) -> int | None:
    return insert_dynamic(conn, 'import_runs', payload)


def finalize_import_run(conn: sqlite3.Connection, run_id: int | None, *, status: str, payload: dict[str, Any]) -> None:
    if run_id is None:
        return
    update_payload = dict(payload)
    update_payload['status'] = status
    update_dynamic(conn, 'import_runs', update_payload, 'id = ?', (run_id,))


def attach_file_to_run(conn: sqlite3.Connection, run_id: int, source_file_id: int) -> None:
    columns = table_columns(conn, 'source_files')
    if 'last_seen_run_id' not in columns:
        return
    conn.execute('UPDATE source_files SET last_seen_run_id = ? WHERE id = ?', (run_id, source_file_id))


def create_import_issue(conn: sqlite3.Connection, payload: dict[str, Any]) -> int | None:
    return insert_dynamic(conn, 'import_issues', payload)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f'PRAGMA table_info({table})').fetchall()
    return {str(row['name']) for row in rows}


def insert_dynamic(conn: sqlite3.Connection, table: str, payload: dict[str, Any]) -> int | None:
    available = table_columns(conn, table)
    filtered = {key: value for key, value in payload.items() if key in available}
    if not filtered:
        return None
    columns = ', '.join(filtered.keys())
    placeholders = ', '.join('?' for _ in filtered)
    cursor = conn.execute(
        f'INSERT INTO {table} ({columns}) VALUES ({placeholders})',
        tuple(filtered.values()),
    )
    return int(cursor.lastrowid)


def update_dynamic(
    conn: sqlite3.Connection,
    table: str,
    payload: dict[str, Any],
    where_clause: str,
    where_params: tuple[Any, ...],
) -> None:
    available = table_columns(conn, table)
    filtered = {key: value for key, value in payload.items() if key in available}
    if not filtered:
        return
    assignments = ', '.join(f'{key} = ?' for key in filtered)
    params = tuple(filtered.values()) + where_params
    conn.execute(f'UPDATE {table} SET {assignments} WHERE {where_clause}', params)

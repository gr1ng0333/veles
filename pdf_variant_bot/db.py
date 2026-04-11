from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

DEFAULT_SCHEMA_VERSION = 1

EXPECTED_TABLES = (
    'schema_meta',
    'source_sets',
    'source_files',
    'task_blocks',
    'tasks',
    'task_assets',
    'answers',
    'import_runs',
    'import_issues',
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_sets (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL CHECK(source_kind IN ('archive', 'manual', 'single_pdf')),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY,
    set_id INTEGER REFERENCES source_sets(id) ON DELETE SET NULL,
    relative_path TEXT NOT NULL,
    file_kind TEXT NOT NULL CHECK(file_kind IN ('tasks_pdf', 'answers_pdf', 'archive', 'image', 'other')),
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(relative_path, sha256)
);

CREATE TABLE IF NOT EXISTS task_blocks (
    id INTEGER PRIMARY KEY,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    block_code TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    page_start INTEGER,
    page_end INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_file_id, block_code, ordinal)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    block_id INTEGER NOT NULL REFERENCES task_blocks(id) ON DELETE CASCADE,
    task_number TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    prompt_text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    fingerprint TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(block_id, task_number, ordinal)
);

CREATE TABLE IF NOT EXISTS task_assets (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    page_number INTEGER,
    asset_index INTEGER NOT NULL DEFAULT 0,
    asset_kind TEXT NOT NULL CHECK(asset_kind IN ('image', 'diagram', 'table', 'unknown')),
    relative_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    width INTEGER,
    height INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(task_id, relative_path)
);

CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    block_code TEXT NOT NULL DEFAULT '',
    task_number TEXT NOT NULL DEFAULT '',
    answer_text TEXT NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    confidence REAL NOT NULL DEFAULT 0.0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    import_kind TEXT NOT NULL CHECK(import_kind IN ('archive_scan', 'tasks_parse', 'answers_parse')),
    source_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'partial')),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS import_issues (
    id INTEGER PRIMARY KEY,
    run_id INTEGER REFERENCES import_runs(id) ON DELETE SET NULL,
    source_file_id INTEGER REFERENCES source_files(id) ON DELETE SET NULL,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error')),
    issue_type TEXT NOT NULL,
    message TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_source_files_set_id ON source_files(set_id);
CREATE INDEX IF NOT EXISTS idx_task_blocks_source_file_id ON task_blocks(source_file_id);
CREATE INDEX IF NOT EXISTS idx_tasks_block_id ON tasks(block_id);
CREATE INDEX IF NOT EXISTS idx_task_assets_task_id ON task_assets(task_id);
CREATE INDEX IF NOT EXISTS idx_answers_task_id ON answers(task_id);
CREATE INDEX IF NOT EXISTS idx_answers_source_file_id ON answers(source_file_id);
CREATE INDEX IF NOT EXISTS idx_import_issues_run_id ON import_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_import_issues_source_file_id ON import_issues(source_file_id);
"""


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    return conn



def initialize_database(db_path: str | Path) -> sqlite3.Connection:
    conn = connect_db(db_path)
    with conn:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            """
            INSERT INTO schema_meta(key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (str(DEFAULT_SCHEMA_VERSION),),
        )
    return conn



def list_user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]



def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        raise RuntimeError('schema_version is missing from schema_meta')
    return int(row[0])



def ensure_expected_tables(conn: sqlite3.Connection) -> Iterable[str]:
    existing = set(list_user_tables(conn))
    missing = [name for name in EXPECTED_TABLES if name not in existing]
    if missing:
        raise RuntimeError(f'Missing expected tables: {missing}')
    return EXPECTED_TABLES

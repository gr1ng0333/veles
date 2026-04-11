"""Scaffold for PDF assignment import and future variant generation."""

from .db import (
    DEFAULT_SCHEMA_VERSION,
    EXPECTED_TABLES,
    connect_db,
    get_schema_version,
    initialize_database,
    list_user_tables,
)
from .ingest import ArchiveImportError, import_archive
from .tasks_parser import TaskParseError, parse_tasks_for_source, segment_task_blocks

__all__ = [
    'DEFAULT_SCHEMA_VERSION',
    'EXPECTED_TABLES',
    'connect_db',
    'get_schema_version',
    'initialize_database',
    'list_user_tables',
    'ArchiveImportError',
    'import_archive',
    'TaskParseError',
    'parse_tasks_for_source',
    'segment_task_blocks',
]

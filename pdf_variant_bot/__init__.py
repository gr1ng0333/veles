"""Scaffold for PDF assignment import and future variant generation."""

from .answers_parser import AnswerParseError, parse_answers_for_source, segment_answer_entries
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
    'AnswerParseError',
    'ArchiveImportError',
    'DEFAULT_SCHEMA_VERSION',
    'EXPECTED_TABLES',
    'TaskParseError',
    'connect_db',
    'get_schema_version',
    'import_archive',
    'initialize_database',
    'list_user_tables',
    'parse_answers_for_source',
    'parse_tasks_for_source',
    'segment_answer_entries',
    'segment_task_blocks',
]

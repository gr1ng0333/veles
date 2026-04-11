from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import initialize_database
from .ingest import create_import_issue, create_import_run, finalize_import_run, utc_now

try:  # optional dependency, graceful fallback in current repo env
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - exercised via runtime environment
    fitz = None


BLOCK_HEADER_RE = re.compile(
    r'^\s*(?:задани[ея]|блок|раздел|section|part)\s*№?\s*(?P<code>[\w.-]{1,24})\s*[:.)-]?\s*(?P<title>.*)\s*$',
    re.IGNORECASE,
)
ANSWER_HEADER_RE = re.compile(r'^\s*(?P<number>\d{1,3}[A-Za-zА-Яа-я]?)\s*[).:]\s*(?P<body>.*)$')
WHITESPACE_RE = re.compile(r'\s+')


class AnswerParseError(RuntimeError):
    """Raised when answer PDF parsing cannot be completed."""


@dataclass(slots=True)
class AnswerPage:
    page_number: int
    text: str


@dataclass(slots=True)
class AnswerEntry:
    block_code: str
    task_number: str
    ordinal: int
    answer_text: str
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AnswerPdfBundle:
    pages: list[AnswerPage]
    parser_backends: dict[str, str]


@dataclass(slots=True)
class AnswerParseIssue:
    severity: str
    issue_type: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def parse_answers_for_source(
    db_path: Path,
    *,
    source_file_id: int,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    conn = initialize_database(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT id, set_id, relative_path, file_kind, status, metadata_json FROM source_files WHERE id = ?',
        (source_file_id,),
    ).fetchone()
    if row is None:
        raise AnswerParseError(f'source_file_id={source_file_id} not found')
    if row['file_kind'] != 'answers_pdf':
        raise AnswerParseError(f'source_file_id={source_file_id} has file_kind={row["file_kind"]}, expected answers_pdf')

    metadata = json.loads(row['metadata_json'] or '{}')
    effective_storage_root = Path(storage_root or metadata.get('storage_root') or Path(db_path).parent / 'pdf_variant_bot_storage')
    pdf_path = effective_storage_root / row['relative_path']
    if not pdf_path.exists():
        raise FileNotFoundError(f'Answer PDF not found: {pdf_path}')

    bundle = _load_pdf_bundle(pdf_path)
    entries, parse_issues = segment_answer_entries(bundle.pages)
    task_index = _load_task_index(conn, set_id=row['set_id'])

    run_id = None
    matched = 0
    unmatched = 0
    ambiguous = 0
    inserted = 0

    try:
        with conn:
            run_id = create_import_run(
                conn,
                {
                    'import_kind': 'answers_parse',
                    'source_path': str(pdf_path),
                    'status': 'running',
                    'started_at': utc_now(),
                    'stats_json': json.dumps(
                        {
                            'source_file_id': source_file_id,
                            'set_id': row['set_id'],
                            'pdf_relative_path': row['relative_path'],
                            'page_count': len(bundle.pages),
                            'parser_backends': bundle.parser_backends,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
            conn.execute('DELETE FROM answers WHERE source_file_id = ?', (source_file_id,))

            for issue in parse_issues:
                create_import_issue(
                    conn,
                    {
                        'run_id': run_id,
                        'source_file_id': source_file_id,
                        'task_id': None,
                        'severity': issue.severity,
                        'issue_type': issue.issue_type,
                        'message': issue.message,
                        'context_json': json.dumps(issue.context, ensure_ascii=False, sort_keys=True),
                    },
                )

            for entry in entries:
                key = (_normalize_block_code(entry.block_code), _normalize_task_number(entry.task_number))
                candidates = task_index.get(key, [])
                task_id = None
                confidence = 0.0
                issue_type = None
                issue_message = None
                issue_context: dict[str, Any] = {
                    'block_code': entry.block_code,
                    'task_number': entry.task_number,
                    'candidate_task_ids': [candidate['task_id'] for candidate in candidates],
                }
                if len(candidates) == 1:
                    task_id = candidates[0]['task_id']
                    confidence = 1.0
                    matched += 1
                elif not candidates:
                    unmatched += 1
                    issue_type = 'unmatched_answer_task'
                    issue_message = 'Answer entry could not be matched to any parsed task'
                else:
                    ambiguous += 1
                    issue_type = 'ambiguous_answer_match'
                    issue_message = 'Answer entry matches multiple parsed tasks; leaving task_id unresolved'

                if issue_type is not None:
                    create_import_issue(
                        conn,
                        {
                            'run_id': run_id,
                            'source_file_id': source_file_id,
                            'task_id': task_id,
                            'severity': 'warning',
                            'issue_type': issue_type,
                            'message': issue_message,
                            'context_json': json.dumps(issue_context, ensure_ascii=False, sort_keys=True),
                        },
                    )

                conn.execute(
                    '''
                    INSERT INTO answers (
                        task_id,
                        source_file_id,
                        block_code,
                        task_number,
                        answer_text,
                        page_start,
                        page_end,
                        confidence,
                        metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        task_id,
                        source_file_id,
                        entry.block_code,
                        entry.task_number,
                        entry.answer_text,
                        entry.page_start,
                        entry.page_end,
                        confidence,
                        json.dumps(entry.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
                inserted += 1

            updated_metadata = dict(metadata)
            parse_state = dict(updated_metadata.get('answer_parse', {}))
            parse_state.update(
                {
                    'page_count': len(bundle.pages),
                    'last_run_id': run_id,
                    'parser_backends': bundle.parser_backends,
                    'matched_answers': matched,
                    'unmatched_answers': unmatched,
                    'ambiguous_answers': ambiguous,
                }
            )
            updated_metadata['answer_parse'] = parse_state
            conn.execute(
                'UPDATE source_files SET status = ?, page_count = ?, metadata_json = ? WHERE id = ?',
                ('parsed_answers', len(bundle.pages), json.dumps(updated_metadata, ensure_ascii=False, sort_keys=True), source_file_id),
            )

            finalize_import_run(
                conn,
                run_id,
                status='completed',
                payload={
                    'finished_at': utc_now(),
                    'stats_json': json.dumps(
                        {
                            'source_file_id': source_file_id,
                            'set_id': row['set_id'],
                            'page_count': len(bundle.pages),
                            'entries': len(entries),
                            'matched': matched,
                            'unmatched': unmatched,
                            'ambiguous': ambiguous,
                            'inserted': inserted,
                            'parser_backends': bundle.parser_backends,
                            'issues_logged': len(parse_issues) + unmatched + ambiguous,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
    except Exception:
        if run_id is not None:
            with conn:
                finalize_import_run(
                    conn,
                    run_id,
                    status='failed',
                    payload={
                        'finished_at': utc_now(),
                        'stats_json': json.dumps(
                            {
                                'source_file_id': source_file_id,
                                'page_count': len(bundle.pages),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                )
        raise
    finally:
        conn.close()

    return {
        'source_file_id': source_file_id,
        'pdf_path': str(pdf_path),
        'page_count': len(bundle.pages),
        'entries': len(entries),
        'matched': matched,
        'unmatched': unmatched,
        'ambiguous': ambiguous,
        'inserted': inserted,
        'parser_backends': bundle.parser_backends,
        'issues_logged': len(parse_issues) + unmatched + ambiguous,
        'status': 'parsed_answers',
    }


def segment_answer_entries(pages: list[AnswerPage]) -> tuple[list[AnswerEntry], list[AnswerParseIssue]]:
    entries: list[AnswerEntry] = []
    issues: list[AnswerParseIssue] = []

    current_block_code: str | None = None
    current_block_title = ''
    current_task_number: str | None = None
    current_task_lines: list[str] = []
    current_task_page_start: int | None = None
    current_task_page_end: int | None = None

    def flush_current_task() -> None:
        nonlocal current_task_number, current_task_lines, current_task_page_start, current_task_page_end
        if current_task_number is None:
            current_task_lines = []
            current_task_page_start = None
            current_task_page_end = None
            return
        answer_text = _compact_answer_lines(current_task_lines)
        entries.append(
            AnswerEntry(
                block_code=current_block_code or '',
                task_number=current_task_number,
                ordinal=len(entries) + 1,
                answer_text=answer_text,
                page_start=current_task_page_start,
                page_end=current_task_page_end,
                metadata={
                    'block_title': current_block_title,
                },
            )
        )
        current_task_number = None
        current_task_lines = []
        current_task_page_start = None
        current_task_page_end = None

    for page in pages:
        for raw_line in page.text.splitlines():
            line = raw_line.strip()
            if not line:
                if current_task_number is not None and current_task_lines and current_task_lines[-1] != '':
                    current_task_lines.append('')
                continue

            block_match = BLOCK_HEADER_RE.match(line)
            if block_match:
                flush_current_task()
                current_block_code = _normalize_block_code(block_match.group('code'))
                current_block_title = _clean_text(block_match.group('title'))
                continue

            task_match = ANSWER_HEADER_RE.match(line)
            if task_match:
                flush_current_task()
                if current_block_code is None:
                    issues.append(
                        AnswerParseIssue(
                            severity='warning',
                            issue_type='missing_block_context',
                            message='Answer task started before any block header was detected',
                            context={
                                'page_number': page.page_number,
                                'line': line,
                            },
                        )
                    )
                    current_block_code = ''
                    current_block_title = ''
                current_task_number = _normalize_task_number(task_match.group('number'))
                current_task_page_start = page.page_number
                current_task_page_end = page.page_number
                body = _clean_text(task_match.group('body'))
                current_task_lines = [body] if body else []
                continue

            if current_task_number is None:
                issues.append(
                    AnswerParseIssue(
                        severity='warning',
                        issue_type='unattached_answer_text',
                        message='Encountered answer text that does not belong to a parsed task header',
                        context={
                            'page_number': page.page_number,
                            'line': line,
                            'block_code': current_block_code or '',
                        },
                    )
                )
                continue

            current_task_lines.append(line)
            current_task_page_end = page.page_number

    flush_current_task()
    return entries, issues


def _load_task_index(conn: sqlite3.Connection, *, set_id: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = conn.execute(
        '''
        SELECT t.id AS task_id, tb.block_code, t.task_number
        FROM tasks AS t
        JOIN task_blocks AS tb ON tb.id = t.block_id
        JOIN source_files AS sf ON sf.id = tb.source_file_id
        WHERE sf.set_id = ?
        ORDER BY t.id ASC
        ''',
        (set_id,),
    ).fetchall()
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (_normalize_block_code(row['block_code']), _normalize_task_number(row['task_number']))
        index.setdefault(key, []).append({'task_id': row['task_id']})
    return index


def _load_pdf_bundle(pdf_path: Path) -> AnswerPdfBundle:
    if fitz is None:
        raise AnswerParseError(
            'PyMuPDF is not installed in this environment; cannot parse answer PDFs until dependency is available'
        )

    doc = fitz.open(pdf_path)  # type: ignore[arg-type]
    try:
        pages = [
            AnswerPage(
                page_number=page_index + 1,
                text=page.get_text('text'),
            )
            for page_index, page in enumerate(doc)
        ]
    finally:
        doc.close()

    return AnswerPdfBundle(
        pages=pages,
        parser_backends={
            'page_text': 'pymupdf',
        },
    )


def _compact_answer_lines(lines: list[str]) -> str:
    normalized: list[str] = []
    for line in lines:
        if not line:
            if normalized and normalized[-1] != '':
                normalized.append('')
            continue
        normalized.append(_clean_text(line))
    return '\n'.join(part for part in normalized).strip()


def _normalize_block_code(value: str) -> str:
    normalized = _clean_text(value).strip('.):;-_ ')
    return normalized


def _normalize_task_number(value: str) -> str:
    return _clean_text(value).strip('.):;-_ ')


def _clean_text(value: str) -> str:
    return WHITESPACE_RE.sub(' ', value or '').strip()

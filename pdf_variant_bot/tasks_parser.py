from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .db import initialize_database
from .ingest import create_import_issue, create_import_run, finalize_import_run, sha256_file, utc_now

try:  # optional dependency, graceful fallback in current repo env
    import fitz  # type: ignore
except ImportError:  # pragma: no cover - exercised via runtime environment
    fitz = None

try:  # optional dependency, graceful fallback in current repo env
    from markitdown import MarkItDown  # type: ignore
except ImportError:  # pragma: no cover - exercised via runtime environment
    MarkItDown = None


BLOCK_HEADER_RE = re.compile(
    r'^\s*(?:задани[ея]|блок|раздел|section|part)\s*№?\s*(?P<code>[\w.-]{1,24})\s*[:.)-]?\s*(?P<title>.*)\s*$',
    re.IGNORECASE,
)
TASK_HEADER_RE = re.compile(r'^\s*(?P<number>\d{1,3}[A-Za-zА-Яа-я]?)\s*[).]\s*(?P<title>.*\S)?\s*$')
WHITESPACE_RE = re.compile(r'\s+')


class TaskParseError(RuntimeError):
    """Raised when task PDF parsing cannot be completed."""


@dataclass(slots=True)
class ExtractedAsset:
    page_number: int
    asset_index: int
    relative_path: str
    mime_type: str
    sha256: str
    width: int | None = None
    height: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PageBundle:
    page_number: int
    text: str
    assets: list[ExtractedAsset] = field(default_factory=list)


@dataclass(slots=True)
class TaskPayload:
    task_number: str
    ordinal: int
    title: str
    prompt_text: str
    page_start: int | None
    page_end: int | None
    fingerprint: str
    metadata: dict[str, Any] = field(default_factory=dict)
    assets: list[ExtractedAsset] = field(default_factory=list)


@dataclass(slots=True)
class BlockPayload:
    block_code: str
    ordinal: int
    title: str
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any] = field(default_factory=dict)
    tasks: list[TaskPayload] = field(default_factory=list)


@dataclass(slots=True)
class PdfBundle:
    pages: list[PageBundle]
    markdown_text: str
    parser_backends: dict[str, str]


@dataclass(slots=True)
class ParseIssue:
    severity: str
    issue_type: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def parse_tasks_for_source(
    db_path: Path,
    *,
    source_file_id: int,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    conn = initialize_database(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT id, relative_path, file_kind, status, metadata_json FROM source_files WHERE id = ?',
        (source_file_id,),
    ).fetchone()
    if row is None:
        raise TaskParseError(f'source_file_id={source_file_id} not found')
    if row['file_kind'] != 'tasks_pdf':
        raise TaskParseError(f'source_file_id={source_file_id} has file_kind={row["file_kind"]}, expected tasks_pdf')

    metadata = _loads_json(row['metadata_json'])
    resolved_storage_root = _resolve_storage_root(storage_root, metadata)
    pdf_path = _resolve_source_pdf_path(resolved_storage_root, row['relative_path'])
    if not pdf_path.exists():
        raise FileNotFoundError(f'Parsed PDF source not found: {pdf_path}')

    run_id = create_import_run(
        conn,
        {
            'import_kind': 'tasks_parse',
            'source_path': str(pdf_path),
            'status': 'running',
            'started_at': utc_now(),
            'stats_json': json.dumps({'source_file_id': source_file_id}, ensure_ascii=False, sort_keys=True),
            'notes': f'tasks parse for source_file_id={source_file_id}',
        },
    )

    try:
        assets_root = resolved_storage_root / 'assets' / f'source_{source_file_id}'
        bundle = _load_pdf_bundle(pdf_path, assets_root=assets_root)
        blocks, issues = segment_task_blocks(bundle.pages)
        _persist_parse_result(
            conn,
            source_file_id=source_file_id,
            source_row=row,
            original_metadata=metadata,
            bundle=bundle,
            blocks=blocks,
            issues=issues,
            pdf_path=pdf_path,
            storage_root=resolved_storage_root,
            run_id=run_id,
        )
        summary = {
            'source_file_id': source_file_id,
            'relative_path': row['relative_path'],
            'pdf_path': str(pdf_path),
            'storage_root': str(resolved_storage_root),
            'page_count': len(bundle.pages),
            'blocks_created': len(blocks),
            'tasks_created': sum(len(block.tasks) for block in blocks),
            'assets_created': sum(len(task.assets) for block in blocks for task in block.tasks),
            'issues_created': len(issues),
            'parser_backends': bundle.parser_backends,
            'run_id': run_id,
            'source_sha256': sha256_file(pdf_path),
            'status': 'completed',
        }
        finalize_import_run(conn, run_id, status='completed', payload=summary)
        conn.close()
        return summary
    except Exception as exc:
        finalize_import_run(
            conn,
            run_id,
            status='failed',
            payload={
                'source_file_id': source_file_id,
                'error': str(exc),
                'pdf_path': str(pdf_path),
            },
        )
        conn.close()
        raise


def segment_task_blocks(pages: Sequence[PageBundle]) -> tuple[list[BlockPayload], list[ParseIssue]]:
    issues: list[ParseIssue] = []
    blocks: list[dict[str, Any]] = []
    explicit_blocks_found = False
    synthetic_block_counter = 0
    block_ordinal = 0
    current_block: dict[str, Any] | None = None
    current_task: dict[str, Any] | None = None

    def flush_task() -> None:
        nonlocal current_task
        if current_block is None or current_task is None:
            return
        prompt_text = '\n'.join(current_task['lines']).strip()
        if not prompt_text:
            current_task = None
            return
        pages_sorted = sorted(current_task['pages'])
        title = current_task['title'] or _derive_title(prompt_text)
        current_block['tasks'].append(
            TaskPayload(
                task_number=current_task['task_number'],
                ordinal=len(current_block['tasks']) + 1,
                title=title,
                prompt_text=prompt_text,
                page_start=pages_sorted[0] if pages_sorted else None,
                page_end=pages_sorted[-1] if pages_sorted else None,
                fingerprint=_fingerprint_text(prompt_text),
                metadata={
                    'source_pages': pages_sorted,
                    'header_line': current_task['header_line'],
                },
            )
        )
        current_task = None

    def flush_block() -> None:
        nonlocal current_block
        flush_task()
        if current_block is None:
            return
        if current_block['tasks']:
            task_pages = [page for task in current_block['tasks'] for page in range(task.page_start or 0, (task.page_end or 0) + 1)]
            pages_sorted = sorted(set(page for page in task_pages if page > 0) | current_block['pages'])
            blocks.append(
                BlockPayload(
                    block_code=current_block['block_code'],
                    ordinal=current_block['ordinal'],
                    title=current_block['title'] or f'Block {current_block["block_code"]}',
                    page_start=pages_sorted[0] if pages_sorted else None,
                    page_end=pages_sorted[-1] if pages_sorted else None,
                    metadata={
                        'header_line': current_block['header_line'],
                        'preamble': current_block['preamble'],
                        'explicit_header': current_block['explicit_header'],
                    },
                    tasks=current_block['tasks'],
                )
            )
        else:
            issues.append(
                ParseIssue(
                    severity='warning',
                    issue_type='empty_block',
                    message=f'Block {current_block["block_code"]} did not produce any tasks',
                    context={'block_code': current_block['block_code']},
                )
            )
        current_block = None

    def ensure_block(page_number: int) -> dict[str, Any]:
        nonlocal current_block, block_ordinal, synthetic_block_counter
        if current_block is not None:
            current_block['pages'].add(page_number)
            return current_block
        synthetic_block_counter += 1
        block_ordinal += 1
        block_code = 'unassigned' if synthetic_block_counter == 1 else f'unassigned-{synthetic_block_counter}'
        current_block = {
            'block_code': block_code,
            'ordinal': block_ordinal,
            'title': '',
            'header_line': '',
            'explicit_header': False,
            'pages': {page_number},
            'preamble': [],
            'tasks': [],
        }
        return current_block

    for page in pages:
        page_number = page.page_number
        for line in _iter_content_lines(page.text):
            block_match = BLOCK_HEADER_RE.match(line)
            if block_match:
                explicit_blocks_found = True
                flush_block()
                block_ordinal += 1
                current_block = {
                    'block_code': block_match.group('code').strip().rstrip('.)'),
                    'ordinal': block_ordinal,
                    'title': (block_match.group('title') or '').strip(),
                    'header_line': line.strip(),
                    'explicit_header': True,
                    'pages': {page_number},
                    'preamble': [],
                    'tasks': [],
                }
                continue

            task_match = TASK_HEADER_RE.match(line)
            if task_match:
                block = ensure_block(page_number)
                flush_task()
                task_number = task_match.group('number').strip()
                title = (task_match.group('title') or '').strip()
                current_task = {
                    'task_number': task_number,
                    'title': title,
                    'header_line': line.strip(),
                    'lines': [line.strip()],
                    'pages': {page_number},
                }
                continue

            if current_task is not None:
                current_task['lines'].append(line.strip())
                current_task['pages'].add(page_number)
                continue

            if current_block is not None:
                current_block['preamble'].append(line.strip())
                current_block['pages'].add(page_number)

    flush_block()

    if not explicit_blocks_found and blocks:
        issues.append(
            ParseIssue(
                severity='warning',
                issue_type='missing_explicit_block_headers',
                message='No explicit block headers were detected; synthetic block codes were used',
                context={'block_codes': [block.block_code for block in blocks]},
            )
        )

    issues.extend(_assign_assets_to_tasks(blocks, pages))
    return blocks, issues


def _load_pdf_bundle(pdf_path: Path, *, assets_root: Path) -> PdfBundle:
    page_bundles = _extract_pages_and_assets(pdf_path, assets_root=assets_root)
    markdown_text, markdown_backend = _extract_markdown_text(pdf_path)
    if not page_bundles and markdown_text.strip():
        page_bundles = [PageBundle(page_number=1, text=markdown_text, assets=[])]
    if not page_bundles:
        raise TaskParseError(f'Unable to extract any text from {pdf_path}')
    return PdfBundle(
        pages=page_bundles,
        markdown_text=markdown_text,
        parser_backends={
            'markitdown': markdown_backend,
            'pymupdf': 'available' if fitz is not None else 'missing',
        },
    )


def _extract_markdown_text(pdf_path: Path) -> tuple[str, str]:
    if MarkItDown is None:
        return '', 'missing'
    converter = MarkItDown()
    result = converter.convert(str(pdf_path))
    for attr in ('text_content', 'markdown', 'text', 'content'):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value, 'available'
    return str(result), 'available'


def _extract_pages_and_assets(pdf_path: Path, *, assets_root: Path) -> list[PageBundle]:
    if fitz is None:
        return []
    assets_root.mkdir(parents=True, exist_ok=True)
    pages: list[PageBundle] = []
    document = fitz.open(pdf_path)
    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_number = page_index + 1
            text = page.get_text('text') or ''
            assets: list[ExtractedAsset] = []
            for asset_index, image_info in enumerate(page.get_images(full=True), start=1):
                xref = image_info[0]
                extracted = document.extract_image(xref)
                image_bytes = extracted.get('image', b'')
                if not image_bytes:
                    continue
                ext = (extracted.get('ext') or 'bin').lower()
                asset_rel = Path('assets') / f'source_{pdf_path.stem}' / f'page-{page_number:03d}-image-{asset_index:02d}.{ext}'
                asset_abs = assets_root.parent.parent / asset_rel
                asset_abs.parent.mkdir(parents=True, exist_ok=True)
                if not asset_abs.exists():
                    asset_abs.write_bytes(image_bytes)
                assets.append(
                    ExtractedAsset(
                        page_number=page_number,
                        asset_index=asset_index,
                        relative_path=asset_rel.as_posix(),
                        mime_type=extracted.get('smask') and 'image/png' or _guess_mime_type(asset_abs),
                        sha256=hashlib.sha256(image_bytes).hexdigest(),
                        width=extracted.get('width'),
                        height=extracted.get('height'),
                        metadata={'xref': xref, 'ext': ext},
                    )
                )
            pages.append(PageBundle(page_number=page_number, text=text, assets=assets))
    finally:
        document.close()
    return pages


def _assign_assets_to_tasks(blocks: list[BlockPayload], pages: Sequence[PageBundle]) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    page_to_tasks: dict[int, list[TaskPayload]] = {}
    for block in blocks:
        for task in block.tasks:
            if task.page_start is None or task.page_end is None:
                continue
            for page_number in range(task.page_start, task.page_end + 1):
                page_to_tasks.setdefault(page_number, []).append(task)

    for page in pages:
        if not page.assets:
            continue
        candidates = page_to_tasks.get(page.page_number, [])
        if len(candidates) == 1:
            candidates[0].assets.extend(page.assets)
            continue
        issue_type = 'ambiguous_asset_page' if candidates else 'orphan_asset_page'
        issues.append(
            ParseIssue(
                severity='warning',
                issue_type=issue_type,
                message=f'Could not attach {len(page.assets)} assets from page {page.page_number} deterministically',
                context={
                    'page_number': page.page_number,
                    'candidate_task_count': len(candidates),
                    'asset_paths': [asset.relative_path for asset in page.assets],
                },
            )
        )
    return issues


def _persist_parse_result(
    conn: sqlite3.Connection,
    *,
    source_file_id: int,
    source_row: sqlite3.Row,
    original_metadata: dict[str, Any],
    bundle: PdfBundle,
    blocks: list[BlockPayload],
    issues: list[ParseIssue],
    pdf_path: Path,
    storage_root: Path,
    run_id: int | None,
) -> None:
    with conn:
        conn.execute('DELETE FROM task_blocks WHERE source_file_id = ?', (source_file_id,))
        for block in blocks:
            block_id = conn.execute(
                '''
                INSERT INTO task_blocks (source_file_id, block_code, ordinal, title, page_start, page_end, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    source_file_id,
                    block.block_code,
                    block.ordinal,
                    block.title,
                    block.page_start,
                    block.page_end,
                    json.dumps(block.metadata, ensure_ascii=False, sort_keys=True),
                ),
            ).lastrowid
            for task in block.tasks:
                task_id = conn.execute(
                    '''
                    INSERT INTO tasks (block_id, task_number, ordinal, title, prompt_text, page_start, page_end, fingerprint, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        block_id,
                        task.task_number,
                        task.ordinal,
                        task.title,
                        task.prompt_text,
                        task.page_start,
                        task.page_end,
                        task.fingerprint,
                        json.dumps(task.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                ).lastrowid
                for asset in task.assets:
                    conn.execute(
                        '''
                        INSERT INTO task_assets (
                            task_id, source_file_id, page_number, asset_index, asset_kind,
                            relative_path, mime_type, sha256, width, height, metadata_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (
                            task_id,
                            source_file_id,
                            asset.page_number,
                            asset.asset_index,
                            'image',
                            asset.relative_path,
                            asset.mime_type,
                            asset.sha256,
                            asset.width,
                            asset.height,
                            json.dumps(asset.metadata, ensure_ascii=False, sort_keys=True),
                        ),
                    )

        for issue in issues:
            create_import_issue(
                conn,
                {
                    'run_id': run_id,
                    'source_file_id': source_file_id,
                    'severity': issue.severity,
                    'issue_type': issue.issue_type,
                    'message': issue.message,
                    'context_json': json.dumps(issue.context, ensure_ascii=False, sort_keys=True),
                },
            )

        updated_metadata = dict(original_metadata)
        updated_metadata['task_parse'] = {
            'parsed_at': utc_now(),
            'page_count': len(bundle.pages),
            'blocks_created': len(blocks),
            'tasks_created': sum(len(block.tasks) for block in blocks),
            'assets_created': sum(len(task.assets) for block in blocks for task in block.tasks),
            'issues_created': len(issues),
            'parser_backends': bundle.parser_backends,
            'markdown_sha256': _fingerprint_text(bundle.markdown_text) if bundle.markdown_text else '',
            'markdown_length': len(bundle.markdown_text),
            'storage_root': str(storage_root),
            'pdf_path': str(pdf_path),
        }
        conn.execute(
            'UPDATE source_files SET page_count = ?, status = ?, metadata_json = ? WHERE id = ?',
            (
                len(bundle.pages),
                'parsed_tasks',
                json.dumps(updated_metadata, ensure_ascii=False, sort_keys=True),
                source_file_id,
            ),
        )


def _resolve_storage_root(storage_root: Path | None, metadata: dict[str, Any]) -> Path:
    if storage_root is not None:
        return Path(storage_root).resolve()
    stored = metadata.get('storage_root')
    if stored:
        return Path(stored).resolve()
    raise TaskParseError('storage_root is required either explicitly or via source_files.metadata_json')


def _resolve_source_pdf_path(storage_root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute():
        return candidate
    return (storage_root / candidate).resolve()


def _iter_content_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = WHITESPACE_RE.sub(' ', raw).strip()
        if line:
            lines.append(line)
    return lines


def _loads_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _fingerprint_text(text: str) -> str:
    normalized = WHITESPACE_RE.sub(' ', text).strip()
    return hashlib.sha1(normalized.encode('utf-8')).hexdigest() if normalized else ''


def _derive_title(text: str) -> str:
    compact = WHITESPACE_RE.sub(' ', text).strip()
    return compact[:120]


def _guess_mime_type(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type or 'application/octet-stream'

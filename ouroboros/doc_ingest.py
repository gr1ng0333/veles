from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Any, Dict

from ouroboros.artifacts import save_incoming_artifact

_DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
_TEXT_MIME = 'text/plain'
_JSON_MIME = 'application/json'
_DOC_MIME = 'application/msword'


def _strip_xml_text(xml_text: str) -> str:
    if not xml_text:
        return ''
    text = xml_text.replace('</w:p>', '\n').replace('</w:tr>', '\n').replace('</w:tbl>', '\n')
    text = re.sub(r'<w:tab[^>]*/>', '\t', text)
    text = re.sub(r'<w:br[^>]*/>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = (
        text.replace('&amp;', '&')
        .replace('&lt;', '<')
        .replace('&gt;', '>')
        .replace('&quot;', '"')
        .replace('&apos;', "'")
    )
    text = text.replace('\r', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    lines = [line.rstrip() for line in text.split('\n')]
    return '\n'.join(lines).strip()


def extract_text_from_docx_bytes(payload: bytes) -> str:
    if not payload:
        return ''
    parts = []
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    try:
        with zipfile.ZipFile(tmp_path) as zf:
            for name in sorted(zf.namelist()):
                if not (
                    name == 'word/document.xml'
                    or name.startswith('word/header')
                    or name.startswith('word/footer')
                ):
                    continue
                try:
                    xml_bytes = zf.read(name)
                except KeyError:
                    continue
                xml_text = xml_bytes.decode('utf-8', errors='ignore')
                clean = _strip_xml_text(xml_text)
                if clean:
                    parts.append(clean)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return '\n\n'.join(part for part in parts if part).strip()


def _pick_converter() -> Dict[str, str]:
    for candidate in ('libreoffice', 'soffice'):
        path = shutil.which(candidate)
        if path:
            return {'kind': 'libreoffice', 'binary': path}
    path = shutil.which('antiword')
    if path:
        return {'kind': 'antiword', 'binary': path}
    path = shutil.which('catdoc')
    if path:
        return {'kind': 'catdoc', 'binary': path}
    return {'kind': '', 'binary': ''}


def ingest_legacy_word_document(
    *,
    drive_root: pathlib.Path,
    file_name: str,
    file_bytes: bytes,
    chat_id: int,
    caption: str,
    message_id: int = 0,
    telegram_file_id: str = '',
    activation_mode: str = 'deferred',
    timeout_sec: int = 60,
) -> Dict[str, Any]:
    normalized_name = file_name or 'file.doc'
    meta_common = {
        'message_id': int(message_id or 0),
        'telegram_file_id': telegram_file_id or '',
        'activation_mode': activation_mode,
        'ingest_kind': 'legacy_word_doc',
    }
    original_meta = save_incoming_artifact(
        drive_root,
        filename=normalized_name,
        data=file_bytes,
        content_kind='doc',
        mime_type=_DOC_MIME,
        chat_id=chat_id,
        caption=caption,
        metadata=meta_common,
    )
    result: Dict[str, Any] = {
        'status': 'archived',
        'converter': '',
        'converter_binary': '',
        'original': original_meta,
        'docx': None,
        'text': None,
        'metadata': None,
        'errors': [],
        'extracted_text': '',
    }

    converter = _pick_converter()
    result['converter'] = converter.get('kind') or ''
    result['converter_binary'] = converter.get('binary') or ''

    tmpdir = tempfile.mkdtemp(prefix='legacy-doc-')
    input_path = pathlib.Path(tmpdir) / normalized_name
    input_path.write_bytes(file_bytes)
    stem = pathlib.Path(normalized_name).stem or 'document'
    docx_name = f'{stem}.docx'
    txt_name = f'{stem}.txt'

    try:
        if converter['kind'] == 'libreoffice':
            cmd = [converter['binary'], '--headless', '--convert-to', 'docx', '--outdir', tmpdir, str(input_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode != 0:
                result['errors'].append({
                    'stage': 'convert-docx',
                    'kind': 'libreoffice_failed',
                    'returncode': proc.returncode,
                    'stderr': (proc.stderr or '').strip()[:4000],
                    'stdout': (proc.stdout or '').strip()[:4000],
                })
            out_path = pathlib.Path(tmpdir) / docx_name
            if out_path.exists():
                docx_bytes = out_path.read_bytes()
                docx_meta = save_incoming_artifact(
                    drive_root,
                    filename=docx_name,
                    data=docx_bytes,
                    content_kind='docx',
                    mime_type=_DOCX_MIME,
                    chat_id=chat_id,
                    caption=caption,
                    metadata={**meta_common, 'derived_from': normalized_name, 'converter': converter['kind']},
                )
                result['docx'] = docx_meta
                result['status'] = 'converted'
                extracted = extract_text_from_docx_bytes(docx_bytes)
                if extracted:
                    text_meta = save_incoming_artifact(
                        drive_root,
                        filename=txt_name,
                        content=extracted,
                        content_kind='text',
                        mime_type=_TEXT_MIME,
                        chat_id=chat_id,
                        caption=caption,
                        metadata={**meta_common, 'derived_from': docx_name, 'extractor': 'docx-xml'},
                    )
                    result['text'] = text_meta
                    result['extracted_text'] = extracted
        elif converter['kind'] in {'antiword', 'catdoc'}:
            cmd = [converter['binary'], str(input_path)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode == 0:
                extracted = (proc.stdout or '').strip()
                if extracted:
                    text_meta = save_incoming_artifact(
                        drive_root,
                        filename=txt_name,
                        content=extracted,
                        content_kind='text',
                        mime_type=_TEXT_MIME,
                        chat_id=chat_id,
                        caption=caption,
                        metadata={**meta_common, 'derived_from': normalized_name, 'extractor': converter['kind']},
                    )
                    result['text'] = text_meta
                    result['extracted_text'] = extracted
                    result['status'] = 'text-extracted'
            else:
                result['errors'].append({
                    'stage': 'extract-text',
                    'kind': f"{converter['kind']}_failed",
                    'returncode': proc.returncode,
                    'stderr': (proc.stderr or '').strip()[:4000],
                    'stdout': (proc.stdout or '').strip()[:4000],
                })
        else:
            result['status'] = 'converter-unavailable'
            result['errors'].append({
                'stage': 'probe',
                'kind': 'converter_unavailable',
                'message': 'No libreoffice/soffice/antiword/catdoc found in runtime.',
            })
    except subprocess.TimeoutExpired:
        result['status'] = 'converter-timeout'
        result['errors'].append({
            'stage': 'convert',
            'kind': 'timeout',
            'message': f'Converter exceeded {timeout_sec}s timeout.',
        })
    finally:
        try:
            for child in pathlib.Path(tmpdir).iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
            pathlib.Path(tmpdir).rmdir()
        except OSError:
            pass

    metadata_content = json.dumps(
        {
            'source_filename': normalized_name,
            'activation_mode': activation_mode,
            'status': result['status'],
            'converter': result['converter'],
            'converter_binary': result['converter_binary'],
            'artifacts': {
                'original': result['original']['relative_path'] if isinstance(result['original'], dict) else '',
                'docx': result['docx']['relative_path'] if isinstance(result['docx'], dict) else '',
                'text': result['text']['relative_path'] if isinstance(result['text'], dict) else '',
            },
            'errors': result['errors'],
        },
        ensure_ascii=False,
        indent=2,
    )
    metadata_meta = save_incoming_artifact(
        drive_root,
        filename=f'{stem}.doc.ingest.json',
        content=metadata_content,
        content_kind='metadata',
        mime_type=_JSON_MIME,
        chat_id=chat_id,
        caption=caption,
        metadata={**meta_common, 'derived_from': normalized_name},
    )
    result['metadata'] = metadata_meta
    return result

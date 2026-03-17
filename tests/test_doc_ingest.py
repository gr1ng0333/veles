import json
import pathlib
import zipfile

from ouroboros.doc_ingest import extract_text_from_docx_bytes, ingest_legacy_word_document


def _make_docx_bytes(text: str) -> bytes:
    import io

    buf = io.BytesIO()
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('word/document.xml', document_xml)
    return buf.getvalue()


def test_extract_text_from_docx_bytes_reads_word_document_xml():
    payload = _make_docx_bytes('Привет, мир')
    text = extract_text_from_docx_bytes(payload)
    assert 'Привет, мир' in text


def test_ingest_legacy_doc_without_converter_still_archives_and_writes_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr('ouroboros.doc_ingest.shutil.which', lambda name: None)
    result = ingest_legacy_word_document(
        drive_root=tmp_path,
        file_name='LR.doc',
        file_bytes=b'\xd0\xcf\x11\xe0fake-doc',
        chat_id=1,
        caption='проверь',
        message_id=42,
        telegram_file_id='tg-file',
        activation_mode='immediate',
    )
    assert result['status'] == 'converter-unavailable'
    assert isinstance(result['original'], dict)
    assert isinstance(result['metadata'], dict)
    assert result['text'] is None
    meta_path = tmp_path / result['metadata']['relative_path']
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    assert meta['status'] == 'converter-unavailable'
    assert meta['artifacts']['original']
    assert meta['artifacts']['docx'] == ''
    assert meta['artifacts']['text'] == ''


class _Completed:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ingest_legacy_doc_with_libreoffice_saves_docx_and_text(tmp_path, monkeypatch):
    docx_payload = _make_docx_bytes('Титульник лабораторной')

    def fake_which(name):
        if name == 'libreoffice':
            return '/usr/bin/libreoffice'
        return None

    def fake_run(cmd, capture_output, text, timeout):
        outdir = pathlib.Path(cmd[cmd.index('--outdir') + 1])
        src = pathlib.Path(cmd[-1])
        (outdir / f'{src.stem}.docx').write_bytes(docx_payload)
        return _Completed(returncode=0, stdout='ok', stderr='')

    monkeypatch.setattr('ouroboros.doc_ingest.shutil.which', fake_which)
    monkeypatch.setattr('ouroboros.doc_ingest.subprocess.run', fake_run)

    result = ingest_legacy_word_document(
        drive_root=tmp_path,
        file_name='LR.doc',
        file_bytes=b'\xd0\xcf\x11\xe0fake-doc',
        chat_id=1,
        caption='проверь',
        activation_mode='immediate',
    )

    assert result['status'] == 'converted'
    assert isinstance(result['docx'], dict)
    assert isinstance(result['text'], dict)
    assert 'Титульник лабораторной' in result['extracted_text']

    txt_path = tmp_path / result['text']['relative_path']
    assert 'Титульник лабораторной' in txt_path.read_text(encoding='utf-8')

    meta_path = tmp_path / result['metadata']['relative_path']
    meta = json.loads(meta_path.read_text(encoding='utf-8'))
    assert meta['status'] == 'converted'
    assert meta['artifacts']['docx']
    assert meta['artifacts']['text']

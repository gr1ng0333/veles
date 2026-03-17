import json
import pathlib
import zipfile

from ouroboros.doc_ingest import ingest_legacy_word_document


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


def test_ingest_legacy_doc_with_libreoffice_saves_docx_and_text(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    buf = io.BytesIO()
    document_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' '<w:body><w:p><w:r><w:t>Титульник лабораторной</w:t></w:r></w:p></w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' '<Default Extension="xml" ContentType="application/xml"/>' '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>' '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' '</Relationships>')
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('word/document.xml', document_xml)
    docx_payload = buf.getvalue()

    monkeypatch.setattr(
        'ouroboros.doc_ingest.shutil.which',
        lambda name: '/usr/bin/libreoffice' if name == 'libreoffice' else None,
    )
    monkeypatch.setattr(
        'ouroboros.doc_ingest.subprocess.run',
        lambda cmd, capture_output, text, timeout: (
            (
                pathlib.Path(cmd[cmd.index('--outdir') + 1])
                / f"{pathlib.Path(cmd[-1]).stem}.docx"
            ).write_bytes(docx_payload),
            SimpleNamespace(returncode=0, stdout='ok', stderr=''),
        )[1],
    )

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

import json
import sqlite3
import zipfile
from pathlib import Path

from pdf_variant_bot.cli import main
from pdf_variant_bot.ingest import import_archive


PDF_BYTES = b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n'


def build_archive(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, 'w') as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)


def test_cli_import_archive_registers_archive_and_extracted_files(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    archive_path = tmp_path / 'eg_bank.zip'
    storage_root = tmp_path / 'storage'
    build_archive(
        archive_path,
        {
            'EG/tasks_15.pdf': PDF_BYTES,
            'EG/answers_15.pdf': PDF_BYTES + b'answers',
            'EG/readme.txt': b'notes',
        },
    )

    assert main([
        'import-archive',
        str(db_path),
        str(archive_path),
        '--slug',
        'eg-bank',
        '--title',
        'EG Bank',
        '--storage-root',
        str(storage_root),
    ]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary['set_slug'] == 'eg-bank'
    assert summary['registered_files'] == {'new': 4, 'existing': 0, 'total_seen': 4}
    assert summary['file_kinds'] == {'answers_pdf': 1, 'archive': 1, 'other': 1, 'tasks_pdf': 1}

    conn = sqlite3.connect(db_path)
    assert conn.execute('SELECT COUNT(*) FROM source_sets').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 4
    assert conn.execute('SELECT COUNT(*) FROM import_runs').fetchone()[0] == 1
    kinds = dict(conn.execute('SELECT file_kind, COUNT(*) FROM source_files GROUP BY file_kind').fetchall())
    assert kinds == {'answers_pdf': 1, 'archive': 1, 'other': 1, 'tasks_pdf': 1}
    conn.close()

    assert (storage_root / summary['stored_archive']).exists()
    assert (storage_root / summary['extracted_root'] / 'EG' / 'tasks_15.pdf').exists()
    assert (storage_root / summary['extracted_root'] / 'EG' / 'answers_15.pdf').exists()


def test_import_archive_is_idempotent_for_same_archive(tmp_path):
    db_path = tmp_path / 'variants.sqlite3'
    archive_path = tmp_path / 'ug_bank.zip'
    storage_root = tmp_path / 'storage'
    build_archive(
        archive_path,
        {
            'UG/tasks_10.pdf': PDF_BYTES,
            'UG/answers_10.pdf': PDF_BYTES + b'ans',
        },
    )

    first = import_archive(db_path, archive_path, set_slug='ug-bank', storage_root=storage_root)
    second = import_archive(db_path, archive_path, set_slug='ug-bank', storage_root=storage_root)

    assert first['registered_files'] == {'new': 3, 'existing': 0, 'total_seen': 3}
    assert second['registered_files'] == {'new': 0, 'existing': 3, 'total_seen': 3}
    assert first['archive_sha256'] == second['archive_sha256']
    assert first['set_id'] == second['set_id']
    assert first['extracted_now'] is True
    assert second['extracted_now'] is False

    conn = sqlite3.connect(db_path)
    assert conn.execute('SELECT COUNT(*) FROM source_sets').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM source_files').fetchone()[0] == 3
    assert conn.execute('SELECT COUNT(*) FROM import_runs').fetchone()[0] == 2
    statuses = [row[0] for row in conn.execute('SELECT status FROM import_runs ORDER BY id').fetchall()]
    assert statuses == ['completed', 'completed']
    conn.close()

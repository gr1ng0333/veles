import json
import sqlite3

from pdf_variant_bot.cli import main
from pdf_variant_bot.db import initialize_database
from pdf_variant_bot.tasks_parser import ExtractedAsset, PageBundle, PdfBundle, parse_tasks_for_source, segment_task_blocks


def test_segment_task_blocks_groups_pages_into_blocks_tasks_and_assets():
    pages = [
        PageBundle(
            page_number=1,
            text='''
            Задание 1. Статика
            1) Найти реакции опор
            Использовать уравнения равновесия.
            ''',
            assets=[
                ExtractedAsset(
                    page_number=1,
                    asset_index=1,
                    relative_path='assets/source_x/page-001-image-01.png',
                    mime_type='image/png',
                    sha256='abc',
                    width=100,
                    height=50,
                )
            ],
        ),
        PageBundle(
            page_number=2,
            text='''
            2) Построить эпюру моментов
            Подсказка: учесть распределённую нагрузку.
            Задание 2. Кинематика
            1) Определить скорость точки B
            ''',
            assets=[],
        ),
    ]

    blocks, issues = segment_task_blocks(pages)

    assert [block.block_code for block in blocks] == ['1', '2']
    assert [len(block.tasks) for block in blocks] == [2, 1]
    assert blocks[0].tasks[0].task_number == '1'
    assert blocks[0].tasks[0].page_start == 1
    assert blocks[0].tasks[0].page_end == 1
    assert blocks[0].tasks[0].assets[0].relative_path.endswith('.png')
    assert blocks[0].tasks[1].page_start == 2
    assert blocks[1].tasks[0].prompt_text.startswith('1) Определить скорость точки B')
    assert issues == []


def test_parse_tasks_for_source_persists_rows_via_fake_bundle(tmp_path, monkeypatch):
    db_path = tmp_path / 'variants.sqlite3'
    storage_root = tmp_path / 'storage'
    pdf_rel = 'extracted/eg-bank/abc/tasks_15.pdf'
    pdf_path = storage_root / pdf_rel
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%fake\n')

    conn = initialize_database(db_path)
    with conn:
        conn.execute(
            "INSERT INTO source_sets (slug, title, source_kind, notes) VALUES (?, ?, ?, ?)",
            ('eg-bank', 'EG Bank', 'archive', ''),
        )
        set_id = conn.execute("SELECT id FROM source_sets WHERE slug = ?", ('eg-bank',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                set_id,
                pdf_rel,
                'tasks_pdf',
                'stub-sha',
                pdf_path.stat().st_size,
                0,
                'pending',
                json.dumps({'storage_root': str(storage_root)}),
            ),
        )
        source_file_id = conn.execute("SELECT id FROM source_files WHERE relative_path = ?", (pdf_rel,)).fetchone()[0]
    conn.close()

    fake_bundle = PdfBundle(
        pages=[
            PageBundle(
                page_number=1,
                text='Задание 1. Статика\n1) Найти реакции опор\nПодробное условие задачи.',
                assets=[
                    ExtractedAsset(
                        page_number=1,
                        asset_index=1,
                        relative_path='assets/source_tasks_15/page-001-image-01.png',
                        mime_type='image/png',
                        sha256='img-sha',
                        width=32,
                        height=16,
                    )
                ],
            )
        ],
        markdown_text='# markdown',
        parser_backends={'markitdown': 'available', 'pymupdf': 'available'},
    )

    monkeypatch.setattr('pdf_variant_bot.tasks_parser._load_pdf_bundle', lambda *_args, **_kwargs: fake_bundle)

    summary = parse_tasks_for_source(db_path, source_file_id=source_file_id)

    assert summary['blocks_created'] == 1
    assert summary['tasks_created'] == 1
    assert summary['assets_created'] == 1
    assert summary['issues_created'] == 0

    conn = sqlite3.connect(db_path)
    assert conn.execute('SELECT COUNT(*) FROM task_blocks').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM task_assets').fetchone()[0] == 1
    assert conn.execute('SELECT status FROM source_files WHERE id = ?', (source_file_id,)).fetchone()[0] == 'parsed_tasks'
    assert conn.execute("SELECT COUNT(*) FROM import_runs WHERE import_kind = 'tasks_parse'").fetchone()[0] == 1
    conn.close()


def test_cli_parse_tasks_prints_machine_readable_summary(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / 'variants.sqlite3'
    storage_root = tmp_path / 'storage'
    pdf_rel = 'extracted/ug-bank/def/tasks_10.pdf'
    pdf_path = storage_root / pdf_rel
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b'%PDF-1.4\n%fake\n')

    conn = initialize_database(db_path)
    with conn:
        conn.execute(
            "INSERT INTO source_sets (slug, title, source_kind, notes) VALUES (?, ?, ?, ?)",
            ('ug-bank', 'UG Bank', 'archive', ''),
        )
        set_id = conn.execute("SELECT id FROM source_sets WHERE slug = ?", ('ug-bank',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                set_id,
                pdf_rel,
                'tasks_pdf',
                'stub-sha',
                pdf_path.stat().st_size,
                0,
                'pending',
                json.dumps({'storage_root': str(storage_root)}),
            ),
        )
        source_file_id = conn.execute("SELECT id FROM source_files WHERE relative_path = ?", (pdf_rel,)).fetchone()[0]
    conn.close()

    monkeypatch.setattr(
        'pdf_variant_bot.tasks_parser._load_pdf_bundle',
        lambda *_args, **_kwargs: PdfBundle(
            pages=[PageBundle(page_number=1, text='Задание 7. Блок\n1) Условие', assets=[])],
            markdown_text='Задание 7',
            parser_backends={'markitdown': 'fake', 'pymupdf': 'fake'},
        ),
    )

    assert main(['parse-tasks', str(db_path), '--source-file-id', str(source_file_id)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['source_file_id'] == source_file_id
    assert summary['blocks_created'] == 1
    assert summary['tasks_created'] == 1

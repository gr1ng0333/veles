import json
import sqlite3

from pdf_variant_bot.answers_parser import AnswerPage, AnswerPdfBundle, parse_answers_for_source, segment_answer_entries
from pdf_variant_bot.db import initialize_database


def test_segment_answer_entries_groups_by_block_and_task_number():
    pages = [
        AnswerPage(
            page_number=1,
            text='''
            Задание 1. Статика
            1) R_A = 5 кН
            2) M(x) = ql^2 / 8
            ''',
        ),
        AnswerPage(
            page_number=2,
            text='''
            Задание 2. Кинематика
            1) v_B = 3 м/с
            ''',
        ),
    ]

    entries, issues = segment_answer_entries(pages)

    assert [(entry.block_code, entry.task_number) for entry in entries] == [('1', '1'), ('1', '2'), ('2', '1')]
    assert entries[0].answer_text == 'R_A = 5 кН'
    assert entries[1].answer_text == 'M(x) = ql^2 / 8'
    assert entries[2].page_start == 2
    assert issues == []


def test_parse_answers_for_source_matches_existing_tasks_via_fake_bundle(tmp_path, monkeypatch):
    db_path = tmp_path / 'variants.sqlite3'
    storage_root = tmp_path / 'storage'
    tasks_rel = 'extracted/eg-bank/abc/tasks_15.pdf'
    answers_rel = 'extracted/eg-bank/abc/answers_15.pdf'

    tasks_path = storage_root / tasks_rel
    answers_path = storage_root / answers_rel
    tasks_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_path.write_bytes(b'%PDF-1.4\n%fake tasks\n')
    answers_path.write_bytes(b'%PDF-1.4\n%fake answers\n')

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
            (set_id, tasks_rel, 'tasks_pdf', 'tasks-sha', tasks_path.stat().st_size, 0, 'parsed_tasks', json.dumps({'storage_root': str(storage_root)})),
        )
        tasks_source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', (tasks_rel,)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (set_id, answers_rel, 'answers_pdf', 'answers-sha', answers_path.stat().st_size, 0, 'pending', json.dumps({'storage_root': str(storage_root)})),
        )
        answers_source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', (answers_rel,)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO task_blocks (source_file_id, block_code, ordinal, title, page_start, page_end, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (tasks_source_file_id, '1', 1, 'Статика', 1, 1, '{}'),
        )
        block_id = conn.execute('SELECT id FROM task_blocks WHERE source_file_id = ?', (tasks_source_file_id,)).fetchone()[0]
        conn.executemany(
            '''
            INSERT INTO tasks (block_id, task_number, ordinal, title, prompt_text, page_start, page_end, fingerprint, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            [
                (block_id, '1', 1, '', 'Prompt 1', 1, 1, 'fp-1', '{}'),
                (block_id, '2', 2, '', 'Prompt 2', 1, 1, 'fp-2', '{}'),
            ],
        )
    conn.close()

    def fake_bundle(_pdf_path):
        return AnswerPdfBundle(
            pages=[
                AnswerPage(page_number=1, text='Задание 1. Статика\n1) 42\n2) 84\n'),
            ],
            parser_backends={'page_text': 'fake'},
        )

    monkeypatch.setattr('pdf_variant_bot.answers_parser._load_pdf_bundle', fake_bundle)

    summary = parse_answers_for_source(db_path, source_file_id=answers_source_file_id)

    assert summary['entries'] == 2
    assert summary['matched'] == 2
    assert summary['unmatched'] == 0
    assert summary['ambiguous'] == 0

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT task_id, block_code, task_number, answer_text, confidence FROM answers ORDER BY task_number'
    ).fetchall()
    assert rows == [
        (1, '1', '1', '42', 1.0),
        (2, '1', '2', '84', 1.0),
    ]
    status = conn.execute('SELECT status FROM source_files WHERE id = ?', (answers_source_file_id,)).fetchone()[0]
    assert status == 'parsed_answers'
    conn.close()


def test_parse_answers_for_source_logs_ambiguous_and_unmatched_cases(tmp_path, monkeypatch):
    db_path = tmp_path / 'variants.sqlite3'
    storage_root = tmp_path / 'storage'
    answers_rel = 'extracted/ug-bank/abc/answers_20.pdf'
    answers_path = storage_root / answers_rel
    answers_path.parent.mkdir(parents=True, exist_ok=True)
    answers_path.write_bytes(b'%PDF-1.4\n%fake answers\n')

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
            (set_id, answers_rel, 'answers_pdf', 'answers-sha', answers_path.stat().st_size, 0, 'pending', json.dumps({'storage_root': str(storage_root)})),
        )
        answers_source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', (answers_rel,)).fetchone()[0]

        for rel_path, block_title in [('extracted/ug-bank/abc/tasks_a.pdf', 'Статика A'), ('extracted/ug-bank/abc/tasks_b.pdf', 'Статика B')]:
            conn.execute(
                '''
                INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (set_id, rel_path, 'tasks_pdf', rel_path, 1, 1, 'parsed_tasks', json.dumps({'storage_root': str(storage_root)})),
            )
            source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', (rel_path,)).fetchone()[0]
            conn.execute(
                '''
                INSERT INTO task_blocks (source_file_id, block_code, ordinal, title, page_start, page_end, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (source_file_id, '1', 1, block_title, 1, 1, '{}'),
            )
            block_id = conn.execute('SELECT id FROM task_blocks WHERE source_file_id = ?', (source_file_id,)).fetchone()[0]
            conn.execute(
                '''
                INSERT INTO tasks (block_id, task_number, ordinal, title, prompt_text, page_start, page_end, fingerprint, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (block_id, '1', 1, '', f'Prompt for {block_title}', 1, 1, f'fp-{block_title}', '{}'),
            )
    conn.close()

    def fake_bundle(_pdf_path):
        return AnswerPdfBundle(
            pages=[
                AnswerPage(page_number=1, text='Задание 1. Статика\n1) ambiguous\n2) missing\n'),
            ],
            parser_backends={'page_text': 'fake'},
        )

    monkeypatch.setattr('pdf_variant_bot.answers_parser._load_pdf_bundle', fake_bundle)

    summary = parse_answers_for_source(db_path, source_file_id=answers_source_file_id)

    assert summary['matched'] == 0
    assert summary['ambiguous'] == 1
    assert summary['unmatched'] == 1

    conn = sqlite3.connect(db_path)
    answers = conn.execute(
        'SELECT task_id, block_code, task_number, answer_text, confidence FROM answers ORDER BY task_number'
    ).fetchall()
    assert answers == [
        (None, '1', '1', 'ambiguous', 0.0),
        (None, '1', '2', 'missing', 0.0),
    ]
    issue_types = [
        row[0]
        for row in conn.execute(
            'SELECT issue_type FROM import_issues WHERE source_file_id = ? ORDER BY id',
            (answers_source_file_id,),
        ).fetchall()
    ]
    assert issue_types == ['ambiguous_answer_match', 'unmatched_answer_task']
    conn.close()

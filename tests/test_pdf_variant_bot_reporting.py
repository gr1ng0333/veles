import json

from pdf_variant_bot.cli import main
from pdf_variant_bot.db import initialize_database


def seed_reporting_fixture(db_path):
    conn = initialize_database(db_path)
    with conn:
        conn.execute(
            "INSERT INTO source_sets (slug, title, source_kind, notes) VALUES (?, ?, ?, ?)",
            ('eg-bank', 'EG Bank', 'archive', 'fixture'),
        )
        set_id = conn.execute("SELECT id FROM source_sets WHERE slug = ?", ('eg-bank',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (set_id, 'archives/eg-bank/source.zip', 'archive', 'archive-sha', 10, 0, 'stored', '{}'),
        )
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (set_id, 'extracted/eg-bank/tasks_15.pdf', 'tasks_pdf', 'tasks-sha', 100, 2, 'parsed_tasks', '{}'),
        )
        tasks_source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', ('extracted/eg-bank/tasks_15.pdf',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO source_files (set_id, relative_path, file_kind, sha256, size_bytes, page_count, status, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (set_id, 'extracted/eg-bank/answers_15.pdf', 'answers_pdf', 'answers-sha', 90, 1, 'parsed_answers', '{}'),
        )
        answers_source_file_id = conn.execute('SELECT id FROM source_files WHERE relative_path = ?', ('extracted/eg-bank/answers_15.pdf',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO task_blocks (source_file_id, block_code, ordinal, title, page_start, page_end, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (tasks_source_file_id, '1', 1, 'Статика', 1, 2, '{}'),
        )
        block_id = conn.execute('SELECT id FROM task_blocks WHERE source_file_id = ?', (tasks_source_file_id,)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO tasks (block_id, task_number, ordinal, title, prompt_text, page_start, page_end, fingerprint, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (block_id, '1', 1, 'Найти реакции', 'Найти реакции опор', 1, 1, 'fp-1', '{}'),
        )
        task_id = conn.execute('SELECT id FROM tasks WHERE block_id = ? AND task_number = ?', (block_id, '1')).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO tasks (block_id, task_number, ordinal, title, prompt_text, page_start, page_end, fingerprint, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (block_id, '2', 2, 'Эпюра моментов', 'Построить эпюру моментов', 2, 2, 'fp-2', '{}'),
        )
        conn.execute(
            '''
            INSERT INTO task_assets (task_id, source_file_id, page_number, asset_index, asset_kind, relative_path, mime_type, sha256, width, height, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (task_id, tasks_source_file_id, 1, 1, 'image', 'assets/page-001-image-01.png', 'image/png', 'asset-sha', 100, 80, '{}'),
        )
        conn.execute(
            '''
            INSERT INTO answers (task_id, source_file_id, block_code, task_number, answer_text, page_start, page_end, confidence, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (task_id, answers_source_file_id, '1', '1', 'R_A = 5 кН', 1, 1, 1.0, '{}'),
        )
        conn.execute(
            '''
            INSERT INTO answers (task_id, source_file_id, block_code, task_number, answer_text, page_start, page_end, confidence, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (None, answers_source_file_id, '1', '2', 'M(x) = ql^2 / 8', 1, 1, 0.0, '{}'),
        )
        conn.execute(
            '''
            INSERT INTO import_runs (import_kind, source_path, status, stats_json, notes)
            VALUES (?, ?, ?, ?, ?)
            ''',
            ('answers_parse', 'fixture://answers', 'completed', '{}', 'fixture'),
        )
        answers_run_id = conn.execute('SELECT id FROM import_runs WHERE source_path = ?', ('fixture://answers',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO import_runs (import_kind, source_path, status, stats_json, notes)
            VALUES (?, ?, ?, ?, ?)
            ''',
            ('tasks_parse', 'fixture://tasks', 'completed', '{}', 'fixture'),
        )
        tasks_run_id = conn.execute('SELECT id FROM import_runs WHERE source_path = ?', ('fixture://tasks',)).fetchone()[0]
        conn.execute(
            '''
            INSERT INTO import_issues (run_id, source_file_id, task_id, severity, issue_type, message, context_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (answers_run_id, answers_source_file_id, None, 'warning', 'unmatched_answer', 'No task match for 1/2', json.dumps({'block_code': '1', 'task_number': '2'})),
        )
        conn.execute(
            '''
            INSERT INTO import_issues (run_id, source_file_id, task_id, severity, issue_type, message, context_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (tasks_run_id, tasks_source_file_id, task_id, 'info', 'asset_attached', 'Attached page asset', json.dumps({'asset_path': 'assets/page-001-image-01.png'})),
        )
    conn.close()


def test_cli_inspect_db_reports_totals_and_set_headline(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    seed_reporting_fixture(db_path)

    assert main(['inspect-db', str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary['totals']['source_sets'] == 1
    assert summary['totals']['tasks'] == 2
    assert summary['totals']['task_assets'] == 1
    assert summary['totals']['answers'] == 2
    assert summary['source_sets'][0]['slug'] == 'eg-bank'
    assert summary['source_sets'][0]['answers']['unmatched'] == 1


def test_cli_inspect_set_reports_file_breakdown(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    seed_reporting_fixture(db_path)

    assert main(['inspect-set', str(db_path), '--slug', 'eg-bank']) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary['source_set']['slug'] == 'eg-bank'
    assert summary['counts']['tasks'] == 2
    assert summary['counts']['task_assets'] == 1
    assert summary['answers'] == {'matched': 1, 'total': 2, 'unmatched': 1}
    files = {item['relative_path']: item for item in summary['files']}
    assert files['extracted/eg-bank/tasks_15.pdf']['task_blocks'] == 1
    assert files['extracted/eg-bank/tasks_15.pdf']['tasks'] == 2
    assert files['extracted/eg-bank/answers_15.pdf']['answers'] == 2
    assert files['extracted/eg-bank/answers_15.pdf']['unmatched_answers'] == 1


def test_cli_list_issues_supports_filters(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    seed_reporting_fixture(db_path)

    assert main(['list-issues', str(db_path), '--slug', 'eg-bank', '--severity', 'warning', '--limit', '5']) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary['filters'] == {'limit': 5, 'set_slug': 'eg-bank', 'severity': 'warning'}
    assert len(summary['issues']) == 1
    assert summary['issues'][0]['issue_type'] == 'unmatched_answer'
    assert summary['issues'][0]['context']['task_number'] == '2'


def test_cli_validate_set_surfaces_warnings_without_failing_fixture(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    seed_reporting_fixture(db_path)

    assert main(['validate-set', str(db_path), '--slug', 'eg-bank']) == 0
    summary = json.loads(capsys.readouterr().out)

    assert summary['ok'] is True
    checks = {item['name']: item for item in summary['checks']}
    assert checks['tasks_pdf_parsed']['status'] == 'pass'
    assert checks['answers_pdf_parsed']['status'] == 'pass'
    assert checks['answers_unmatched']['status'] == 'warn'
    assert checks['import_issues_present']['status'] == 'warn'

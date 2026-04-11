import json

from pdf_variant_bot.cli import main
from pdf_variant_bot.db import DEFAULT_SCHEMA_VERSION, EXPECTED_TABLES, get_schema_version, initialize_database, list_user_tables



def test_initialize_database_creates_expected_tables(tmp_path):
    conn = initialize_database(tmp_path / 'variants.sqlite3')
    tables = set(list_user_tables(conn))
    assert set(EXPECTED_TABLES).issubset(tables)
    assert get_schema_version(conn) == DEFAULT_SCHEMA_VERSION
    conn.close()



def test_initialize_database_is_idempotent_and_enables_foreign_keys(tmp_path):
    db_path = tmp_path / 'variants.sqlite3'
    first = initialize_database(db_path)
    first.close()

    second = initialize_database(db_path)
    assert second.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert get_schema_version(second) == DEFAULT_SCHEMA_VERSION
    second.close()



def test_cli_init_db_prints_machine_readable_summary(tmp_path, capsys):
    db_path = tmp_path / 'variants.sqlite3'
    assert main(['init-db', str(db_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['schema_version'] == DEFAULT_SCHEMA_VERSION
    assert summary['db_path'] == str(db_path)
    assert summary['table_count'] >= len(EXPECTED_TABLES)

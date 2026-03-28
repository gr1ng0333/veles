"""Tests for run_shell cmd string recovery, including Python-list-style strings."""
from ouroboros.tools.shell import _try_parse_python_list_string


def test_unquoted_python_list():
    # The crash case: [Errno 2] No such file or directory: '[grep,'
    result = _try_parse_python_list_string('[grep, -n, pattern, file.py]')
    assert result == ['grep', '-n', 'pattern', 'file.py']


def test_quoted_json_list():
    # Proper JSON list — should also be parsed correctly
    result = _try_parse_python_list_string("['git', 'add', '-A']")
    assert result == ['git', 'add', '-A']


def test_double_quoted_json_list():
    result = _try_parse_python_list_string('["bash", "-c", "echo hi"]')
    assert result == ['bash', '-c', 'echo hi']


def test_plain_string_returns_none():
    # Non-list strings: shlex will handle these, not this function
    assert _try_parse_python_list_string('git log --oneline') is None


def test_empty_list_returns_none():
    assert _try_parse_python_list_string('[]') is None


def test_mixed_unquoted_flags():
    result = _try_parse_python_list_string('[python3, -m, pytest, tests/, -v]')
    assert result == ['python3', '-m', 'pytest', 'tests/', '-v']


def test_single_element_list():
    result = _try_parse_python_list_string('[ls]')
    assert result == ['ls']

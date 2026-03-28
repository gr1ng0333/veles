"""Unit tests for code_search tool."""
import json
import sys
sys.path.insert(0, '/opt/veles')

from ouroboros.tools.code_search import _code_search, get_tools

class FakeCtx:
    pass

ctx = FakeCtx()

def test_get_tools_registers_one_tool():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == 'code_search'

def test_literal_search_finds_matches():
    r = _code_search(ctx, 'def get_tools', path_filter='ouroboros/tools', max_results=30)
    data = json.loads(r)
    assert data['total_matches'] > 0
    assert all('line' in m and 'file' in m and 'text' in m for m in data['matches'])

def test_symbol_mode_finds_functions():
    r = _code_search(ctx, 'log_query', mode='symbol')
    data = json.loads(r)
    assert data['total_matches'] >= 1
    kinds = {m['kind'] for m in data['matches']}
    assert kinds <= {'def', 'class', 'async_def'}

def test_regex_mode():
    r = _code_search(ctx, r'class \w+Error', mode='regex', path_filter='.py')
    data = json.loads(r)
    assert data['total_matches'] >= 1
    for m in data['matches']:
        assert 'Error' in m['text'] or 'class' in m['text'].lower()

def test_case_insensitive_default():
    r_lower = _code_search(ctx, 'evolution', path_filter='supervisor', max_results=50)
    r_upper = _code_search(ctx, 'EVOLUTION', path_filter='supervisor', max_results=50)
    dl = json.loads(r_lower)
    du = json.loads(r_upper)
    # Both should find same count (case-insensitive by default)
    assert dl['total_matches'] == du['total_matches']

def test_context_lines():
    r = _code_search(ctx, 'def _code_search', context_lines=2)
    data = json.loads(r)
    assert data['total_matches'] >= 1
    m = data['matches'][0]
    assert 'before' in m or 'after' in m  # at least one context side

def test_path_filter_restricts():
    r_all = _code_search(ctx, 'import json', max_results=200)
    r_filtered = _code_search(ctx, 'import json', path_filter='supervisor', max_results=200)
    dall = json.loads(r_all)
    dfilt = json.loads(r_filtered)
    assert dfilt['total_matches'] <= dall['total_matches']

def test_invalid_regex_returns_error():
    r = _code_search(ctx, '[invalid(', mode='regex')
    data = json.loads(r)
    assert 'error' in data

def test_empty_pattern_returns_error():
    r = _code_search(ctx, '')
    data = json.loads(r)
    assert 'error' in data

def test_ext_filter():
    r = _code_search(ctx, 'import', ext_filter='.py', max_results=100)
    data = json.loads(r)
    for m in data['matches']:
        assert m['file'].endswith('.py')

if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    passed = 0
    for t in tests:
        try:
            t()
            print(f'  PASS  {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  FAIL  {t.__name__}: {e}')
    print(f'\n{passed}/{len(tests)} tests passed')
    sys.exit(0 if passed == len(tests) else 1)

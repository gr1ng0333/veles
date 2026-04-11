import json
import pathlib

from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.tools.ssh_targets import _ssh_target_register
from ouroboros.tools.xui_panel import _normalize_panel_base_url, _xui_panel_status


def _schema_names(registry: ToolRegistry) -> set[str]:
    names: set[str] = set()
    for schema in registry.schemas():
        fn = schema.get('function') or {}
        name = fn.get('name') or schema.get('name')
        if name:
            names.add(name)
    return names


def _ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


def test_xui_panel_tool_registered():
    tmp = pathlib.Path('/tmp')
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    assert 'xui_panel_status' in _schema_names(registry)


def test_normalize_panel_base_url_appends_trailing_slash():
    assert _normalize_panel_base_url('https://panel.example.com/base') == 'https://panel.example.com/base/'


def test_xui_panel_status_success(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='panel-box',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='ssh-secret',
        panel_type='3x-ui',
        panel_url='https://panel.example.com/secret/',
        panel_username='admin',
        panel_password='adminpass',
    )

    calls = []

    class FakeHeaders(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    class FakeResponse:
        def __init__(self, payload, content_type='application/json'):
            self._payload = payload
            self.headers = FakeHeaders({'Content-Type': content_type})
            self._body = json.dumps(payload).encode('utf-8')

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self):
            return self._body

    class FakeOpener:
        def open(self, request, timeout=None):
            method = request.get_method()
            url = request.full_url
            body = request.data.decode('utf-8') if request.data else ''
            calls.append((method, url, body))
            if url.endswith('/login'):
                return FakeResponse({'success': True, 'msg': 'ok'})
            if url.endswith('/api/server/status'):
                return FakeResponse({'cpu': 14, 'mem': {'current': 42}, 'xray': {'state': 'running'}})
            if url.endswith('/api/inbounds/list'):
                return FakeResponse([
                    {'id': 1, 'remark': 'RU reality', 'protocol': 'vless', 'port': 443, 'enable': True, 'up': 10, 'down': 20, 'total': 100, 'settings': json.dumps({'clients': [{'id': 'a'}, {'id': 'b'}]})},
                    {'id': 2, 'remark': 'WS backup', 'protocol': 'vmess', 'port': 8443, 'enable': False, 'up': 0, 'down': 0, 'total': 0, 'settings': json.dumps({'clients': [{'id': 'c'}]})},
                ])
            raise AssertionError(f'unexpected url: {url}')

    monkeypatch.setattr('ouroboros.tools.xui_panel._make_opener', lambda: FakeOpener())
    payload = json.loads(_xui_panel_status(ctx, 'panel-box'))

    assert payload['status'] == 'ok'
    assert payload['target']['panel_url'] == 'https://panel.example.com/secret/'
    assert payload['summary'] == {'inbounds_total': 2, 'inbounds_enabled': 1, 'clients_total': 3}
    assert payload['inbounds'][0]['client_count'] == 2
    assert calls[0][0] == 'POST'
    assert calls[0][1] == 'https://panel.example.com/login'
    assert 'username=admin' in calls[0][2]
    assert calls[1][1].endswith('/api/server/status')
    assert calls[2][1].endswith('/api/inbounds/list')


def test_xui_panel_status_requires_credentials(tmp_path):
    ctx = _ctx(tmp_path)
    _ssh_target_register(
        ctx,
        alias='panel-box',
        host='203.0.113.10',
        user='root',
        auth_mode='password',
        password='ssh-secret',
        panel_type='3x-ui',
        panel_url='https://panel.example.com/secret/',
    )

    try:
        _xui_panel_status(ctx, 'panel-box')
    except Exception as exc:
        assert 'panel credentials' in str(exc)
    else:
        raise AssertionError('expected missing panel credentials error')

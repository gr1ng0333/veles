from __future__ import annotations

import json
from http.cookiejar import CookieJar
from typing import Any, Dict, List
from urllib.parse import urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import _get_target_record


class XuiPanelError(RuntimeError):
    pass


def _tool_entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler, is_code_tool: bool = False) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
        timeout_sec=120,
    )


def _normalize_panel_base_url(url: str) -> str:
    value = str(url or '').strip()
    if not value:
        raise XuiPanelError('panel_url is not configured for this target')
    if not value.endswith('/'):
        value += '/'
    return value


def _panel_credentials(record: Dict[str, Any]) -> tuple[str, str]:
    username = str(record.get('panel_username') or '').strip()
    password = str(record.get('panel_password') or '')
    if not username or not password:
        raise XuiPanelError('panel credentials are not configured for this target')
    return username, password


def _make_opener():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def _request_json(opener, method: str, url: str, *, payload: Dict[str, Any] | None = None, timeout: int = 20) -> Any:
    data = None
    headers = {'User-Agent': 'Veles/7.x xui-panel-client'}
    if payload is not None:
        data = urlencode(payload).encode('utf-8')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
            content_type = response.headers.get('Content-Type', '')
    except HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise XuiPanelError(f'3x-ui request failed: HTTP {exc.code} for {url}: {body[:200]}') from exc
    except URLError as exc:
        raise XuiPanelError(f'3x-ui request failed for {url}: {exc.reason}') from exc

    if 'application/json' not in content_type and not body.strip().startswith(('{', '[')):
        raise XuiPanelError(f'expected JSON from 3x-ui API, got {content_type or "unknown content type"}')
    try:
        return json.loads(body)
    except ValueError as exc:
        raise XuiPanelError('3x-ui API returned invalid JSON') from exc


def _login(opener, base_url: str, username: str, password: str, timeout_sec: int) -> None:
    login_url = urljoin(base_url, '../login')
    data = _request_json(opener, 'POST', login_url, payload={'username': username, 'password': password}, timeout=timeout_sec)
    if isinstance(data, dict):
        if data.get('success') is False:
            raise XuiPanelError(str(data.get('msg') or '3x-ui login failed'))
        return
    raise XuiPanelError('unexpected 3x-ui login response format')


def _fetch_panel_status(opener, base_url: str, timeout_sec: int) -> Dict[str, Any]:
    data = _request_json(opener, 'GET', urljoin(base_url, 'api/server/status'), timeout=timeout_sec)
    if not isinstance(data, dict):
        raise XuiPanelError('unexpected 3x-ui server status response')
    return data


def _fetch_inbounds(opener, base_url: str, timeout_sec: int) -> List[Dict[str, Any]]:
    data = _request_json(opener, 'GET', urljoin(base_url, 'api/inbounds/list'), timeout=timeout_sec)
    if not isinstance(data, list):
        raise XuiPanelError('unexpected 3x-ui inbounds response')
    return [item for item in data if isinstance(item, dict)]


def _client_count(inbound: Dict[str, Any]) -> int:
    settings = inbound.get('settings')
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            settings = {}
    if not isinstance(settings, dict):
        return 0
    clients = settings.get('clients')
    return len(clients) if isinstance(clients, list) else 0


def _summarize_inbound(inbound: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'id': inbound.get('id'),
        'remark': inbound.get('remark') or '',
        'protocol': inbound.get('protocol') or '',
        'port': inbound.get('port'),
        'enable': bool(inbound.get('enable', False)),
        'up': inbound.get('up'),
        'down': inbound.get('down'),
        'total': inbound.get('total'),
        'expiry_time': inbound.get('expiryTime') or inbound.get('expiry_time'),
        'listen': inbound.get('listen') or '',
        'tag': inbound.get('tag') or '',
        'client_count': _client_count(inbound),
    }


def _xui_panel_status(ctx: ToolContext, alias: str, *, timeout_sec: int = 20) -> str:
    record = _get_target_record(ctx, alias)
    base_url = _normalize_panel_base_url(record.get('panel_url', ''))
    username, password = _panel_credentials(record)

    opener = _make_opener()
    _login(opener, base_url, username, password, timeout_sec)
    status = _fetch_panel_status(opener, base_url, timeout_sec)
    inbounds = _fetch_inbounds(opener, base_url, timeout_sec)

    summarized_inbounds = [_summarize_inbound(item) for item in inbounds]
    enabled = sum(1 for item in summarized_inbounds if item['enable'])
    total_clients = sum(item['client_count'] for item in summarized_inbounds)

    payload = {
        'status': 'ok',
        'target': {
            'alias': record['alias'],
            'label': record.get('label') or record['alias'],
            'panel_type': record.get('panel_type') or '',
            'panel_url': base_url,
            'panel_username': username,
        },
        'panel_status': status,
        'inbounds': summarized_inbounds,
        'summary': {
            'inbounds_total': len(summarized_inbounds),
            'inbounds_enabled': enabled,
            'clients_total': total_clients,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            'xui_panel_status',
            'Login to a configured 3x-ui panel over HTTP API and return panel status plus inbound summary.',
            {
                'alias': {'type': 'string'},
                'timeout_sec': {'type': 'integer', 'default': 20},
            },
            ['alias'],
            _xui_panel_status,
            is_code_tool=True,
        )
    ]

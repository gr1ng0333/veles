import json
import pathlib

from scripts import fleet_monitor


def _args(tmp_path: pathlib.Path, *extra: str):
    return fleet_monitor.build_parser().parse_args([
        '--repo-dir', str(tmp_path),
        '--drive-root', str(tmp_path),
        *extra,
    ])


def test_run_monitor_writes_snapshot_and_history(monkeypatch, tmp_path):
    def fake_fleet_health(ctx, aliases=None, tags=None, include_panel=True, include_xray=True, max_workers=None):
        assert ctx.repo_dir == tmp_path
        assert ctx.drive_root == tmp_path
        assert aliases == ['edge-1']
        assert tags == ['vpn', 'ru']
        assert include_panel is True
        assert include_xray is True
        assert max_workers == 3
        return json.dumps({
            'status': 'ok',
            'summary': {
                'overall_verdict': 'warn',
                'matched_targets': 1,
                'by_verdict': {'ok': 0, 'warn': 1, 'critical': 0},
            },
            'targets': [{'alias': 'edge-1', 'verdict': 'warn'}],
        })

    monkeypatch.setattr(fleet_monitor, 'fleet_health', fake_fleet_health)
    args = _args(tmp_path, '--alias', 'edge-1', '--tag', 'vpn,ru', '--max-workers', '3')
    report, exit_code = fleet_monitor.run_monitor(args)

    assert exit_code == 1
    assert report['summary']['overall_verdict'] == 'warn'
    assert report['artifacts']['snapshot_path'].endswith('state/fleet_health_latest.json')
    assert report['artifacts']['history_path'].endswith('logs/fleet_health.jsonl')

    snapshot = json.loads((tmp_path / 'state' / 'fleet_health_latest.json').read_text(encoding='utf-8'))
    assert snapshot['filters']['aliases'] == ['edge-1']
    assert snapshot['filters']['tags'] == ['vpn', 'ru']
    assert snapshot['summary']['overall_verdict'] == 'warn'

    history_lines = (tmp_path / 'logs' / 'fleet_health.jsonl').read_text(encoding='utf-8').strip().splitlines()
    assert len(history_lines) == 1
    assert json.loads(history_lines[0])['summary']['matched_targets'] == 1


def test_run_monitor_resolves_relative_custom_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fleet_monitor,
        'fleet_health',
        lambda *args, **kwargs: json.dumps({
            'status': 'ok',
            'summary': {
                'overall_verdict': 'ok',
                'matched_targets': 0,
                'by_verdict': {'ok': 0, 'warn': 0, 'critical': 0},
            },
            'targets': [],
        }),
    )
    args = _args(
        tmp_path,
        '--no-panel',
        '--no-xray',
        '--output-path', 'reports/latest.json',
        '--history-path', 'reports/history.jsonl',
    )
    report, exit_code = fleet_monitor.run_monitor(args)

    assert exit_code == 0
    assert report['filters']['include_panel'] is False
    assert report['filters']['include_xray'] is False
    assert (tmp_path / 'reports' / 'latest.json').exists()
    assert (tmp_path / 'reports' / 'history.jsonl').exists()


def test_summary_line_and_exit_codes():
    payload = {
        'summary': {
            'overall_verdict': 'critical',
            'matched_targets': 4,
            'by_verdict': {'ok': 1, 'warn': 1, 'critical': 2},
        }
    }
    assert fleet_monitor._summary_line(payload) == 'critical matched=4 ok=1 warn=1 critical=2'
    assert fleet_monitor._exit_code_for_verdict('ok') == 0
    assert fleet_monitor._exit_code_for_verdict('warn') == 1
    assert fleet_monitor._exit_code_for_verdict('critical') == 2
    assert fleet_monitor._exit_code_for_verdict('broken') == 3

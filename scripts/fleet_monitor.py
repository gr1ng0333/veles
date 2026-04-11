#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import socket
import sys
from typing import Any, Iterable

_THIS_FILE = pathlib.Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ouroboros.tools.fleet_health import fleet_health
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import append_jsonl, utc_now_iso, write_text

_DEFAULT_REPO_DIR = pathlib.Path(os.environ.get('VELES_REPO_DIR', '/opt/veles'))
_DEFAULT_DRIVE_ROOT = pathlib.Path(os.environ.get('VELES_DRIVE_ROOT', '/opt/veles-data'))
_DEFAULT_SNAPSHOT_REL = pathlib.Path('state/fleet_health_latest.json')
_DEFAULT_HISTORY_REL = pathlib.Path('logs/fleet_health.jsonl')


def _split_csv(items: Iterable[str] | None) -> list[str]:
    values: list[str] = []
    for item in items or []:
        for chunk in str(item or '').split(','):
            text = chunk.strip()
            if text:
                values.append(text)
    return values


def _resolve_output_path(drive_root: pathlib.Path, raw_path: str, default_rel: pathlib.Path) -> pathlib.Path:
    if not raw_path:
        return drive_root / default_rel
    path = pathlib.Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return drive_root / path


def _build_context(repo_dir: pathlib.Path, drive_root: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=repo_dir, drive_root=drive_root)


def _exit_code_for_verdict(verdict: str) -> int:
    normalized = str(verdict or '').strip().lower()
    if normalized == 'ok':
        return 0
    if normalized == 'warn':
        return 1
    if normalized == 'critical':
        return 2
    return 3


def _summary_line(payload: dict[str, Any]) -> str:
    summary = payload.get('summary') or {}
    by_verdict = summary.get('by_verdict') or {}
    return (
        f"{summary.get('overall_verdict', 'unknown')} "
        f"matched={summary.get('matched_targets', 0)} "
        f"ok={by_verdict.get('ok', 0)} "
        f"warn={by_verdict.get('warn', 0)} "
        f"critical={by_verdict.get('critical', 0)}"
    )


def run_monitor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo_dir = pathlib.Path(args.repo_dir).expanduser()
    drive_root = pathlib.Path(args.drive_root).expanduser()
    aliases = _split_csv(args.aliases)
    tags = _split_csv(args.tags)
    ctx = _build_context(repo_dir=repo_dir, drive_root=drive_root)
    raw_payload = fleet_health(
        ctx,
        aliases=aliases or None,
        tags=tags or None,
        include_panel=not args.no_panel,
        include_xray=not args.no_xray,
        max_workers=args.max_workers,
    )
    payload = json.loads(raw_payload)
    summary = payload.get('summary') or {}
    verdict = str(summary.get('overall_verdict') or payload.get('status') or 'unknown')
    exit_code = _exit_code_for_verdict(verdict)

    report = {
        'generated_at': utc_now_iso(),
        'host': socket.gethostname(),
        'filters': {
            'aliases': aliases,
            'tags': tags,
            'include_panel': not args.no_panel,
            'include_xray': not args.no_xray,
            'max_workers': args.max_workers,
        },
        **payload,
        'exit_code': exit_code,
    }

    if not args.no_write:
        snapshot_path = _resolve_output_path(drive_root, args.output_path, _DEFAULT_SNAPSHOT_REL)
        history_path = _resolve_output_path(drive_root, args.history_path, _DEFAULT_HISTORY_REL)
        report['artifacts'] = {
            'snapshot_path': str(snapshot_path),
            'history_path': str(history_path),
        }
        write_text(snapshot_path, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        append_jsonl(history_path, report)

    return report, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Standalone fleet monitor for SSH / Xray / 3x-ui targets. Writes structured JSON and returns a verdict exit code.'
    )
    parser.add_argument('--repo-dir', default=str(_DEFAULT_REPO_DIR), help='Path to Veles repository (default: /opt/veles or VELES_REPO_DIR).')
    parser.add_argument('--drive-root', default=str(_DEFAULT_DRIVE_ROOT), help='Path to drive root (default: /opt/veles-data or VELES_DRIVE_ROOT).')
    parser.add_argument('--alias', dest='aliases', action='append', default=[], help='Target alias to include (repeatable; comma-separated values are also accepted).')
    parser.add_argument('--tag', dest='tags', action='append', default=[], help='Target tag to require (repeatable; comma-separated values are also accepted).')
    parser.add_argument('--max-workers', type=int, default=6, help='Parallel workers for fleet polling (default: 6).')
    parser.add_argument('--no-panel', action='store_true', help='Skip native 3x-ui panel checks.')
    parser.add_argument('--no-xray', action='store_true', help='Skip Xray diagnostics.')
    parser.add_argument('--no-write', action='store_true', help='Do not write snapshot/history files; stdout only.')
    parser.add_argument('--output-path', default='', help='Custom snapshot JSON path. Relative paths are resolved under drive root.')
    parser.add_argument('--history-path', default='', help='Custom JSONL history path. Relative paths are resolved under drive root.')
    parser.add_argument('--stdout-format', choices=('pretty', 'json', 'summary'), default='pretty', help='How to print the result to stdout.')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report, exit_code = run_monitor(args)
    if args.stdout_format == 'summary':
        print(_summary_line(report))
    elif args.stdout_format == 'json':
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())

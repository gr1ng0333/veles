from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import subprocess
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.ssh_targets import (
    _SESSION_CACHE,
    SshConnectionError,
    _base_ssh_command,
    _bootstrap_session,
    _get_target_record,
    _load_registry,
    _public_target_view,
    _run_ssh_probe,
    _save_registry,
)

_KEY_DIR_REL = "state/ssh_keys"
_DEFAULT_DEPLOY_TIMEOUT_SEC = 25
_MAX_DEPLOY_TIMEOUT_SEC = 120
_ALLOWED_KEY_TYPES = {"ed25519", "rsa"}
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SshKeyManagementError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def _tool_entry(name: str, description: str, properties: Dict[str, Any], required: List[str], handler, is_code_tool: bool = False) -> ToolEntry:
    return ToolEntry(
        name=name,
        schema={
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        handler=handler,
        is_code_tool=is_code_tool,
        timeout_sec=_MAX_DEPLOY_TIMEOUT_SEC + 10,
    )


def _key_dir(ctx: ToolContext) -> pathlib.Path:
    path = ctx.drive_path(_KEY_DIR_REL)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _normalize_key_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise SshKeyManagementError("invalid_key_name", "key_name must not be empty")
    if not _KEY_NAME_RE.fullmatch(text):
        raise SshKeyManagementError("invalid_key_name", "key_name may contain only letters, digits, dot, underscore, and hyphen")
    return text


def _normalize_key_type(value: Any) -> str:
    text = str(value or "ed25519").strip().lower()
    if text not in _ALLOWED_KEY_TYPES:
        raise SshKeyManagementError("invalid_key_type", "key_type must be one of: ed25519, rsa")
    return text


def _normalize_timeout(value: Any) -> int:
    try:
        timeout = int(value if value is not None else _DEFAULT_DEPLOY_TIMEOUT_SEC)
    except Exception as exc:
        raise SshKeyManagementError("invalid_timeout", "timeout_sec must be an integer") from exc
    if timeout < 5 or timeout > _MAX_DEPLOY_TIMEOUT_SEC:
        raise SshKeyManagementError("invalid_timeout", f"timeout_sec must be between 5 and {_MAX_DEPLOY_TIMEOUT_SEC}")
    return timeout


def _private_key_path(ctx: ToolContext, key_name: str) -> pathlib.Path:
    return _key_dir(ctx) / key_name


def _public_key_path_from_private(private_key_path: pathlib.Path) -> pathlib.Path:
    return private_key_path.with_name(private_key_path.name + '.pub')


def _fingerprint(public_key_path: pathlib.Path) -> str:
    result = subprocess.run(
        ['ssh-keygen', '-lf', str(public_key_path)],
        cwd='/',
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return ''
    return result.stdout.strip()


def _read_public_key(public_key_path: pathlib.Path) -> str:
    if not public_key_path.exists():
        raise SshKeyManagementError('public_key_missing', f'public key not found: {public_key_path}')
    text = public_key_path.read_text(encoding='utf-8').strip()
    if not text or ' ' not in text:
        raise SshKeyManagementError('invalid_public_key', f'invalid public key file: {public_key_path}')
    return text


def _public_payload_for_key(ctx: ToolContext, key_name: Optional[str], public_key_path: Optional[str]) -> tuple[pathlib.Path, pathlib.Path, str, str]:
    if key_name:
        normalized = _normalize_key_name(key_name)
        private_path = _private_key_path(ctx, normalized)
        pub_path = _public_key_path_from_private(private_path)
        public_key = _read_public_key(pub_path)
        return private_path, pub_path, public_key, normalized
    custom_path = pathlib.Path(str(public_key_path or '').strip()).expanduser()
    if not custom_path.is_absolute():
        custom_path = (ctx.repo_dir / custom_path).resolve()
    public_key = _read_public_key(custom_path)
    guessed_private = custom_path.with_suffix('') if custom_path.suffix == '.pub' else custom_path
    return guessed_private, custom_path, public_key, guessed_private.name


def _run_key_ssh_command(ctx: ToolContext, record: Dict[str, Any], remote_command: str, timeout_sec: int) -> Dict[str, Any]:
    cmd = _base_ssh_command(ctx, record) + [remote_command]
    result = subprocess.run(
        cmd,
        cwd='/',
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return {
        'exit_code': result.returncode,
        'output': (result.stdout or '') + (result.stderr or ''),
        'password_prompt_seen': False,
    }


def _authorized_keys_append_command(public_key: str) -> str:
    quoted = shlex.quote(public_key)
    return (
        'umask 077; '
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
        'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
        f"grep -qxF {quoted} ~/.ssh/authorized_keys || printf '%s\n' {quoted} >> ~/.ssh/authorized_keys"
    )


def ssh_key_generate(ctx: ToolContext, key_name: str, key_type: str = 'ed25519', comment: str = '', overwrite: bool = False, passphrase: str = '') -> str:
    try:
        normalized_name = _normalize_key_name(key_name)
        normalized_type = _normalize_key_type(key_type)
        private_path = _private_key_path(ctx, normalized_name)
        public_path = _public_key_path_from_private(private_path)
        if private_path.exists() and not overwrite:
            raise SshKeyManagementError('key_exists', f'key already exists: {normalized_name}')
        if overwrite:
            for path in (private_path, public_path):
                if path.exists():
                    path.unlink()
        comment_value = str(comment or normalized_name).strip() or normalized_name
        command = ['ssh-keygen', '-t', normalized_type, '-f', str(private_path), '-N', str(passphrase or ''), '-C', comment_value]
        if normalized_type == 'rsa':
            command.extend(['-b', '4096'])
        result = subprocess.run(command, cwd='/', capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            raise SshKeyManagementError('ssh_keygen_failed', (result.stderr or result.stdout or 'ssh-keygen failed').strip())
        fingerprint = _fingerprint(public_path)
        payload = {
            'status': 'ok',
            'key': {
                'name': normalized_name,
                'type': normalized_type,
                'comment': comment_value,
                'private_key_path': str(private_path),
                'public_key_path': str(public_path),
                'fingerprint': fingerprint,
            },
        }
        ctx.pending_events.append({'type': 'ssh_key_generate', 'key_name': normalized_name, 'key_type': normalized_type})
        return json.dumps(payload, ensure_ascii=False)
    except SshKeyManagementError as exc:
        ctx.pending_events.append({'type': 'ssh_key_generate', 'status': 'error', 'kind': exc.kind, 'error': exc.message})
        return json.dumps({'status': 'error', 'kind': exc.kind, 'error': exc.message}, ensure_ascii=False)


def ssh_key_list(ctx: ToolContext) -> str:
    key_dir = _key_dir(ctx)
    keys: List[Dict[str, Any]] = []
    for private_path in sorted(p for p in key_dir.iterdir() if p.is_file() and not p.name.endswith('.pub')):
        public_path = _public_key_path_from_private(private_path)
        keys.append({
            'name': private_path.name,
            'private_key_path': str(private_path),
            'public_key_path': str(public_path),
            'public_key_exists': public_path.exists(),
            'fingerprint': _fingerprint(public_path) if public_path.exists() else '',
        })
    return json.dumps({'status': 'ok', 'keys': keys, 'count': len(keys)}, ensure_ascii=False)


def ssh_key_deploy(
    ctx: ToolContext,
    alias: str,
    key_name: str = '',
    public_key_path: str = '',
    password: str = '',
    switch_target_to_key: bool = True,
    keep_password: bool = True,
    timeout_sec: int = _DEFAULT_DEPLOY_TIMEOUT_SEC,
) -> str:
    try:
        record = _get_target_record(ctx, alias)
        private_path, public_path, public_key, resolved_key_name = _public_payload_for_key(ctx, key_name or None, public_key_path or None)
        normalized_timeout = _normalize_timeout(timeout_sec)
        remote_command = _authorized_keys_append_command(public_key)
        secret_password = str(password or record.get('password') or '')

        if secret_password:
            if record.get('auth_mode') != 'password' or record.get('password') != secret_password:
                registry = _load_registry(ctx)
                targets = registry.setdefault('targets', {})
                target_entry = targets.setdefault(record['alias'], {})
                target_entry['password'] = secret_password
                target_entry['auth_mode'] = 'password'
                _save_registry(ctx, registry)
                record = _get_target_record(ctx, alias)
            probe_result = _run_ssh_probe(ctx, record, command=remote_command, timeout=normalized_timeout)
            deploy_result = {
                'exit_code': probe_result.returncode,
                'output': (probe_result.stdout or '') + (probe_result.stderr or ''),
                'password_prompt_seen': True,
            }
        elif record.get('auth_mode') == 'key':
            deploy_result = _run_key_ssh_command(ctx, record, remote_command, normalized_timeout)
        else:
            raise SshKeyManagementError('missing_password', 'password is required to deploy a new ssh key to a password-auth target')

        if deploy_result['exit_code'] != 0:
            raise SshKeyManagementError('remote_command_failed', (deploy_result['output'] or 'remote authorized_keys update failed').strip())

        verification: Dict[str, Any] = {'status': 'skipped'}
        public_view = _public_target_view(record)
        if switch_target_to_key:
            registry = _load_registry(ctx)
            targets = registry.setdefault('targets', {})
            alias_norm = record['alias']
            original_record = dict(targets[alias_norm])
            updated_record = dict(original_record)
            updated_record['auth_mode'] = 'key'
            updated_record['ssh_key_path'] = str(private_path)
            if not keep_password:
                updated_record['password'] = ''
            targets[alias_norm] = updated_record
            _save_registry(ctx, registry)
            _SESSION_CACHE.pop(alias_norm, None)
            try:
                verification = _bootstrap_session(ctx, alias_norm)
            except SshConnectionError as exc:
                targets[alias_norm] = original_record
                _save_registry(ctx, registry)
                _SESSION_CACHE.pop(alias_norm, None)
                raise SshKeyManagementError('verification_failed', f'public key was installed but key auth verification failed: {exc.message}')
            public_view = _public_target_view(targets[alias_norm])

        payload = {
            'status': 'ok',
            'target': public_view,
            'key': {
                'name': resolved_key_name,
                'private_key_path': str(private_path),
                'public_key_path': str(public_path),
                'fingerprint': _fingerprint(public_path),
            },
            'switch_target_to_key': bool(switch_target_to_key),
            'verification': verification,
            'deploy_output': (deploy_result['output'] or '').strip()[-2000:],
        }
        ctx.pending_events.append({
            'type': 'ssh_key_deploy',
            'alias': record['alias'],
            'key_name': resolved_key_name,
            'switch_target_to_key': bool(switch_target_to_key),
        })
        return json.dumps(payload, ensure_ascii=False)
    except SshKeyManagementError as exc:
        ctx.pending_events.append({'type': 'ssh_key_deploy', 'status': 'error', 'alias': alias, 'kind': exc.kind, 'error': exc.message})
        return json.dumps({'status': 'error', 'kind': exc.kind, 'error': exc.message}, ensure_ascii=False)


def get_tools() -> List[ToolEntry]:
    return [
        _tool_entry(
            name='ssh_key_generate',
            description='Generate a local SSH key pair for remote server access and store it under drive state.',
            properties={
                'key_name': {'type': 'string', 'description': 'Logical key name, e.g. prod-box'},
                'key_type': {'type': 'string', 'enum': ['ed25519', 'rsa'], 'description': 'SSH key algorithm'},
                'comment': {'type': 'string', 'description': 'Comment embedded into the public key'},
                'overwrite': {'type': 'boolean', 'description': 'Overwrite an existing key with the same name'},
                'passphrase': {'type': 'string', 'description': 'Optional passphrase for the private key'},
            },
            required=['key_name'],
            handler=ssh_key_generate,
            is_code_tool=False,
        ),
        _tool_entry(
            name='ssh_key_list',
            description='List locally stored SSH keys available for remote deployment.',
            properties={},
            required=[],
            handler=ssh_key_list,
            is_code_tool=False,
        ),
        _tool_entry(
            name='ssh_key_deploy',
            description='Install a public SSH key into authorized_keys on a registered target and optionally switch the target to key-based auth.',
            properties={
                'alias': {'type': 'string', 'description': 'Registered SSH target alias'},
                'key_name': {'type': 'string', 'description': 'Previously generated key name stored under drive state'},
                'public_key_path': {'type': 'string', 'description': 'Alternative path to a public key file if not using key_name'},
                'password': {'type': 'string', 'description': 'Optional password override for password-auth targets'},
                'switch_target_to_key': {'type': 'boolean', 'description': 'Update the registry target to auth_mode=key after successful deployment'},
                'keep_password': {'type': 'boolean', 'description': 'Keep stored password in registry for rollback/fallback'},
                'timeout_sec': {'type': 'integer', 'description': f'Deploy timeout in seconds (5-{_MAX_DEPLOY_TIMEOUT_SEC})'},
            },
            required=['alias'],
            handler=ssh_key_deploy,
            is_code_tool=False,
        ),
    ]

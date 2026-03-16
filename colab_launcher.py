# Veles runtime launcher: thin orchestrator; heavy logic lives in supervisor/.
import logging
import os, sys, json, time, uuid, pathlib, subprocess, datetime, threading, queue as _queue_mod
from typing import Any, Dict, List, Optional, Set, Tuple
log = logging.getLogger(__name__)
def install_launcher_deps() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "requests", "SpeechRecognition"],
        check=True,
    )
install_launcher_deps()
from ouroboros.apply_patch import install as install_apply_patch
from ouroboros.llm import DEFAULT_LIGHT_MODEL
from ouroboros.model_modes import bootstrap_mode_env, get_active_mode, mode_summary_text, persist_active_mode
from ouroboros.artifacts import save_incoming_artifact, schedule_inbox_confirmation
install_apply_patch()
_LEGACY_CFG_WARNED: Set[str] = set()
_userdata_get = lambda key: None
def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = _userdata_get(name)
    if v is None or str(v).strip() == "":
        v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v
def get_cfg(name: str, default: Optional[str] = None, allow_legacy_secret: bool = False) -> Optional[str]:
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return v
    if allow_legacy_secret:
        legacy = _userdata_get(name)
        if legacy is not None and str(legacy).strip() != "":
            if name not in _LEGACY_CFG_WARNED:
                print(f"[cfg] DEPRECATED: move {name} from Colab Secrets to config cell/env.")
                _LEGACY_CFG_WARNED.add(name)
            return legacy
    return default

def _parse_int_cfg(raw: Optional[str], default: int, minimum: int = 0) -> int:
    try:
        val = int(str(raw))
    except Exception:
        val = default
    return max(minimum, val)
OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)
# Robust TOTAL_BUDGET parsing — handles \r\n, spaces, and other junk from Colab Secrets
# Example: user enters "8 800" → Colab stores as "8\r\n800" → we need 8800
try:
    import re
    _raw_budget = str(TOTAL_BUDGET_DEFAULT or "")
    _clean_budget = re.sub(r'[^0-9.\-]', '', _raw_budget)  # keep only digits, dot, minus
    TOTAL_BUDGET_LIMIT = float(_clean_budget) if _clean_budget else 0.0
    if _raw_budget.strip() != _clean_budget:
        log.warning(f"TOTAL_BUDGET cleaned: {_raw_budget!r} → {TOTAL_BUDGET_LIMIT}")
except Exception as e:
    log.warning(f"Failed to parse TOTAL_BUDGET ({TOTAL_BUDGET_DEFAULT!r}): {e}")
    TOTAL_BUDGET_LIMIT = 0.0
OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY", default="")
GITHUB_USER = get_cfg("GITHUB_USER", default=None, allow_legacy_secret=True)
GITHUB_REPO = get_cfg("GITHUB_REPO", default=None, allow_legacy_secret=True)
assert GITHUB_USER and str(GITHUB_USER).strip(), "GITHUB_USER not set. Add it to your config cell (see README)."
assert GITHUB_REPO and str(GITHUB_REPO).strip(), "GITHUB_REPO not set. Add it to your config cell (see README)."
MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="5", allow_legacy_secret=True) or "5")
MODEL_MAIN = get_cfg("OUROBOROS_MODEL", default="anthropic/claude-sonnet-4.6", allow_legacy_secret=True)
MODEL_CODE = get_cfg("OUROBOROS_MODEL_CODE", default="anthropic/claude-sonnet-4.6", allow_legacy_secret=True)
MODEL_LIGHT = get_cfg("OUROBOROS_MODEL_LIGHT", default=DEFAULT_LIGHT_MODEL, allow_legacy_secret=True)
BUDGET_REPORT_EVERY_MESSAGES = 10
SOFT_TIMEOUT_SEC = max(60, int(get_cfg("OUROBOROS_SOFT_TIMEOUT_SEC", default="600", allow_legacy_secret=True) or "600"))
HARD_TIMEOUT_SEC = max(120, int(get_cfg("OUROBOROS_HARD_TIMEOUT_SEC", default="1800", allow_legacy_secret=True) or "1800"))
EVOLUTION_HARD_TIMEOUT_SEC = max(300, int(get_cfg("OUROBOROS_EVOLUTION_HARD_TIMEOUT_SEC", default="3600", allow_legacy_secret=True) or "3600"))
DIAG_HEARTBEAT_SEC = _parse_int_cfg(
    get_cfg("OUROBOROS_DIAG_HEARTBEAT_SEC", default="30", allow_legacy_secret=True),
    default=30,
    minimum=0,
)
DIAG_SLOW_CYCLE_SEC = _parse_int_cfg(
    get_cfg("OUROBOROS_DIAG_SLOW_CYCLE_SEC", default="20", allow_legacy_secret=True),
    default=20,
    minimum=0,
)
os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
os.environ["ANTHROPIC_API_KEY"] = str(ANTHROPIC_API_KEY or "")
os.environ["GITHUB_USER"] = str(GITHUB_USER)
os.environ["GITHUB_REPO"] = str(GITHUB_REPO)
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN or "anthropic/claude-sonnet-4.6")
os.environ["OUROBOROS_MODEL_CODE"] = str(MODEL_CODE or "anthropic/claude-sonnet-4.6")
if MODEL_LIGHT:
    os.environ["OUROBOROS_MODEL_LIGHT"] = str(MODEL_LIGHT)
os.environ["OUROBOROS_DIAG_HEARTBEAT_SEC"] = str(DIAG_HEARTBEAT_SEC)
os.environ["OUROBOROS_DIAG_SLOW_CYCLE_SEC"] = str(DIAG_SLOW_CYCLE_SEC)
os.environ["OUROBOROS_EVOLUTION_HARD_TIMEOUT_SEC"] = str(EVOLUTION_HARD_TIMEOUT_SEC)
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)

# ----------------------------
# 2) Data directories (VPS: local disk, no Drive mount)
# ----------------------------
DRIVE_ROOT = pathlib.Path("/opt/veles-data").resolve()
REPO_DIR = pathlib.Path("/opt/veles").resolve()
DRIVE_ROOT.mkdir(parents=True, exist_ok=True)
for subdir in ("state", "logs", "memory/knowledge"):
    (DRIVE_ROOT / subdir).mkdir(parents=True, exist_ok=True)
REPO_DIR.mkdir(parents=True, exist_ok=True)
# ----------------------------
# 2.1) PID lock — prevent duplicate supervisor processes
# ----------------------------
_PID_LOCK_PATH = DRIVE_ROOT / "state" / "supervisor.pid"
def _acquire_pid_lock() -> Optional[int]:
    """Write our PID to lock file, killing any stale previous process."""
    import signal
    previous_pid: Optional[int] = None
    if _PID_LOCK_PATH.exists():
        try:
            old_pid = int(_PID_LOCK_PATH.read_text(encoding="utf-8").strip())
            if old_pid != os.getpid():
                previous_pid = old_pid
                try:
                    os.kill(old_pid, signal.SIGTERM)
                    log.warning("Killed stale supervisor process PID=%d", old_pid)
                    time.sleep(1)
                except (ProcessLookupError, PermissionError):
                    pass  # process already dead
        except (ValueError, OSError):
            pass
    _PID_LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return previous_pid
_PREVIOUS_SUPERVISOR_PID = _acquire_pid_lock()
# Clear stale owner mailbox files from previous session
try:
    from ouroboros.owner_inject import get_pending_path
    # Clean legacy global file
    _stale_inject = get_pending_path(DRIVE_ROOT)
    if _stale_inject.exists():
        _stale_inject.unlink(missing_ok=True)
    # Clean per-task mailbox dir
    _mailbox_dir = DRIVE_ROOT / "memory" / "owner_mailbox"
    if _mailbox_dir.exists():
        for _f in _mailbox_dir.iterdir():
            _f.unlink(missing_ok=True)
except Exception:
    pass
CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")
# 3) Git constants
BRANCH_DEV = "veles"
BRANCH_STABLE = "veles-stable"
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
# 4) Initialize supervisor modules
from supervisor.state import (
    init as state_init, load_state, save_state, append_jsonl,
    update_budget_from_usage, status_text, rotate_chat_log_if_needed,
    init_state,
)
state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
init_state()
ACTIVE_MODEL_MODE = bootstrap_mode_env()
from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat,
)
TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
telegram_init(
    drive_root=DRIVE_ROOT,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    budget_report_every=BUDGET_REPORT_EVERY_MESSAGES,
    tg_client=TG,
)
from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, checkout_and_reset,
    sync_runtime_dependencies, import_test, safe_restart,
)
git_ops_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)
from supervisor.queue import (
    enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot, snapshot_interrupted_work_info,
    cancel_task_by_id, queue_review_task, sort_pending,
)
from supervisor.workers import (
    init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    handle_chat_direct, handle_post_restart_ack, _get_chat_agent, owner_message_allows_auto_resume_release,
)
workers_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
    soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)
from supervisor.events import dispatch_event
from supervisor.audio_stt import transcribe_telegram_audio, AudioTranscriptionError
from supervisor.codex_bootstrap import prewarm_codex_accounts
from supervisor.restart_observability import arm_manual_terminal_restart_handoff


def _document_to_text_payload(doc: Dict[str, Any], caption: str, tg: TelegramClient, chat_id: int, drive_root: pathlib.Path, message_id: int = 0) -> Tuple[Optional[str], Optional[Tuple[str, str, str]], bool]:
    """Normalize Telegram document into either text augmentation or image payload.

    New rule: every incoming file is archived to artifacts/inbox first.
    Files without caption stay deferred in inbox and are not injected into LLM context.

    Returns: (text_override, image_data, handled)
    handled=False means unsupported and caller should stop processing.
    """
    mime_type = str(doc.get('mime_type') or '')
    file_name = str(doc.get('file_name') or 'file')
    file_ext = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
    file_id = doc.get('file_id')
    has_caption = bool((caption or '').strip())
    text_extensions = {
        'py', 'txt', 'md', 'json', 'csv', 'yaml', 'yml', 'toml',
        'cfg', 'ini', 'sh', 'bash', 'js', 'ts', 'html', 'css',
        'xml', 'sql', 'log', 'env', 'gitignore', 'dockerfile',
    }
    image_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}
    archive = lambda raw_b64, detected_mime, kind: save_incoming_artifact(
        drive_root, filename=file_name, file_base64=raw_b64, content_kind=kind,
        mime_type=detected_mime or mime_type or 'application/octet-stream',
        chat_id=chat_id, caption=caption, metadata={
            'message_id': int(message_id or 0),
            'telegram_file_id': file_id or '',
            'activation_mode': 'immediate' if has_caption else 'deferred',
        },
    )

    if ((mime_type or '').strip().lower().startswith('image/') or file_ext in image_extensions) and file_id:
        b64, detected_mime = tg.download_file_base64(file_id)
        if not b64:
            return None, None, False
        meta = archive(b64, detected_mime, 'image')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if has_caption:
            return None, (b64, detected_mime, caption), True
        return None, None, True

    if (file_ext in text_extensions or mime_type.startswith('text/')) and file_id:
        raw_b64, detected_mime = tg.download_file_base64(file_id)
        if not raw_b64:
            return None, None, False
        meta = archive(raw_b64, detected_mime, 'incoming')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if not has_caption:
            return None, None, True
        import base64 as _b64mod
        file_bytes = _b64mod.b64decode(raw_b64)
        try:
            text_content = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            text_content = file_bytes.decode('latin-1')
        max_file_content = 80000
        full_len = len(text_content)
        if full_len > max_file_content:
            text_content = text_content[:max_file_content] + f'\n\n... (обрезано, всего {full_len} символов)'
        user_text = caption or ''
        payload = f"{user_text}\n\n📎 Файл: {file_name}\n```{file_ext}\n{text_content}\n```"
        return payload.strip(), None, True

    if file_ext == 'pdf' and file_id:
        raw_b64, detected_mime = tg.download_file_base64(file_id)
        if not raw_b64:
            return None, None, False
        meta = archive(raw_b64, detected_mime or 'application/pdf', 'pdf')
        if isinstance(meta, dict) and not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
        if not has_caption:
            return None, None, True
        import base64 as _b64mod
        import tempfile as _tmpmod
        file_bytes = _b64mod.b64decode(raw_b64)
        pdf_text = None
        tmp_path = None
        try:
            with _tmpmod.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            try:
                import pdfplumber
                with pdfplumber.open(tmp_path) as pdf:
                    pdf_text = '\n\n'.join(page.extract_text() or '' for page in pdf.pages)
            except ImportError:
                try:
                    from PyPDF2 import PdfReader
                    reader = PdfReader(tmp_path)
                    pdf_text = '\n\n'.join(page.extract_text() or '' for page in reader.pages)
                except ImportError:
                    pdf_text = None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if pdf_text:
            max_file_content = 80000
            if len(pdf_text) > max_file_content:
                pdf_text = pdf_text[:max_file_content] + '\n\n... (обрезано)'
            user_text = caption or ''
            payload = f"{user_text}\n\n📎 PDF: {file_name}\n{pdf_text}"
            return payload.strip(), None, True
        send_with_budget(chat_id, '⚠️ Не удалось извлечь текст из PDF. Установите pdfplumber или PyPDF2.')
        return None, None, False

    raw_b64, detected_mime = tg.download_file_base64(file_id) if file_id else (None, '')
    if raw_b64:
        archive(raw_b64, detected_mime, 'binary')
        if not has_caption:
            schedule_inbox_confirmation(chat_id, file_name, send_with_budget)
            return None, None, True
    send_with_budget(chat_id, f'⚠️ Формат .{file_ext or "bin"} не поддерживается для немедленной обработки. Файл сохранён во входящий архив.')
    return None, None, True

# ----------------------------
# 5) Bootstrap repo
# ----------------------------
ensure_repo_present()
ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
assert ok, f"Bootstrap failed: {msg}"
prewarm_codex_accounts(DRIVE_ROOT)
# ----------------------------
# 6) Start workers
# ----------------------------
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
_snapshot_resume = snapshot_interrupted_work_info()
_st_resume = load_state()
if _snapshot_resume.get("has_interrupted_work"):
    _st_resume["resume_snapshot_pending_count"] = int(_snapshot_resume.get("pending_count") or 0)
    _st_resume["resume_snapshot_running_count"] = int(_snapshot_resume.get("running_count") or 0)
    save_state(_st_resume)
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(int(st_boot["owner_chat_id"]),
                         f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.")
_launcher_session_id = uuid.uuid4().hex
_st_launch = load_state()
_st_launch["launcher_session_id"] = _launcher_session_id
_st_launch, _manual_terminal_restart_armed = arm_manual_terminal_restart_handoff(
    _st_launch,
    _PREVIOUS_SUPERVISOR_PID,
)
save_state(_st_launch)
if _manual_terminal_restart_armed:
    append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "manual_terminal_restart_inferred",
        "previous_pid": _PREVIOUS_SUPERVISOR_PID,
        "launcher_session_id": _launcher_session_id,
    })
append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "launcher_session_id": _launcher_session_id,
    "active_model_mode": get_active_mode().key,
    "model_default": os.environ.get("OUROBOROS_MODEL", MODEL_MAIN), "model_code": MODEL_CODE, "model_light": os.environ.get("OUROBOROS_MODEL_LIGHT", MODEL_LIGHT),
    "soft_timeout_sec": SOFT_TIMEOUT_SEC, "hard_timeout_sec": HARD_TIMEOUT_SEC,
    "worker_start_method": str(os.environ.get("OUROBOROS_WORKER_START_METHOD") or ""),
    "diag_heartbeat_sec": DIAG_HEARTBEAT_SEC,
    "diag_slow_cycle_sec": DIAG_SLOW_CYCLE_SEC,
})
# ----------------------------
# 6.0.1) Post-restart owner notification
# ----------------------------
def _dispatch_agent_post_restart_ack() -> None:
    try:
        st = load_state()
        if not bool(st.get("restart_notify_pending")):
            return
        chat_id = int(st.get("owner_chat_id") or 0)
        if not chat_id:
            return
        reason = str(st.get("restart_notify_reason") or "").strip() or "unspecified"
        source = str(st.get("restart_notify_source") or "").strip() or "restart"
        requested_at = str(st.get("restart_notify_requested_at") or "").strip()
        current_sha = str(st.get("current_sha") or "").strip()
        launcher_started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        service_text = (
            "♻️ Restart completed: service layer is up.\n"
            f"Restart time: <code>{requested_at or 'unknown'}</code>\n"
            f"Source: <code>{source}</code>\n\n"
            f"<code>launcher_start: {launcher_started_at} branch={BRANCH_DEV} sha={(current_sha or 'unknown')[:12]} mode={get_active_mode().key}</code>"
        )
        send_with_budget(chat_id, service_text, force_budget=True, fmt="html")
        def _run_agent_ack() -> None:
            ok = handle_post_restart_ack(
                chat_id=chat_id,
                restart_reason=reason,
                restart_source=source,
                restart_requested_at=requested_at,
            )
            if not ok:
                return
            try:
                st_done = load_state()
                st_done["restart_notify_pending"] = False
                st_done["restart_notify_reason"] = ""
                st_done["restart_notify_requested_at"] = ""
                st_done["restart_notify_source"] = ""
                save_state(st_done)
                append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "restart_ack_dispatched",
                    "reason": reason,
                    "source": source,
                    "launcher_session_id": _launcher_session_id,
                })
            except Exception as e:
                append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "restart_ack_finalize_error",
                    "error": repr(e),
                })
        threading.Thread(
            target=_run_agent_ack,
            name="post-restart-agent-ack",
            daemon=True,
        ).start()
    except Exception as e:
        append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "restart_ack_dispatch_error",
            "error": repr(e),
        })

# 6.1) Post-restart acknowledgement from the agent itself
_dispatch_agent_post_restart_ack()
# ----------------------------
# 6.2) Direct-mode watchdog
# ----------------------------
def _chat_watchdog_loop():
    """Monitor direct-mode chat agent for hangs. Runs as daemon thread."""
    soft_warned = False
    while True:
        time.sleep(30)
        try:
            agent = _get_chat_agent()
            if not agent._busy:
                soft_warned = False
                continue
            now = time.time()
            idle_sec = now - agent._last_progress_ts
            total_sec = now - agent._task_started_ts
            if idle_sec >= HARD_TIMEOUT_SEC:
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"⚠️ Task stuck ({int(total_sec)}s without progress). "
                        f"Restarting agent.",
                    )
                reset_chat_agent()
                soft_warned = False
                continue
            if idle_sec >= SOFT_TIMEOUT_SEC and not soft_warned:
                soft_warned = True
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "chat_soft_timeout",
                        "runtime_sec": round(total_sec, 2),
                        "idle_sec": round(idle_sec, 2),
                        "soft_timeout_sec": SOFT_TIMEOUT_SEC,
                        "hard_timeout_sec": HARD_TIMEOUT_SEC,
                    },
                )
        except Exception:
            log.debug("Failed to check/notify chat watchdog", exc_info=True)
            pass
_watchdog_thread = threading.Thread(target=_chat_watchdog_loop, daemon=True)
_watchdog_thread.start()
# 6.3) Background consciousness
from ouroboros.consciousness import BackgroundConsciousness
def _get_owner_chat_id() -> Optional[int]:
    try:
        st = load_state()
        cid = st.get("owner_chat_id")
        return int(cid) if cid else None
    except Exception:
        return None
_consciousness = BackgroundConsciousness(
    drive_root=DRIVE_ROOT,
    repo_dir=REPO_DIR,
    event_queue=get_event_q(),
    owner_chat_id_fn=_get_owner_chat_id,
)
def reset_chat_agent():
    """Reset the direct-mode chat agent (called by watchdog on hangs)."""
    import supervisor.workers as _w
    _w._chat_agent = None
# ----------------------------
# 7) Main loop
# ----------------------------
import types
_event_ctx = types.SimpleNamespace(
    DRIVE_ROOT=DRIVE_ROOT,
    REPO_DIR=REPO_DIR,
    BRANCH_DEV=BRANCH_DEV,
    BRANCH_STABLE=BRANCH_STABLE,
    TG=TG,
    WORKERS=WORKERS,
    PENDING=PENDING,
    RUNNING=RUNNING,
    MAX_WORKERS=MAX_WORKERS,
    send_with_budget=send_with_budget,
    load_state=load_state,
    save_state=save_state,
    update_budget_from_usage=update_budget_from_usage,
    append_jsonl=append_jsonl,
    enqueue_task=enqueue_task,
    cancel_task_by_id=cancel_task_by_id,
    queue_review_task=queue_review_task,
    persist_queue_snapshot=persist_queue_snapshot,
    safe_restart=safe_restart,
    kill_workers=kill_workers,
    spawn_workers=spawn_workers,
    sort_pending=sort_pending,
    consciousness=_consciousness,
)

def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
    """Handle supervisor slash-commands.
    Returns:
        True  — terminal command fully handled (caller should `continue`)
        str   — dual-path note to prepend (caller falls through to LLM)
        ""    — not a recognized command (falsy, caller falls through)
    """
    lowered = text.strip().lower()
    if lowered.startswith("/panic"):
        send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
        kill_workers()
        st2 = load_state()
        st2["tg_offset"] = tg_offset
        save_state(st2)
        raise SystemExit("PANIC")
    if lowered.startswith("/restart"):
        st2 = load_state()
        st2["session_id"] = uuid.uuid4().hex
        st2["tg_offset"] = tg_offset
        _agent_busy = False
        try:
            _agent_busy = bool(_get_chat_agent()._busy)
        except Exception:
            _agent_busy = False
        st2["resume_needed"] = False
        st2["resume_reason"] = ""
        st2["resume_snapshot_pending_count"] = len(PENDING)
        st2["resume_snapshot_running_count"] = len(RUNNING)
        st2["restart_notify_pending"] = True
        st2["restart_notify_reason"] = "owner_restart"
        st2["restart_notify_requested_at"] = now_iso
        st2["restart_notify_source"] = "owner_restart_command"
        save_state(st2)
        send_with_budget(chat_id, "♻️ Restarting (soft).")
        ok, msg = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
        if not ok:
            send_with_budget(chat_id, f"⚠️ Restart cancelled: {msg}")
            return True
        kill_workers()
        os.execv(sys.executable, [sys.executable, __file__])
    # Dual-path commands: supervisor handles + LLM sees a note
    if lowered.startswith("/status"):
        status = status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
        send_with_budget(chat_id, status, force_budget=True)
        return "[Supervisor handled /status — status text already sent to chat]\n"
    if lowered.startswith("/review"):
        queue_review_task(reason="owner:/review", force=True)
        return "[Supervisor handled /review — review task queued]\n"
    if lowered.startswith("/evolve"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "on"
        turn_on = action not in ("off", "stop", "0")
        st2 = load_state()
        st2["evolution_mode_enabled"] = bool(turn_on)
        if turn_on:
            st2["evolution_consecutive_failures"] = 0
            st2["no_commit_streak"] = 0
            st2["evolution_cycles_1h"] = []
        else:
            st2["suppress_auto_resume_until_owner_message"] = True
        save_state(st2)
        if not turn_on:
            PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
            # Cancel running evolution tasks
            for task_id, meta in list(RUNNING.items()):
                task = meta.get("task") if isinstance(meta, dict) else {}
                if isinstance(task, dict) and str(task.get("type")) == "evolution":
                    cancel_task_by_id(task_id)
            sort_pending()
            persist_queue_snapshot(reason="evolve_off")
        state_str = "ON" if turn_on else "OFF"
        send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
        return f"[Supervisor handled /evolve — evolution toggled {state_str}]\n"
    if lowered.startswith("/bg"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "status"
        if action in ("start", "on", "1"):
            result = _consciousness.start()
            send_with_budget(chat_id, f"🧠 {result}")
        elif action in ("stop", "off", "0"):
            result = _consciousness.stop()
            send_with_budget(chat_id, f"🧠 {result}")
        else:
            bg_status = "running" if _consciousness.is_running else "stopped"
            send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
        return f"[Supervisor handled /bg {action}]\n"
    if lowered.startswith("/sonnet"):
        mode = persist_active_mode("sonnet")
        send_with_budget(chat_id, f"✅ Mode: sonnet\n• Main: {mode.model}\n• Rounds: {mode.max_rounds}\n• Tools: off")
        return True
    if lowered.startswith("/haiku"):
        mode = persist_active_mode("haiku")
        send_with_budget(chat_id, f"✅ Mode: haiku\n• Main: {mode.model}\n• Rounds: {mode.max_rounds}\n• Tools: on")
        return True
    if lowered.startswith("/opus"):
        mode = persist_active_mode("opus")
        send_with_budget(chat_id, f"✅ Mode: opus\n• Main: {mode.model}\n• Rounds: {mode.max_rounds}\n• Tools: off")
        return True
    if lowered.startswith("/qwen"):
        send_with_budget(chat_id, "⚠️ /qwen removed from the primary mode surface. Use /codex, /haiku, /sonnet, /opus.")
        return True
    if lowered.startswith("/codex"):
        persist_active_mode("codex")
        send_with_budget(chat_id, "✅ Switched to codex mode\n" + mode_summary_text())
        return True
    if lowered.startswith("/model"):
        send_with_budget(chat_id, mode_summary_text())
        return True

    if lowered.startswith("/accounts"):
        from ouroboros.codex_proxy import get_accounts_status
        statuses = get_accounts_status()
        lines = [f"📊 Codex Accounts: {len(statuses)} шт.\n"]
        for st_acc in statuses:
            i = st_acc["index"]
            if st_acc["dead"]:
                icon, status = "💀", "dead"
            elif st_acc["in_cooldown"]:
                icon, status = "⏳", f"cooldown ({st_acc['cooldown_remaining']}s)"
            elif st_acc["has_access"]:
                icon, status = "✅", "active"
            else:
                icon, status = "⚠️", "no access token"
            active_marker = " ← [active]" if st_acc["active"] else ""
            usage = f"5h:{st_acc['requests_5h']} 7d:{st_acc['requests_7d']}"
            lines.append(f"{icon} #{i}: {status} | {usage}{active_marker}")
        send_with_budget(chat_id, "\n".join(lines))
        return True
    if lowered.startswith("/switch"):
        from ouroboros.codex_proxy import force_switch_account
        parts = lowered.split()
        target = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else -1
        result = force_switch_account(target_idx=target)
        if result["ok"]:
            send_with_budget(chat_id, f"✅ {result['message']}")
        else:
            send_with_budget(chat_id, f"⚠️ {result['message']}")
        return True
    return ""

offset = int(load_state().get("tg_offset") or 0)
_last_diag_heartbeat_ts = 0.0
_last_message_ts: float = time.time()  # Start in active mode after restart
_ACTIVE_MODE_SEC: int = 300  # 5 min of activity = active polling mode
# Auto-start background consciousness (creator's policy: always on by default)
try:
    _consciousness.start()
    log.info("🧠 Background consciousness auto-started (default: always on)")
except Exception as e:
    log.warning("consciousness auto-start failed: %s", e)
while True:
    loop_started_ts = time.time()
    rotate_chat_log_if_needed(DRIVE_ROOT)
    ensure_workers_healthy()
    # Drain worker events
    event_q = get_event_q()
    while True:
        try:
            evt = event_q.get_nowait()
        except _queue_mod.Empty:
            break
        dispatch_event(evt, _event_ctx)
    enforce_task_timeouts()
    enqueue_evolution_task_if_needed()
    assign_tasks()
    persist_queue_snapshot(reason="main_loop")
    _now = time.time()
    # Poll Telegram — adaptive: fast when active, long-poll when idle
    _active = (_now - _last_message_ts) < _ACTIVE_MODE_SEC
    _poll_timeout = 0 if _active else 10
    try:
        updates = TG.get_updates(offset=offset, timeout=_poll_timeout)
    except Exception as e:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "telegram_poll_error", "offset": offset, "error": repr(e),
            },
        )
        time.sleep(1.5)
        continue
    for upd in updates:
        offset = int(upd["update_id"]) + 1
        msg = upd.get("message") or upd.get("edited_message") or {}
        if not msg:
            continue
        chat_id = int(msg["chat"]["id"])
        from_user = msg.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        text = str(msg.get("text") or "")
        caption = str(msg.get("caption") or "")
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Extract image/audio/file payload if present
        image_data = None  # Will be (base64, mime_type, caption) or None
        if msg.get("photo"):
            # photo is array of PhotoSize, last one is largest
            best_photo = msg["photo"][-1]
            file_id = best_photo.get("file_id")
            if file_id:
                b64, mime = TG.download_file_base64(file_id)
                if b64:
                    image_data = (b64, mime, caption)
        elif msg.get("voice") or msg.get("audio") or msg.get("video_note"):
            audio_obj = msg.get("voice") or msg.get("audio") or msg.get("video_note") or {}
            audio_kind = "voice" if msg.get("voice") else ("audio" if msg.get("audio") else "video_note")
            file_id = str(audio_obj.get("file_id") or "")
            mime_type = str(audio_obj.get("mime_type") or "")
            file_name = str(audio_obj.get("file_name") or f"{audio_kind}")
            if file_id:
                audio_b64, audio_mime = TG.download_file_base64(file_id, max_bytes=25_000_000)
                if audio_b64:
                    try:
                        tr = transcribe_telegram_audio(
                            drive_root=DRIVE_ROOT,
                            audio_b64=audio_b64,
                            mime_type=mime_type or audio_mime,
                            kind=audio_kind,
                            file_name=file_name,
                            language="ru-RU",
                        )
                        transcribed = str(tr.get("text") or "").strip()
                        voice_prefix_map = {
                            "voice": "[Голосовое сообщение]",
                            "audio": "[Аудио]",
                            "video_note": "[Кружок]",
                        }
                        prefix = voice_prefix_map.get(audio_kind, "[Аудио]")
                        text = f"{prefix}\n{transcribed}" if transcribed else prefix
                        if caption:
                            text = f"{caption}\n\n{text}" if text else caption
                    except AudioTranscriptionError as e:
                        send_with_budget(chat_id, f"⚠️ Не удалось распознать голосовое: {e}")
                        continue
                else:
                    send_with_budget(chat_id, "⚠️ Не удалось скачать голосовое из Telegram.")
                    continue
        elif msg.get("document"):
            doc = msg["document"]
            text_override, doc_image_data, handled = _document_to_text_payload(doc, caption, TG, chat_id, DRIVE_ROOT, int(msg.get("message_id") or 0))
            if not handled:
                continue
            if text_override:
                text = text_override
            if doc_image_data:
                image_data = doc_image_data
        st = load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id
            st["last_owner_message_at"] = now_iso
            save_state(st)
            log_chat("in", chat_id, user_id, text)
            send_with_budget(chat_id, "✅ Owner registered. Veles online.")
            continue
        if user_id != int(st.get("owner_id")):
            continue
        log_chat("in", chat_id, user_id, text)
        st["last_owner_message_at"] = now_iso
        if bool(st.get("suppress_auto_resume_until_owner_message")) and owner_message_allows_auto_resume_release(text):
            st["suppress_auto_resume_until_owner_message"] = False
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": now_iso,
                    "type": "auto_resume_unsuppressed",
                    "reason": "working_owner_message",
                },
            )
        _last_message_ts = time.time()
        save_state(st)
        # --- Supervisor commands ---
        if text.strip().lower().startswith("/"):
            try:
                result = _handle_supervisor_command(text, chat_id, tg_offset=offset)
                if result is True:
                    continue  # terminal command, fully handled
                elif result:  # non-empty string = dual-path note
                    text = result + text  # prepend note, fall through to LLM
            except SystemExit:
                raise
            except Exception:
                log.warning("Supervisor command handler error", exc_info=True)
        # All other messages (and dual-path commands) → direct chat with Ouroboros
        if not text and not image_data:
            continue  # empty message, skip
        # Feed observation to consciousness
        _consciousness.inject_observation(f"Owner message: {text[:100]}")
        agent = _get_chat_agent()
        if agent._busy:
            # BUSY PATH: inject into active conversation (single consumer)
            if image_data:
                if text:
                    agent.inject_message(text)
                send_with_budget(chat_id, "📎 Photo received, but a task is in progress. Send again when I'm free.")
            elif text:
                agent.inject_message(text)
        else:
            # FREE PATH: batch-collect burst messages, then dispatch (single consumer)
            # Batch-collect burst messages: wait briefly for follow-up messages
            # This prevents "do X" → "cancel" race conditions
            _BATCH_WINDOW_SEC = 1.5  # collect messages for 1500ms
            _EARLY_EXIT_SEC = 0.15   # if no burst within 150ms → dispatch immediately
            _batch_start = time.time()
            _batch_deadline = _batch_start + _BATCH_WINDOW_SEC
            _batched_texts = [text] if text else []
            _batched_image = image_data  # keep first image
            _batch_state = load_state()
            _batch_state_dirty = False
            while time.time() < _batch_deadline:
                time.sleep(0.1)
                try:
                    _extra_updates = TG.get_updates(offset=offset, timeout=0) or []
                except Exception:
                    _extra_updates = []
                if not _extra_updates and (time.time() - _batch_start) < _EARLY_EXIT_SEC:
                    # No follow-up messages in first 150ms → single message, dispatch immediately
                    break
                for _upd in _extra_updates:
                    offset = max(offset, int(_upd.get("update_id", offset - 1)) + 1)
                    _msg2 = _upd.get("message") or _upd.get("edited_message") or {}
                    _uid2 = (_msg2.get("from") or {}).get("id")
                    _cid2 = (_msg2.get("chat") or {}).get("id")
                    _txt2 = _msg2.get("text") or _msg2.get("caption") or ""
                    if _uid2 and _batch_state.get("owner_id") and _uid2 == int(_batch_state["owner_id"]):
                        log_chat("in", _cid2, _uid2, _txt2)
                        _batch_state["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        _batch_state_dirty = True
                        # Handle supervisor commands in batch window
                        if _txt2.strip().lower().startswith("/"):
                            try:
                                _cmd_result = _handle_supervisor_command(_txt2, _cid2, tg_offset=offset)
                                if _cmd_result is True:
                                    continue  # terminal command, don't batch
                                elif _cmd_result:
                                    _txt2 = _cmd_result + _txt2  # dual-path: prepend note
                            except SystemExit:
                                raise
                            except Exception:
                                log.warning("Supervisor command in batch failed", exc_info=True)
                        if _txt2:
                            _batched_texts.append(_txt2)
                            _batch_deadline = max(_batch_deadline, time.time() + 0.3)  # extend for burst
                        if not _batched_image:
                            _photo2 = (_msg2.get("photo") or [None])[-1] or {}
                            _fid2 = _photo2.get("file_id")
                            if _fid2:
                                _b642, _mime2 = TG.download_file_base64(_fid2)
                                if _b642 and _is_supported_image_mime(_mime2):
                                    _batched_image = (_b642, _mime2, _txt2)
                            elif _msg2.get("document"):
                                _doc_text2, _doc_img2, _doc_handled2 = _document_to_text_payload(_msg2.get("document") or {}, _txt2, TG, _cid2, DRIVE_ROOT, int(_msg2.get("message_id") or 0))
                                if _doc_text2:
                                    _batched_texts.append(_doc_text2)
                                    _batch_deadline = max(_batch_deadline, time.time() + 0.3)
                                if _doc_img2 and not _batched_image:
                                    _batched_image = _doc_img2
                                if not _doc_handled2:
                                    continue
            # Save state once if mutated during batch window
            if _batch_state_dirty:
                save_state(_batch_state)
            # Merge all batched texts into one message
            if len(_batched_texts) > 1:
                final_text = "\n\n".join(_batched_texts)
                log.info("Message batch: %d messages merged into one", len(_batched_texts))
            elif _batched_texts:
                final_text = _batched_texts[0]
            else:
                final_text = text  # fallback to original
            # Re-check if agent became busy during batch window (race condition fix)
            if agent._busy:
                if final_text:
                    agent.inject_message(final_text)
                if _batched_image:
                    send_with_budget(chat_id, "📎 Photo received, but a task is in progress. Send again when I'm free.")
            else:
                # Dispatch to direct chat handler
                _consciousness.pause()
                def _run_task_and_resume(cid, txt, img):
                    try:
                        handle_chat_direct(cid, txt, img)
                    finally:
                        _consciousness.resume()
                _t = threading.Thread(
                    target=_run_task_and_resume,
                    args=(chat_id, final_text, _batched_image),
                    daemon=True,
                )
                try:
                    _t.start()
                except Exception as _te:
                    log.error("Failed to start chat thread: %s", _te)
                    _consciousness.resume()  # ensure resume if thread fails to start
    st = load_state()
    st["tg_offset"] = offset
    save_state(st)
    now_epoch = time.time()
    loop_duration_sec = now_epoch - loop_started_ts
    if DIAG_SLOW_CYCLE_SEC > 0 and loop_duration_sec >= float(DIAG_SLOW_CYCLE_SEC):
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_slow_cycle",
                "duration_sec": round(loop_duration_sec, 3),
                "pending_count": len(PENDING),
                "running_count": len(RUNNING),
            },
        )
    if DIAG_HEARTBEAT_SEC > 0 and (now_epoch - _last_diag_heartbeat_ts) >= float(DIAG_HEARTBEAT_SEC):
        workers_total = len(WORKERS)
        workers_alive = sum(1 for w in WORKERS.values() if w.proc.is_alive())
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_heartbeat",
                "offset": offset,
                "workers_total": workers_total,
                "workers_alive": workers_alive,
                "pending_count": len(PENDING),
                "running_count": len(RUNNING),
                "event_q_size": (int(event_q.qsize()) if hasattr(event_q, "qsize") else -1),
                "running_task_ids": list(RUNNING.keys())[:5],
                "spent_usd": st.get("spent_usd"),
            },
        )
        _last_diag_heartbeat_ts = now_epoch
    # Short sleep in active mode (fast response), longer when idle (save CPU)
    _loop_sleep = 0.1 if (_now - _last_message_ts) < _ACTIVE_MODE_SEC else 0.5
    time.sleep(_loop_sleep)

"""digest_scheduler — background daemon that auto-fires inbox_digest on schedule.

Runs as a daemon thread in colab_launcher.py.
Every 5 minutes wakes up, checks if digest_schedule.json has a due run, and if so:
  - calls inbox_digest with configured params
  - optionally sends to owner
  - updates next_run_at

This is how inbox_digest becomes autonomous rather than on-demand only.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_SCHEDULE_FILE = "memory/digest_schedule.json"
_CHECK_INTERVAL_SEC = 300  # check every 5 minutes


def _schedule_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _SCHEDULE_FILE


def _load_schedule() -> Dict[str, Any]:
    path = _schedule_path()
    if not path.exists():
        return {"enabled": False}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": False}


def _save_schedule(data: Dict[str, Any]) -> None:
    path = _schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _run_digest(
    schedule: Dict[str, Any],
    send_fn: Callable[[int, str], None],
    owner_chat_id: Optional[int],
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> None:
    """Execute inbox_digest with schedule params."""
    sources = schedule.get("sources") or []
    notify = schedule.get("notify_owner", True)
    model = schedule.get("model", "codex/gpt-4.1-mini")

    try:
        from ouroboros.tools.registry import ToolContext, ToolRegistry
        ctx = ToolContext(
            repo_dir=repo_dir,
            drive_root=drive_root,
        )
        # emit_progress_fn: no-op for background runs
        registry = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)

        # Build args
        args: Dict[str, Any] = {"notify_owner": notify, "model": model}
        if sources:
            args["sources"] = sources

        result = registry.execute("inbox_digest", args, ctx)

        # Log success
        log.info("DigestScheduler: digest completed, notify=%s, model=%s", notify, model)
        from ouroboros.utils import append_jsonl, utc_now_iso
        append_jsonl(drive_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "digest_scheduler_run",
            "status": "ok",
            "notify": notify,
            "model": model,
            "sources": sources,
        })

    except Exception as e:
        log.error("DigestScheduler: digest failed: %s", e)
        from ouroboros.utils import append_jsonl, utc_now_iso
        append_jsonl(pathlib.Path(_DRIVE_ROOT) / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "digest_scheduler_run",
            "status": "error",
            "error": repr(e)[:300],
        })
        # Notify owner about failure if possible
        if notify and owner_chat_id and send_fn:
            try:
                send_fn(owner_chat_id, f"⚠️ Auto-digest failed: {repr(e)[:200]}")
            except Exception:
                pass
        raise  # Re-raise so caller knows digest failed


class DigestScheduler:
    """Daemon thread that checks digest schedule every 5 minutes and fires when due."""

    def __init__(
        self,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        owner_chat_id_fn: Callable[[], Optional[int]],
        send_fn: Callable[[int, str], None],
    ):
        self._repo_dir = repo_dir
        self._drive_root = drive_root
        self._owner_chat_id_fn = owner_chat_id_fn
        self._send_fn = send_fn
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="digest-scheduler")
        self._thread.start()
        log.info("DigestScheduler started")

    def stop(self) -> None:
        self._stop.set()

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        # Stagger initial check by 30s to avoid startup noise
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self._check_and_run()
            except Exception:
                log.debug("DigestScheduler check error", exc_info=True)
            self._stop.wait(_CHECK_INTERVAL_SEC)

    def _check_and_run(self) -> None:
        schedule = _load_schedule()
        if not schedule.get("enabled"):
            return

        next_run_str = schedule.get("next_run_at")
        if not next_run_str:
            return

        try:
            next_run = datetime.fromisoformat(next_run_str)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
        except Exception:
            return

        now = _utc_now()
        if now < next_run:
            return  # Not yet

        # Due! Run the digest.
        log.info("DigestScheduler: scheduled run is due (next_run=%s), firing now", next_run_str)

        interval_hours = float(schedule.get("interval_hours", 6.0))
        owner_chat_id = self._owner_chat_id_fn()

        try:
            _run_digest(
                schedule=schedule,
                send_fn=self._send_fn,
                owner_chat_id=owner_chat_id,
                repo_dir=self._repo_dir,
                drive_root=self._drive_root,
            )
            # Save updated schedule only after successful run
            schedule["last_run_at"] = now.isoformat()
            schedule["next_run_at"] = (now + timedelta(hours=interval_hours)).isoformat()
            schedule["run_count"] = int(schedule.get("run_count", 0)) + 1
            _save_schedule(schedule)
        except Exception:
            # Digest failed — do NOT advance next_run_at so it retries next cycle
            log.warning("DigestScheduler: digest failed, next_run_at NOT advanced (will retry)")

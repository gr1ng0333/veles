from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional


_ARTIFACT_ROOT = "artifacts/outbox"


def save_artifact(
    ctx_or_root,
    *,
    filename: str,
    data: bytes | None = None,
    content: str = "",
    file_base64: str = "",
    content_kind: str = "generic",
    source: str = "tool",
    task_id: str = "",
    chat_id: Optional[int] = None,
    mime_type: str = "application/octet-stream",
    caption: str = "",
    related_message: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any] | str:
    drive_root = getattr(ctx_or_root, "drive_root", ctx_or_root)
    task_id = task_id or getattr(ctx_or_root, "task_id", "") or ""
    chat_id = chat_id if chat_id is not None else getattr(ctx_or_root, "current_chat_id", None)
    if not content and data is None and not file_base64:
        return "⚠️ save_artifact requires non-empty content."
    payload = data if data is not None else (base64.b64decode(file_base64) if file_base64 else content.encode("utf-8"))
    now = datetime.now(timezone.utc)
    base_dir = pathlib.Path(drive_root) / _ARTIFACT_ROOT / now.strftime("%Y/%m/%d")
    task_part = (re.sub(r"-+", "-", re.sub(r"[^a-z0-9._-]+", "-", (task_id or "direct-chat").strip().lower())).strip("-._") or "direct-chat")
    kind_part = (content_kind or ("python" if pathlib.Path(filename or "").suffix.lower() == ".py" else "html" if pathlib.Path(filename or "").suffix.lower() in {".html", ".htm"} else "config" if pathlib.Path(filename or "").suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"} else "table" if pathlib.Path(filename or "").suffix.lower() in {".csv", ".tsv"} else "text" if pathlib.Path(filename or "").suffix.lower() in {".txt", ".md", ".rst"} or mime_type.startswith("text/") else "generic"))
    kind_part = re.sub(r"-+", "-", re.sub(r"[^a-z0-9._-]+", "-", kind_part.strip().lower())).strip("-._") or "generic"
    target_dir = base_dir / task_part / kind_part
    target_dir.mkdir(parents=True, exist_ok=True)

    path = pathlib.Path((filename or "").strip())
    stem = re.sub(r"-+", "-", re.sub(r"[^a-z0-9._-]+", "-", (path.stem or kind_part).strip().lower())).strip("-._") or kind_part
    suffix = path.suffix[:20].lower()
    if not suffix or not re.fullmatch(r"\.[A-Za-z0-9._-]{1,19}", suffix):
        suffix = ".bin"
    file_path = target_dir / f"{stem}{suffix}"
    if file_path.exists():
        digest = hashlib.sha256(payload).hexdigest()[:10]
        file_path = target_dir / f"{stem}-{digest}{suffix}"

    file_path.write_bytes(payload)
    meta = {
        "ts": now.isoformat(),
        "filename": file_path.name,
        "relative_path": str(file_path.relative_to(drive_root)),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mime_type": mime_type,
        "content_kind": content_kind,
        "source": source,
        "task_id": task_id or "",
        "chat_id": int(chat_id or 0),
        "caption": caption or "",
        "related_message": related_message or "",
    }
    if metadata:
        meta["metadata"] = metadata
    file_path.with_suffix(file_path.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta

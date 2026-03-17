"""File tools: repo_read, repo_list, drive_read, drive_list, drive_write, codebase_digest, summarize_dialogue."""

from __future__ import annotations

import ast
import base64
import json
import logging
import mimetypes
import os
import pathlib
import tempfile
import uuid
from functools import partial
from typing import Any, Dict, List, Tuple

from ouroboros.artifacts import list_incoming_artifacts, save_artifact
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath, utc_now_iso

log = logging.getLogger(__name__)


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
    target = (root / safe_relpath(rel)).resolve()
    if not target.exists():
        return [f"⚠️ Directory not found: {rel}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {rel}"]
    items = []
    try:
        for entry in sorted(target.iterdir()):
            if len(items) >= max_entries:
                items.append(f"...(truncated at {max_entries})")
                break
            suffix = "/" if entry.is_dir() else ""
            items.append(str(entry.relative_to(root)) + suffix)
    except Exception as e:
        items.append(f"⚠️ Error listing: {e}")
    return items


def _read_from(ctx: ToolContext, path: str, which: str) -> str:
    return read_text(getattr(ctx, which)(path))


def _list_from(ctx: ToolContext, dir: str = ".", max_entries: int = 500, root_attr: str = "repo_dir") -> str:
    return json.dumps(_list_dir(getattr(ctx, root_attr), dir, max_entries), ensure_ascii=False, indent=2)


def _drive_write(ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> str:
    p = ctx.drive_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    return f"OK: wrote {mode} {path} ({len(content)} chars)"



def _send_local_file(
    ctx: ToolContext,
    path: str,
    caption: str = "",
    filename: str = "",
    mime_type: str = "",
) -> str:
    """Send an existing local file from repo/drive/tmp to Telegram without manual base64 handling."""
    chat_id = int(ctx.current_chat_id or 0)
    if not chat_id:
        return "⚠️ No current chat available for send_local_file."

    raw = str(path or "").strip()
    if not raw:
        return "⚠️ send_local_file requires a non-empty path."

    candidate = pathlib.Path(raw).expanduser()
    try:
        local_path = ((ctx.repo_dir / safe_relpath(raw)).resolve() if not candidate.is_absolute() else candidate.resolve())
    except ValueError as e:
        return f"⚠️ {e}"

    allowed_roots = [ctx.repo_dir.resolve(), ctx.drive_root.resolve(), pathlib.Path(tempfile.gettempdir()).resolve()]
    if not any(local_path == root or root in local_path.parents for root in allowed_roots):
        return "⚠️ path is outside allowed roots (repo, drive_root, tmp)."
    if not local_path.exists():
        return f"⚠️ local file not found: {local_path}"
    if not local_path.is_file():
        return f"⚠️ path is not a file: {local_path}"
    if local_path.stat().st_size <= 0:
        return f"⚠️ local file is empty: {local_path}"

    payload = local_path.read_bytes()
    safe_filename = (filename or local_path.name or "file.bin").strip() or "file.bin"
    mime = (mime_type or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream").strip()
    payload_b64 = base64.b64encode(payload).decode("ascii")
    archive_meta = save_artifact(
        ctx.drive_root,
        file_base64=payload_b64,
        filename=safe_filename,
        content_kind=("python" if pathlib.Path(safe_filename).suffix.lower() == ".py" else "html" if pathlib.Path(safe_filename).suffix.lower() in {".html", ".htm"} else "config" if pathlib.Path(safe_filename).suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"} else "table" if pathlib.Path(safe_filename).suffix.lower() in {".csv", ".tsv"} else "text" if pathlib.Path(safe_filename).suffix.lower() in {".txt", ".md", ".rst"} or mime.startswith("text/") else "generic"),
        source="send_local_file_tool",
        task_id=ctx.task_id or "",
        chat_id=chat_id,
        mime_type=mime,
        caption=caption or "",
        metadata={"source_path": str(local_path)},
    )

    event = {
        "type": "send_document",
        "chat_id": chat_id,
        "file_base64": payload_b64,
        "filename": safe_filename,
        "caption": caption or "",
        "mime_type": mime,
        "artifact_archive_path": archive_meta["relative_path"],
    }
    ctx.pending_events.append(event)
    return (
        f"✅ Local file queued for delivery: {safe_filename} "
        f"from {local_path} · archived at {archive_meta['relative_path']}"
    )



# ---------------------------------------------------------------------------
# Send photo to owner
# ---------------------------------------------------------------------------



def _send_document(
    ctx: ToolContext,
    file_base64: str = "",
    filename: str = "file.bin",
    caption: str = "",
    mime_type: str = "application/octet-stream",
    content: str = "",
) -> str:
    """Send a document/file to Telegram owner chat."""
    chat_id = int(ctx.current_chat_id or 0)
    if not chat_id:
        return "⚠️ No current chat available for send_document."

    payload_b64 = (file_base64 or "").strip()
    if content:
        payload_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    if not payload_b64:
        return "⚠️ send_document requires file_base64 or content."

    safe_filename = (filename or "").strip() or "file.bin"
    mime = mime_type or "application/octet-stream"
    archive_meta = save_artifact(
        ctx.drive_root,
        file_base64=payload_b64,
        filename=safe_filename,
        content_kind=("python" if pathlib.Path(safe_filename).suffix.lower() == ".py" else "html" if pathlib.Path(safe_filename).suffix.lower() in {".html", ".htm"} else "config" if pathlib.Path(safe_filename).suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"} else "table" if pathlib.Path(safe_filename).suffix.lower() in {".csv", ".tsv"} else "text" if pathlib.Path(safe_filename).suffix.lower() in {".txt", ".md", ".rst"} or mime.startswith("text/") else "generic"),
        source="send_document_tool",
        task_id=ctx.task_id or "",
        chat_id=chat_id,
        mime_type=mime,
        caption=caption or "",
    )

    event = {
        "type": "send_document",
        "chat_id": chat_id,
        "file_base64": payload_b64,
        "filename": safe_filename,
        "caption": caption or "",
        "mime_type": mime,
        "artifact_archive_path": archive_meta["relative_path"],
    }
    ctx.pending_events.append(event)
    return f"✅ Document queued for delivery: {safe_filename} · archived at {archive_meta['relative_path']}"


def _send_documents(ctx: ToolContext, files: List[Dict[str, Any]], caption: str = "") -> str:
    """Queue multiple documents for sequential Telegram delivery in one tool call."""
    chat_id = int(ctx.current_chat_id or 0)
    if not chat_id:
        return "⚠️ No current chat available for send_documents."

    if not isinstance(files, list) or not files:
        return "⚠️ send_documents requires a non-empty files list."

    prepared_files: List[Dict[str, str]] = []
    for idx, item in enumerate(files, start=1):
        if not isinstance(item, dict):
            return f"⚠️ send_documents item #{idx} must be an object."

        payload_b64 = str(item.get("file_base64") or "").strip()
        content = item.get("content")
        if isinstance(content, str) and content:
            payload_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

        if not payload_b64:
            return f"⚠️ send_documents item #{idx} requires file_base64 or content."

        safe_filename = str(item.get("filename") or "").strip() or f"file_{idx}.bin"
        mime = str(item.get("mime_type") or "application/octet-stream")
        archive_meta = save_artifact(
            ctx.drive_root,
            file_base64=payload_b64,
            filename=safe_filename,
            content_kind=("python" if pathlib.Path(safe_filename).suffix.lower() == ".py" else "html" if pathlib.Path(safe_filename).suffix.lower() in {".html", ".htm"} else "config" if pathlib.Path(safe_filename).suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"} else "table" if pathlib.Path(safe_filename).suffix.lower() in {".csv", ".tsv"} else "text" if pathlib.Path(safe_filename).suffix.lower() in {".txt", ".md", ".rst"} or mime.startswith("text/") else "generic"),
            source="send_documents_tool",
            task_id=ctx.task_id or "",
            chat_id=chat_id,
            mime_type=mime,
            caption=str(item.get("caption") or caption or ""),
            metadata={"batch_index": idx},
        )
        prepared_files.append({
            "file_base64": payload_b64,
            "filename": safe_filename,
            "caption": str(item.get("caption") or ""),
            "mime_type": mime,
            "artifact_archive_path": archive_meta["relative_path"],
        })

    event = {
        "type": "send_documents",
        "chat_id": chat_id,
        "caption": caption or "",
        "files": prepared_files,
    }
    ctx.pending_events.append(event)
    return f"✅ {len(prepared_files)} documents queued for sequential delivery · archived under artifacts/outbox"

def _send_photo(ctx: ToolContext, image_base64: str, caption: str = "") -> str:
    """Send a base64-encoded image to the owner's Telegram chat."""
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send photo."

    source = "raw_image"

    # Resolve screenshot reference from stash
    actual_b64 = image_base64
    if image_base64 == "__last_screenshot__":
        if not ctx.browser_state.last_screenshot_b64:
            return "⚠️ No screenshot stored. Take one first with browse_page(output='screenshot')."
        actual_b64 = ctx.browser_state.last_screenshot_b64
        source = "browser_last_screenshot"

    if not actual_b64 or len(actual_b64) < 100:
        return "⚠️ image_base64 is empty or too short. Take a screenshot first with browse_page(output='screenshot')."

    event = {
        "type": "send_photo",
        "chat_id": ctx.current_chat_id,
        "image_base64": actual_b64,
        "caption": caption or "",
        "source": source,
        "task_id": ctx.task_id or "",
        "task_type": ctx.current_task_type or "",
        "is_direct_chat": bool(ctx.is_direct_chat),
    }
    ctx.pending_events.append(event)
    return (
        "OK: photo queued for delivery to owner "
        f"(source={source}, chat_id={ctx.current_chat_id}, pending_events={len(ctx.pending_events)})."
    )

def _send_browser_screenshot(ctx: ToolContext, caption: str = "") -> str:
    """Capture the current browser page if possible, then send it to Telegram."""
    page = getattr(ctx.browser_state, "page", None)
    if page is not None:
        try:
            data = page.screenshot(type="png", full_page=False)
            ctx.browser_state.last_screenshot_b64 = base64.b64encode(data).decode()
        except Exception as e:
            return f"⚠️ Failed to capture browser screenshot: {e}"

    if not ctx.browser_state.last_screenshot_b64:
        return "⚠️ No screenshot stored and no active browser page. Open a page first with browse_page(...) or browser_action(...)."
    return _send_photo(ctx, image_base64="__last_screenshot__", caption=caption)


# ---------------------------------------------------------------------------
# Codebase digest
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
})


def _extract_python_symbols(file_path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Extract class and function names from a Python file using AST."""
    try:
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        classes = []
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
        return list(dict.fromkeys(classes)), list(dict.fromkeys(functions))
    except Exception:
        log.warning(f"Failed to extract Python symbols from {file_path}", exc_info=True)
        return [], []


def _codebase_digest(ctx: ToolContext) -> str:
    """Generate a compact digest of the codebase: files, sizes, classes, functions."""
    repo_dir = ctx.repo_dir
    py_files: List[pathlib.Path] = []
    md_files: List[pathlib.Path] = []
    other_files: List[pathlib.Path] = []

    for dirpath, dirnames, filenames in os.walk(str(repo_dir)):
        # Skip excluded directories
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            p = pathlib.Path(dirpath) / fn
            if not p.is_file():
                continue
            if p.suffix == ".py":
                py_files.append(p)
            elif p.suffix == ".md":
                md_files.append(p)
            elif p.suffix in (".txt", ".cfg", ".toml", ".yml", ".yaml", ".json"):
                other_files.append(p)

    total_lines = 0
    total_functions = 0
    sections: List[str] = []

    # Python files
    for pf in py_files:
        try:
            lines = pf.read_text(encoding="utf-8").splitlines()
            line_count = len(lines)
            total_lines += line_count
            classes, functions = _extract_python_symbols(pf)
            total_functions += len(functions)
            rel = pf.relative_to(repo_dir).as_posix()
            parts = [f"\n== {rel} ({line_count} lines) =="]
            if classes:
                cl = ", ".join(classes[:10])
                if len(classes) > 10:
                    cl += f", ... ({len(classes)} total)"
                parts.append(f"  Classes: {cl}")
            if functions:
                fn = ", ".join(functions[:20])
                if len(functions) > 20:
                    fn += f", ... ({len(functions)} total)"
                parts.append(f"  Functions: {fn}")
            sections.append("\n".join(parts))
        except Exception:
            log.debug(f"Failed to process Python file {pf} in codebase_digest", exc_info=True)
            pass

    # Markdown files
    for mf in md_files:
        try:
            line_count = len(mf.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = mf.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(f"Failed to process markdown file {mf} in codebase_digest", exc_info=True)
            pass

    # Other config files (just names + sizes)
    for of in other_files:
        try:
            line_count = len(of.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = of.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(f"Failed to process config file {of} in codebase_digest", exc_info=True)
            pass

    total_files = len(py_files) + len(md_files) + len(other_files)
    header = f"Codebase Digest ({total_files} files, {total_lines} lines, {total_functions} functions)"
    return header + "\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# Summarize dialogue
# ---------------------------------------------------------------------------

def _summarize_dialogue(ctx: ToolContext, last_n: int = 200) -> str:
    """Summarize dialogue history into key moments, decisions, and creator preferences."""
    from ouroboros.llm import LLMClient
    from ouroboros.model_modes import get_aux_light_model

    # Read last_n messages from chat.jsonl
    chat_path = ctx.drive_root / "logs" / "chat.jsonl"
    if not chat_path.exists():
        return "⚠️ chat.jsonl not found"

    try:
        entries = []
        with chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.debug("Failed to parse chat.jsonl line in summarize_dialogue", exc_info=True)
                        continue

        # Take last N entries
        entries = entries[-last_n:] if len(entries) > last_n else entries

        if not entries:
            return "⚠️ No chat entries found"

        # Format entries as text
        dialogue_text = []
        for entry in entries:
            ts = entry.get("ts", "")
            direction = entry.get("direction", "")
            role = "Creator" if direction == "in" else "Veles"
            text = entry.get("text", "")
            dialogue_text.append(f"[{ts}] {role}: {text}")

        formatted_dialogue = "\n".join(dialogue_text)

        # Build summarization prompt
        prompt = f"""Summarize the following dialogue history between the creator and Veles.

Extract:
1. Key decisions made (technical, architectural, strategic)
2. Creator's preferences and communication style
3. Important technical choices and their rationale
4. Recurring themes or patterns

For each key moment, include the timestamp.

Format as markdown with clear sections.

Dialogue history ({len(entries)} messages):

{formatted_dialogue}

Now write a comprehensive summary:"""

        # Call LLM
        llm = LLMClient()
        model = get_aux_light_model()

        messages = [
            {"role": "user", "content": prompt}
        ]

        response, usage = llm.chat(
            messages=messages,
            model=model,
            max_tokens=4096,
        )

        # Track cost in budget system
        if usage:
            usage_event = {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": ctx.task_id if ctx.task_id else "",
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cost": usage.get("cost", 0),
                },
                "category": "summarize",
            }
            if ctx.event_queue is not None:
                try:
                    ctx.event_queue.put_nowait(usage_event)
                except Exception:
                    if hasattr(ctx, "pending_events"):
                        ctx.pending_events.append(usage_event)
            elif hasattr(ctx, "pending_events"):
                ctx.pending_events.append(usage_event)

        summary = response.get("content", "")
        if not summary:
            return "⚠️ LLM returned empty summary"

        # Write to memory/dialogue_summary.md
        summary_path = ctx.drive_root / "memory" / "dialogue_summary.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary, encoding="utf-8")

        cost = float(usage.get("cost", 0))
        return f"OK: Summarized {len(entries)} messages. Written to memory/dialogue_summary.md. Cost: ${cost:.4f}\n\n{summary[:500]}..."

    except Exception as e:
        log.warning("Failed to summarize dialogue", exc_info=True)
        return f"⚠️ Error: {repr(e)}"


# ---------------------------------------------------------------------------
# forward_to_worker — LLM-initiated message routing to worker tasks
# ---------------------------------------------------------------------------

def _forward_to_worker(ctx: ToolContext, task_id: str, message: str) -> str:
    """Forward a message to a running worker task's mailbox."""
    from ouroboros.owner_inject import write_owner_message
    write_owner_message(ctx.drive_root, message, task_id=task_id, msg_id=uuid.uuid4().hex)
    return f"Message forwarded to task {task_id}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("repo_read", {
            "name": "repo_read",
            "description": "Read a UTF-8 text file from the GitHub repo (relative path).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }, partial(_read_from, which="repo_path")),
        ToolEntry("repo_list", {
            "name": "repo_list",
            "description": "List files under a repo directory (relative path).",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, partial(_list_from, root_attr="repo_dir")),
        ToolEntry("drive_read", {
            "name": "drive_read",
            "description": "Read a UTF-8 text file from Google Drive (relative to MyDrive/Ouroboros/).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }, partial(_read_from, which="drive_path")),
        ToolEntry("drive_list", {
            "name": "drive_list",
            "description": "List files under a Drive directory.",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, partial(_list_from, root_attr="drive_root")),
        ToolEntry("drive_write", {
            "name": "drive_write",
            "description": "Write a UTF-8 text file on Google Drive.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
            }, "required": ["path", "content"]},
        }, _drive_write),

        ToolEntry("save_artifact", {
            "name": "save_artifact",
            "description": "Сохранить локальный текстовый артефакт в постоянное хранилище под drive_root/artifacts/outbox. Подходит для Python-кода, планов, markdown и txt, чтобы потом можно было к ним вернуться.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Текст артефакта"},
                "filename": {"type": "string", "description": "Имя файла, например plan.md или solution.py"},
                "content_kind": {"type": "string", "description": "Категория: python, text, plan, markdown, html, config, generic"},
                "mime_type": {"type": "string", "description": "MIME-тип артефакта"},
                "note": {"type": "string", "description": "Короткая заметка о происхождении артефакта"}
            }, "required": ["content", "filename"]},
        }, save_artifact),

        ToolEntry("list_incoming_artifacts", {
            "name": "list_incoming_artifacts",
            "description": "Показать последние входящие файлы, сохранённые в artifacts/inbox. Используется для сценариев вроде: посмотреть 10 последних загруженных файлов и потом явно выбрать, что с ними делать.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Сколько последних файлов показать", "default": 10},
                "chat_id": {"type": "integer", "description": "Необязательный chat_id для фильтрации"},
                "content_kind": {"type": "string", "description": "Фильтр по content_kind, например incoming или pdf"},
                "filename_contains": {"type": "string", "description": "Подстрока в имени файла"}
            }, "required": []},
        }, list_incoming_artifacts),

        ToolEntry("send_local_file", {
            "name": "send_local_file",
            "description": "Отправить существующий локальный файл владельцу в Telegram без ручного base64. Разрешены только пути внутри repo, drive_root и системного tmp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к локальному файлу"},
                    "caption": {"type": "string", "description": "Подпись к файлу"},
                    "filename": {"type": "string", "description": "Необязательное имя файла при отправке"},
                    "mime_type": {"type": "string", "description": "Необязательный MIME-тип; по умолчанию определяется по имени файла"}
                },
                "required": ["path"],
            },
        }, _send_local_file),
        ToolEntry("send_document", {
            "name": "send_document",
            "description": "Отправить файл (document) владельцу в Telegram.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_base64": {"type": "string", "description": "Содержимое файла в base64"},
                    "filename": {"type": "string", "description": "Имя файла (например report.py)"},
                    "caption": {"type": "string", "description": "Подпись к файлу"},
                    "mime_type": {"type": "string", "description": "MIME-тип файла"},
                    "content": {"type": "string", "description": "Текст файла; если передан, будет закодирован в base64 автоматически"},
                },
                "required": [],
            },
        }, _send_document),
        ToolEntry("send_documents", {
            "name": "send_documents",
            "description": "Отправить несколько файлов владельцу в Telegram за один tool-вызов. Файлы будут отправлены последовательно одной операцией.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "Список файлов для отправки",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_base64": {"type": "string", "description": "Содержимое файла в base64"},
                                "filename": {"type": "string", "description": "Имя файла"},
                                "caption": {"type": "string", "description": "Подпись к конкретному файлу"},
                                "mime_type": {"type": "string", "description": "MIME-тип файла"},
                                "content": {"type": "string", "description": "Текст файла; если передан, будет закодирован в base64 автоматически"}
                            },
                            "required": []
                        }
                    },
                    "caption": {"type": "string", "description": "Общая подпись по умолчанию для файлов без собственного caption"}
                },
                "required": ["files"],
            },
        }, _send_documents),
        ToolEntry("send_photo", {
            "name": "send_photo",
            "description": (
                "Send a base64-encoded image (PNG) to the owner's Telegram chat. "
                "Use after browse_page(output='screenshot') or browser_action(action='screenshot'). "
                "Pass the base64 string from the screenshot result as image_base64."
            ),
            "parameters": {"type": "object", "properties": {
                "image_base64": {"type": "string", "description": "Base64-encoded PNG image data"},
                "caption": {"type": "string", "description": "Optional caption for the photo"},
            }, "required": ["image_base64"]},
        }, _send_photo),
        ToolEntry("send_browser_screenshot", {
            "name": "send_browser_screenshot",
            "description": (
                "Send the last browser screenshot directly to the owner's Telegram chat. "
                "Use after browse_page(output='screenshot') or browser_action(action='screenshot')."
            ),
            "parameters": {"type": "object", "properties": {
                "caption": {"type": "string", "description": "Optional caption for the screenshot"},
            }, "required": []},
        }, _send_browser_screenshot),
        ToolEntry("codebase_digest", {
            "name": "codebase_digest",
            "description": "Get a compact digest of the entire codebase: files, sizes, classes, functions. One call instead of many repo_read calls.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _codebase_digest),
        ToolEntry("summarize_dialogue", {
            "name": "summarize_dialogue",
            "description": "Summarize dialogue history into key moments, decisions, and creator preferences. Writes to memory/dialogue_summary.md.",
            "parameters": {"type": "object", "properties": {
                "last_n": {"type": "integer", "description": "Number of recent messages to summarize (default 200)"},
            }, "required": []},
        }, _summarize_dialogue),
        ToolEntry("forward_to_worker", {
            "name": "forward_to_worker",
            "description": (
                "Forward a message to a running worker task's mailbox. "
                "Use when the owner sends a message during your active conversation "
                "that is relevant to a specific running background task. "
                "The worker will see it as [Owner message during task] on its next LLM round."
            ),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string", "description": "ID of the running task to forward to"},
                "message": {"type": "string", "description": "Message text to forward"},
            }, "required": ["task_id", "message"]},
        }, _forward_to_worker),
    ]

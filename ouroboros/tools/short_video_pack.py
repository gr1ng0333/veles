from __future__ import annotations

import importlib.util
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from ouroboros.artifacts import save_artifact
from ouroboros.tools.core import _send_documents, _send_local_file
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import safe_relpath

_DEFAULT_MAX_ITEMS = 10
_HARD_MAX_ITEMS = 20
_ALLOWED_DELIVERY_MODES = {"documents", "zip"}
_DEFAULT_SOURCE = "short_video_pack_download"
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@dataclass
class NormalizedItem:
    index: int
    url: str
    source: str
    title: str
    notes: str


@dataclass
class DownloadedItem:
    normalized: NormalizedItem
    status: str
    filename: str = ""
    file_path: str = ""
    artifact_path: str = ""
    error: str = ""


class ContractError(ValueError):
    pass


def _slugify(text: str, fallback: str = "clip") -> str:
    raw = str(text or "").strip().lower()
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9._-]+", "-", raw)).strip("-._")
    return slug or fallback


def _normalize_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def _safe_manifest_path(ctx: ToolContext, raw: str) -> pathlib.Path:
    candidate = pathlib.Path(str(raw or "").strip()).expanduser()
    allowed_roots = [ctx.repo_dir.resolve(), ctx.drive_root.resolve(), pathlib.Path(tempfile.gettempdir()).resolve()]
    if candidate.is_absolute():
        path = candidate.resolve()
        if not any(path == root or root in path.parents for root in allowed_roots):
            raise ContractError("manifest_path is outside allowed roots (repo, drive_root, tmp)")
        return path
    rel = safe_relpath(str(raw))
    repo_path = (ctx.repo_dir / rel).resolve()
    drive_path = (ctx.drive_root / rel).resolve()
    for path in (repo_path, drive_path):
        if any(path == root or root in path.parents for root in allowed_roots):
            if path.exists():
                return path
    return repo_path


def _load_manifest(ctx: ToolContext, *, items: Optional[List[Dict[str, Any]]], manifest_path: str) -> List[Dict[str, Any]]:
    has_items = items is not None
    has_manifest = bool(str(manifest_path or "").strip())
    if has_items == has_manifest:
        raise ContractError("Provide exactly one manifest source: either items or manifest_path.")
    if has_items:
        if not isinstance(items, list):
            raise ContractError("items must be a JSON array.")
        return items
    path = _safe_manifest_path(ctx, manifest_path)
    if not path.exists() or not path.is_file():
        raise ContractError(f"manifest_path not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ContractError(f"manifest_path is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ContractError("manifest file must contain a JSON array.")
    return payload


def _normalize_items(raw_items: List[Dict[str, Any]], *, max_items: int, dedupe: bool) -> tuple[list[NormalizedItem], int]:
    normalized: List[NormalizedItem] = []
    seen: set[str] = set()
    dropped = 0
    for idx, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            dropped += 1
            continue
        url = _normalize_url(str(raw.get("url") or ""))
        if not url:
            dropped += 1
            continue
        if dedupe and url in seen:
            dropped += 1
            continue
        seen.add(url)
        normalized.append(
            NormalizedItem(
                index=len(normalized) + 1,
                url=url,
                source=str(raw.get("source") or "tiktok").strip() or "tiktok",
                title=str(raw.get("title") or "").strip(),
                notes=str(raw.get("notes") or "").strip(),
            )
        )
        if len(normalized) >= max_items:
            break
    if not normalized:
        raise ContractError("All manifest items are empty, invalid, or removed by dedupe/max_items.")
    return normalized, dropped


def _pick_downloaded_file(target_dir: pathlib.Path) -> Optional[pathlib.Path]:
    candidates = [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in _VIDEO_EXTENSIONS and p.stat().st_size > 0]
    if not candidates:
        candidates = [p for p in target_dir.iterdir() if p.is_file() and p.stat().st_size > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
    return candidates[0]


def _resolve_yt_dlp() -> List[str]:
    python_exe = shutil.which("python") or shutil.which("python3")
    if python_exe and importlib.util.find_spec("yt_dlp") is not None:
        return [python_exe, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    raise RuntimeError("yt-dlp is not installed in the runtime.")


def _build_output_filename(item: NormalizedItem, source_index: int, downloaded_path: pathlib.Path) -> str:
    base = _slugify(item.title or item.notes or f"clip-{source_index:02d}", fallback=f"clip-{source_index:02d}")
    ext = downloaded_path.suffix.lower() or ".mp4"
    if len(ext) > 8:
        ext = ".mp4"
    return f"{source_index:02d}-{base}{ext}"


def _download_one(item: NormalizedItem, workspace: pathlib.Path, downloader_cmd: List[str]) -> DownloadedItem:
    item_dir = workspace / f"item-{item.index:02d}"
    item_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(item_dir / "downloaded.%(ext)s")
    cmd = [
        *downloader_cmd,
        "--no-playlist",
        "--restrict-filenames",
        "--no-progress",
        "--no-warnings",
        "-o",
        output_template,
        item.url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        error = (proc.stderr or proc.stdout or "yt-dlp returned non-zero exit").strip()
        return DownloadedItem(normalized=item, status="failed", error=error[:400])
    file_path = _pick_downloaded_file(item_dir)
    if file_path is None:
        return DownloadedItem(normalized=item, status="failed", error="download finished but no non-empty file was produced")
    final_name = _build_output_filename(item, item.index, file_path)
    final_path = workspace / final_name
    if final_path.exists():
        final_path.unlink()
    file_path.replace(final_path)
    return DownloadedItem(
        normalized=item,
        status="ok",
        filename=final_name,
        file_path=str(final_path),
    )


def _archive_manifest(ctx: ToolContext, *, raw_items: List[Dict[str, Any]], normalized_items: List[NormalizedItem]) -> str:
    payload = {
        "source": _DEFAULT_SOURCE,
        "raw_items": raw_items,
        "normalized_items": [
            {
                "index": item.index,
                "url": item.url,
                "source": item.source,
                "title": item.title,
                "notes": item.notes,
            }
            for item in normalized_items
        ],
    }
    archived = save_artifact(
        ctx,
        filename="short-video-manifest.json",
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        content_kind="plan",
        source=_DEFAULT_SOURCE,
        mime_type="application/json",
        caption="short-video pack manifest",
    )
    if isinstance(archived, dict):
        return str(archived.get("relative_path") or "")
    return ""



def _archive_download_artifacts(ctx: ToolContext, downloaded: List[DownloadedItem]) -> None:
    for item in downloaded:
        if item.status != "ok" or not item.file_path:
            continue
        file_path = pathlib.Path(item.file_path)
        if not file_path.exists() or not file_path.is_file():
            item.status = "failed"
            item.error = "downloaded file disappeared before artifact archival"
            item.filename = ""
            item.file_path = ""
            item.artifact_path = ""
            continue
        mime = mimetypes.guess_type(item.filename)[0] or "video/mp4"
        archived = save_artifact(
            ctx,
            filename=item.filename,
            data=file_path.read_bytes(),
            content_kind="video",
            source=_DEFAULT_SOURCE,
            mime_type=mime,
            caption="short-video pack item",
            metadata={
                "url": item.normalized.url,
                "index": item.normalized.index,
                "title": item.normalized.title,
            },
        )
        if isinstance(archived, dict):
            item.artifact_path = str(archived.get("relative_path") or "")


def _zip_downloads(workspace: pathlib.Path, downloaded: List[DownloadedItem]) -> pathlib.Path:
    zip_path = workspace / "short-video-pack.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in downloaded:
            if item.status == "ok":
                file_path = pathlib.Path(item.file_path)
                zf.write(file_path, arcname=item.filename)
    return zip_path


def _deliver_downloads(ctx: ToolContext, *, downloaded: List[DownloadedItem], delivery_mode: str) -> Dict[str, Any]:
    successful = [item for item in downloaded if item.status == "ok"]
    if not successful:
        return {"mode": delivery_mode, "sent_files": 0, "zip_path": "", "tool_result": ""}
    if delivery_mode == "zip":
        zip_path = _zip_downloads(pathlib.Path(successful[0].file_path).parent, successful)
        archived_zip = save_artifact(
            ctx,
            filename=zip_path.name,
            data=zip_path.read_bytes(),
            content_kind="archive",
            source=_DEFAULT_SOURCE,
            mime_type="application/zip",
            caption="short-video pack",
            metadata={"items": len(successful)},
        )
        archived_rel = str(archived_zip.get("relative_path") or "") if isinstance(archived_zip, dict) else ""
        send_path = str((ctx.drive_root / archived_rel).resolve()) if archived_rel else str(zip_path)
        result = _send_local_file(
            ctx,
            path=send_path,
            filename=zip_path.name,
            mime_type="application/zip",
            caption="short-video pack",
        )
        return {"mode": "zip", "sent_files": len(successful), "zip_path": archived_rel, "tool_result": result}
    files = []
    import base64
    for item in successful:
        source_path = pathlib.Path(ctx.drive_root / item.artifact_path) if item.artifact_path else pathlib.Path(item.file_path)
        mime = mimetypes.guess_type(item.filename)[0] or "video/mp4"
        files.append({
            "filename": item.filename,
            "file_base64": base64.b64encode(source_path.read_bytes()).decode("ascii"),
            "mime_type": mime,
            "caption": "",
        })
    result = _send_documents(ctx, files=files, caption="short-video pack")
    return {"mode": "documents", "sent_files": len(successful), "zip_path": "", "tool_result": result}


def _build_result(*, raw_items: List[Dict[str, Any]], normalized_items: List[NormalizedItem], downloaded: List[DownloadedItem], delivery: Dict[str, Any], manifest_archive_path: str, dropped: int) -> Dict[str, Any]:
    success_count = sum(1 for item in downloaded if item.status == "ok")
    failed_count = sum(1 for item in downloaded if item.status != "ok")
    if success_count and failed_count:
        status = "partial"
    elif success_count:
        status = "ok"
    else:
        status = "failed"
    return {
        "status": status,
        "requested": len(raw_items),
        "normalized": len(normalized_items),
        "dropped": dropped,
        "processed": len(downloaded),
        "downloaded": success_count,
        "failed": failed_count,
        "manifest_archive_path": manifest_archive_path,
        "delivery": {
            "mode": delivery.get("mode") or "",
            "sent_files": delivery.get("sent_files") or 0,
            "zip_path": delivery.get("zip_path") or "",
            "tool_result": delivery.get("tool_result") or "",
        },
        "items": [
            {
                "index": item.normalized.index,
                "url": item.normalized.url,
                "status": item.status,
                "file_path": "",
                "artifact_path": item.artifact_path,
                "filename": item.filename,
                "error": item.error,
            }
            for item in downloaded
        ],
    }


def _short_video_pack_download(
    ctx: ToolContext,
    items: Optional[List[Dict[str, Any]]] = None,
    manifest_path: str = "",
    delivery_mode: str = "documents",
    max_items: int = _DEFAULT_MAX_ITEMS,
    continue_on_error: bool = True,
    dedupe: bool = True,
    archive_manifest: bool = True,
) -> str:
    try:
        raw_items = _load_manifest(ctx, items=items, manifest_path=manifest_path)
        chosen_mode = str(delivery_mode or "documents").strip().lower()
        if chosen_mode not in _ALLOWED_DELIVERY_MODES:
            raise ContractError(f"delivery_mode must be one of: {', '.join(sorted(_ALLOWED_DELIVERY_MODES))}")
        safe_max_items = max(1, min(int(max_items or _DEFAULT_MAX_ITEMS), _HARD_MAX_ITEMS))
        normalized_items, dropped = _normalize_items(raw_items, max_items=safe_max_items, dedupe=bool(dedupe))
    except ContractError as exc:
        return json.dumps({
            "status": "failed",
            "error_kind": "contract",
            "error": str(exc),
        }, ensure_ascii=False, indent=2)

    manifest_archive_path = _archive_manifest(ctx, raw_items=raw_items, normalized_items=normalized_items) if archive_manifest else ""

    try:
        downloader_cmd = _resolve_yt_dlp()
    except RuntimeError as exc:
        return json.dumps({
            "status": "failed",
            "error_kind": "backend_unavailable",
            "error": str(exc),
            "requested": len(raw_items),
            "normalized": len(normalized_items),
            "manifest_archive_path": manifest_archive_path,
        }, ensure_ascii=False, indent=2)

    workspace = pathlib.Path(tempfile.mkdtemp(prefix="short-video-pack-"))
    downloaded: List[DownloadedItem] = []
    try:
        for item in normalized_items:
            result = _download_one(item, workspace, downloader_cmd)
            downloaded.append(result)
            if result.status != "ok" and not continue_on_error:
                break
        _archive_download_artifacts(ctx, downloaded)
        delivery = _deliver_downloads(ctx, downloaded=downloaded, delivery_mode=chosen_mode)
        return json.dumps(
            _build_result(
                raw_items=raw_items,
                normalized_items=normalized_items,
                downloaded=downloaded,
                delivery=delivery,
                manifest_archive_path=manifest_archive_path,
                dropped=dropped,
            ),
            ensure_ascii=False,
            indent=2,
        )
    finally:
        if os.environ.get("VELES_KEEP_SHORT_VIDEO_TMP") != "1":
            shutil.rmtree(workspace, ignore_errors=True)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "short_video_pack_download",
            {
                "name": "short_video_pack_download",
                "description": (
                    "Download a curated pack of short-video URLs from a provided manifest, "
                    "continue on per-item failures, and optionally deliver results to Telegram as documents or zip."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "description": "Inline manifest array. Mutually exclusive with manifest_path.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string"},
                                    "source": {"type": "string"},
                                    "title": {"type": "string"},
                                    "notes": {"type": "string"},
                                },
                                "required": ["url"],
                            },
                        },
                        "manifest_path": {
                            "type": "string",
                            "description": "Path to a JSON array manifest inside repo, drive_root, or tmp. Mutually exclusive with items.",
                        },
                        "delivery_mode": {
                            "type": "string",
                            "enum": ["documents", "zip"],
                            "description": "How to deliver successful downloads to Telegram.",
                            "default": "documents",
                        },
                        "max_items": {
                            "type": "integer",
                            "description": "Maximum number of items to process after normalization/dedupe (hard cap 20).",
                            "default": 10,
                        },
                        "continue_on_error": {
                            "type": "boolean",
                            "description": "Keep processing remaining items when one URL fails.",
                            "default": True,
                        },
                        "dedupe": {
                            "type": "boolean",
                            "description": "Deduplicate by normalized URL while preserving first occurrence order.",
                            "default": True,
                        },
                        "archive_manifest": {
                            "type": "boolean",
                            "description": "Archive the input manifest into artifacts/outbox for provenance.",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
            _short_video_pack_download,
            timeout_sec=900,
        )
    ]

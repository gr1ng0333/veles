"""notes — personal note-taking and retrieval tool.

Allows saving tagged notes while reading sources (HN, Reddit, arXiv, TG channels),
then finding them again via memory_search or note_search.

Notes are stored in /opt/veles-data/memory/notes.jsonl (append-only log).
Each note has: id, timestamp, text, tags, source (optional URL or channel name).

Tools:
    note_add(text, tags?, source?)   — save a note with optional tags and source URL
    note_search(query, tags?, limit?)— full-text search over saved notes
    note_list(tags?, limit?, since_days?)  — list recent notes, optionally filtered by tag
    note_delete(note_id)             — soft-delete a note by id

Usage:
    note_add(text="Reward model breaks on colon prefix", tags=["ml", "research"], source="https://t.me/abstractDL/402")
    note_search(query="reward model")
    note_list(tags=["ml"], limit=10)
    note_list(since_days=7)
    note_delete(note_id="20260404_abc123")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DRIVE_ROOT = os.environ.get("DRIVE_ROOT", "/opt/veles-data")
_NOTES_FILE = "memory/notes.jsonl"


# ── Persistence ────────────────────────────────────────────────────────────────

def _notes_path() -> pathlib.Path:
    return pathlib.Path(_DRIVE_ROOT) / _NOTES_FILE


def _load_notes() -> List[Dict[str, Any]]:
    """Load all non-deleted notes from the JSONL file."""
    path = _notes_path()
    if not path.exists():
        return []
    notes = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                note = json.loads(line)
                if not note.get("deleted", False):
                    notes.append(note)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return notes


def _append_note(note: Dict[str, Any]) -> None:
    """Append a single note record to the JSONL file."""
    path = _notes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")


def _note_id(text: str, ts: str) -> str:
    """Deterministic short ID: date prefix + hash."""
    date = ts[:10].replace("-", "")
    h = hashlib.sha256(f"{ts}{text}".encode()).hexdigest()[:6]
    return f"{date}_{h}"


# ── Text search ────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-zA-Zа-яёА-ЯЁ0-9]{2,}", text.lower())


def _score(note: Dict[str, Any], query_tokens: List[str]) -> float:
    """Simple token-overlap score (count of query tokens present in note text)."""
    text = (note.get("text", "") + " " + " ".join(note.get("tags", []))).lower()
    note_tokens = set(_tokenize(text))
    if not query_tokens:
        return 1.0
    return sum(1 for t in query_tokens if t in note_tokens) / len(query_tokens)


# ── Tool implementations ────────────────────────────────────────────────────────

def _note_add(
    ctx: ToolContext,
    text: str,
    tags: Optional[List[str]] = None,
    source: Optional[str] = None,
) -> str:
    """Save a note with optional tags and source URL."""
    text = text.strip()
    if not text:
        return "❌ Note text cannot be empty"
    if len(text) > 10_000:
        return "❌ Note too long (max 10,000 chars)"

    ts = datetime.now(tz=timezone.utc).isoformat()
    note_id = _note_id(text, ts)

    note: Dict[str, Any] = {
        "id": note_id,
        "timestamp": ts,
        "text": text,
    }
    if tags:
        note["tags"] = [t.strip().lower() for t in tags if t.strip()]
    if source:
        note["source"] = source.strip()

    _append_note(note)

    tag_str = f" [{', '.join(note.get('tags', []))}]" if note.get("tags") else ""
    src_str = f"\n🔗 {source}" if source else ""
    return f"✅ Note saved: `{note_id}`{tag_str}{src_str}"


def _note_search(
    ctx: ToolContext,
    query: str,
    tags: Optional[List[str]] = None,
    limit: int = 20,
) -> str:
    """Full-text search over saved notes."""
    if not query.strip():
        return "❌ Query cannot be empty"
    if limit < 1 or limit > 200:
        limit = 20

    notes = _load_notes()
    if not notes:
        return "📭 No notes saved yet. Use `note_add` to save notes."

    # Tag filter
    if tags:
        filter_tags = {t.strip().lower() for t in tags if t.strip()}
        notes = [n for n in notes if filter_tags.issubset(set(n.get("tags", [])))]

    query_tokens = _tokenize(query)
    scored = [(n, _score(n, query_tokens)) for n in notes]
    scored = [(n, s) for n, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)

    if not scored:
        return f"🔍 No notes found for: `{query}`"

    results = scored[:limit]
    lines = [f"🔍 Found {len(scored)} note(s) for `{query}` (showing {min(len(scored), limit)}):\n"]
    for note, score in results:
        ts = note["timestamp"][:16].replace("T", " ")
        tag_str = f" [{', '.join(note['tags'])}]" if note.get("tags") else ""
        src_str = f"\n   🔗 {note['source']}" if note.get("source") else ""
        text_preview = note["text"][:300] + ("…" if len(note["text"]) > 300 else "")
        lines.append(f"**{note['id']}** ({ts}){tag_str}")
        lines.append(f"   {text_preview}{src_str}")
        lines.append("")

    return "\n".join(lines)


def _note_list(
    ctx: ToolContext,
    tags: Optional[List[str]] = None,
    limit: int = 20,
    since_days: Optional[float] = None,
) -> str:
    """List recent notes, optionally filtered by tag or time window."""
    if limit < 1 or limit > 200:
        limit = 20

    notes = _load_notes()
    if not notes:
        return "📭 No notes saved yet. Use `note_add` to save notes."

    # Time filter
    if since_days is not None and since_days > 0:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
        notes = [
            n for n in notes
            if datetime.fromisoformat(n["timestamp"]) >= cutoff
        ]

    # Tag filter
    if tags:
        filter_tags = {t.strip().lower() for t in tags if t.strip()}
        notes = [n for n in notes if filter_tags.issubset(set(n.get("tags", [])))]

    # Sort newest-first
    notes.sort(key=lambda n: n["timestamp"], reverse=True)
    total = len(notes)
    notes = notes[:limit]

    if not notes:
        return "📭 No notes match the filter."

    lines = [f"📝 {total} note(s) total, showing {len(notes)}:\n"]
    for note in notes:
        ts = note["timestamp"][:16].replace("T", " ")
        tag_str = f" [{', '.join(note['tags'])}]" if note.get("tags") else ""
        src_str = f"\n   🔗 {note['source']}" if note.get("source") else ""
        text_preview = note["text"][:200] + ("…" if len(note["text"]) > 200 else "")
        lines.append(f"**{note['id']}** ({ts}){tag_str}")
        lines.append(f"   {text_preview}{src_str}")
        lines.append("")

    return "\n".join(lines)


def _note_delete(ctx: ToolContext, note_id: str) -> str:
    """Soft-delete a note by id."""
    note_id = note_id.strip()
    if not note_id:
        return "❌ note_id cannot be empty"

    notes = _load_notes()
    matched = [n for n in notes if n["id"] == note_id]
    if not matched:
        return f"❌ Note not found: `{note_id}`"

    # Append tombstone record
    tombstone = {
        "id": note_id,
        "deleted": True,
        "deleted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    _append_note(tombstone)
    return f"🗑️ Note `{note_id}` deleted."


# ── Tool registry ──────────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="note_add",
            description=(
                "Save a personal note with optional tags and source URL. "
                "Use while reading HN/Reddit/arXiv/TG to bookmark interesting items. "
                "Notes are searchable via note_search and memory_search."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Note content (max 10,000 chars)"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tags, e.g. ['ml', 'research', 'todo']",
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional source URL or reference, e.g. 'https://t.me/abstractDL/402'",
                    },
                },
                "required": ["text"],
            },
            execute=lambda ctx, **kw: _note_add(ctx, **kw),
        ),
        ToolEntry(
            name="note_search",
            description=(
                "Full-text search over saved notes. "
                "Returns notes ranked by relevance to the query."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tag filter — return only notes with ALL these tags",
                    },
                    "limit": {"type": "integer", "description": "Max results to return (default 20)"},
                },
                "required": ["query"],
            },
            execute=lambda ctx, **kw: _note_search(ctx, **kw),
        ),
        ToolEntry(
            name="note_list",
            description=(
                "List recent saved notes. Optionally filter by tag(s) or time window."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tag filter",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                    "since_days": {
                        "type": "number",
                        "description": "Only show notes from the last N days",
                    },
                },
                "required": [],
            },
            execute=lambda ctx, **kw: _note_list(ctx, **kw),
        ),
        ToolEntry(
            name="note_delete",
            description="Soft-delete a note by its id.",
            parameters={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string", "description": "Note id, e.g. '20260404_abc123'"},
                },
                "required": ["note_id"],
            },
            execute=lambda ctx, **kw: _note_delete(ctx, **kw),
        ),
    ]

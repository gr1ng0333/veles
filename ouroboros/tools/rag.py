"""RAG (Retrieval-Augmented Generation) — document indexing and search.

Parses local files into chunks, builds a BM25 index, and retrieves
the most relevant fragments for a query. No external dependencies —
pure Python, same BM25 engine as memory_search.

Supported file types:
  .txt .md .rst .tex .yaml .yml .toml .ini .cfg .conf .env
  .py .js .ts .go .rs .java .c .cpp .h .rb .php .swift .kt
  .lua .r .sh .bash .sql .html .css .xml .json .csv .tsv .log
  .pdf  (pdfminer.six — text layer only)
  .docx .doc  (python-docx)

Collections are stored under:
  {drive_root}/rag/{collection_name}/
    chunks.json   — list of {text, source, page, chunk_idx}
    meta.json     — {created_at, file_count, chunk_count, files: [...]}

Usage:
    rag_index(paths=["/opt/docs/task1.pdf", "/opt/docs/task2.docx"],
              collection="tasks")
    rag_query(query="динамическое программирование", collection="tasks", top_k=5)
    rag_list()
    rag_delete(collection="tasks")
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import re
import textwrap
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CHUNK_SIZE = 600          # chars
CHUNK_OVERLAP = 100       # chars
MIN_CHUNK_TOKENS = 4      # discard tiny fragments

# BM25 params
BM25_K1 = 1.5
BM25_B = 0.75

# Stop-words (Russian + English)
_STOP_WORDS = frozenset("""
а б в г д е ё ж з и й к л м н о п р с т у ф х ц ч ш щ ъ ы ь э ю я
это как что для так да нет не из по на но или то есть от там где когда
если при уже было были были также через между после перед
the a an in on at to of and or is are was were be been being have has
had do does did will would could should may might shall can cannot with
that this they them their which from for into by up out about over just
""".split())

# ── Tokenizer ─────────────────────────────────────────────────────────────────


def _tokenize(text: str) -> List[str]:
    """Lowercase, extract word tokens ≥2 chars, remove stop-words."""
    text = text.lower()
    tokens = re.findall(r"[a-zа-яёa-z0-9_][a-zа-яёa-z0-9_]{1,}", text)
    return [t for t in tokens if t not in _STOP_WORDS]


# ── File parsers ──────────────────────────────────────────────────────────────


def _parse_text(path: pathlib.Path) -> str:
    """Read plain text / code / config files."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning("rag: cannot read %s: %s", path, exc)
        return ""


def _parse_pdf(path: pathlib.Path) -> str:
    """Extract text from PDF via pdfminer.six (text layer only)."""
    try:
        from pdfminer.high_level import extract_text  # type: ignore
        text = extract_text(str(path))
        return text or ""
    except ImportError:
        log.warning("rag: pdfminer.six not installed — cannot parse PDF")
        return f"[PDF parsing unavailable: install pdfminer.six to index {path.name}]"
    except Exception as exc:
        log.warning("rag: PDF parse error %s: %s", path, exc)
        return ""


def _parse_docx(path: pathlib.Path) -> str:
    """Extract text from .docx via python-docx."""
    try:
        import docx  # type: ignore
        doc = docx.Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        log.warning("rag: python-docx not installed — cannot parse DOCX")
        return f"[DOCX parsing unavailable: install python-docx to index {path.name}]"
    except Exception as exc:
        log.warning("rag: DOCX parse error %s: %s", path, exc)
        return ""


_TEXT_EXTS = frozenset("""
.txt .md .rst .tex .yaml .yml .toml .ini .cfg .conf .env
.py .js .ts .go .rs .java .c .cpp .h .rb .php .swift .kt
.lua .r .sh .bash .sql .html .css .xml .json .csv .tsv .log
""".split())


def _parse_file(path: pathlib.Path) -> str:
    """Dispatch to the right parser by file extension."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in (".docx", ".doc"):
        return _parse_docx(path)
    if ext in _TEXT_EXTS or not ext:
        return _parse_text(path)
    # Try as text anyway
    return _parse_text(path)


# ── Chunker ───────────────────────────────────────────────────────────────────


def _chunk_text(
    text: str,
    source: str,
    page: int = 0,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> List[Dict[str, Any]]:
    """Split text into overlapping chunks. Returns list of chunk dicts."""
    if not text.strip():
        return []

    # Prefer splitting on paragraph boundaries when possible
    paragraphs = re.split(r"\n\s*\n", text)
    buffer = ""
    chunks: List[Dict[str, Any]] = []
    idx = 0

    def _emit(buf: str) -> None:
        nonlocal idx
        buf = buf.strip()
        if not buf:
            return
        tokens = _tokenize(buf)
        if len(tokens) < MIN_CHUNK_TOKENS:
            return
        chunks.append({
            "text": buf,
            "source": source,
            "page": page,
            "chunk_idx": idx,
        })
        idx += 1

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # If single paragraph is huge, slice it directly
        if len(para) > chunk_size:
            # Flush buffer first
            if buffer:
                _emit(buffer)
                buffer = buffer[-overlap:] if overlap else ""
            step = chunk_size - overlap
            for i in range(0, len(para), step):
                slice_ = para[i: i + chunk_size]
                _emit(slice_)
            continue

        # Accumulate into buffer
        candidate = (buffer + "\n\n" + para).strip() if buffer else para
        if len(candidate) > chunk_size and buffer:
            _emit(buffer)
            buffer = buffer[-overlap:] + "\n\n" + para if overlap else para
        else:
            buffer = candidate

    if buffer:
        _emit(buffer)

    return chunks


# ── Collection storage ────────────────────────────────────────────────────────


def _collection_dir(drive_root: pathlib.Path, collection: str) -> pathlib.Path:
    _validate_collection_name(collection)
    return drive_root / "rag" / collection


def _validate_collection_name(name: str) -> None:
    if not name or not re.match(r"^[a-zA-Z0-9_\-]{1,64}$", name):
        raise ValueError(
            f"Invalid collection name '{name}'. "
            "Use alphanumeric, hyphens, underscores. Max 64 chars."
        )


def _load_chunks(col_dir: pathlib.Path) -> List[Dict[str, Any]]:
    chunks_file = col_dir / "chunks.json"
    if not chunks_file.exists():
        return []
    try:
        return json.loads(chunks_file.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("rag: cannot load chunks from %s: %s", col_dir, exc)
        return []


def _load_meta(col_dir: pathlib.Path) -> Dict[str, Any]:
    meta_file = col_dir / "meta.json"
    if not meta_file.exists():
        return {}
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_collection(
    col_dir: pathlib.Path,
    chunks: List[Dict[str, Any]],
    files: List[str],
) -> None:
    col_dir.mkdir(parents=True, exist_ok=True)
    (col_dir / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "chunk_count": len(chunks),
        "files": files,
    }
    (col_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ── BM25 engine ───────────────────────────────────────────────────────────────


def _build_bm25_index(
    chunks: List[Dict[str, Any]],
) -> Tuple[List[List[str]], Dict[str, float], float]:
    """Pre-tokenize chunks and compute IDF + avgdl.

    Returns:
        tokenized_chunks: list of token lists per chunk
        idf: {term -> idf_score}
        avgdl: average document length (in tokens)
    """
    tokenized: List[List[str]] = [_tokenize(c["text"]) for c in chunks]
    n = len(tokenized)
    if n == 0:
        return tokenized, {}, 0.0

    df: Counter = Counter()
    total_len = 0
    for toks in tokenized:
        total_len += len(toks)
        for tok in set(toks):
            df[tok] += 1

    avgdl = total_len / n if n else 1.0
    idf = {
        tok: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
        for tok, freq in df.items()
    }
    return tokenized, idf, avgdl


def _bm25_score(
    query_tokens: List[str],
    doc_tokens: List[str],
    idf: Dict[str, float],
    avgdl: float,
    k1: float = BM25_K1,
    b: float = BM25_B,
) -> float:
    """BM25 score for one document given query tokens."""
    if not doc_tokens or not query_tokens:
        return 0.0
    dl = len(doc_tokens)
    tf_map = Counter(doc_tokens)
    score = 0.0
    for qt in query_tokens:
        if qt not in tf_map:
            continue
        tf = tf_map[qt]
        idf_val = idf.get(qt, 0.0)
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * dl / max(avgdl, 1))
        score += idf_val * (numerator / denominator)
    return score


# ── Tool implementations ──────────────────────────────────────────────────────


def _rag_index(
    ctx: ToolContext,
    paths: List[str],
    collection: str,
    overwrite: bool = True,
) -> str:
    """Index files into a named collection."""
    try:
        _validate_collection_name(collection)
    except ValueError as exc:
        return f"rag_index error: {exc}"

    if not paths:
        return "rag_index error: paths list is empty."

    col_dir = _collection_dir(ctx.drive_root, collection)

    # Load existing chunks if not overwriting
    existing_chunks: List[Dict[str, Any]] = []
    existing_files: List[str] = []
    if not overwrite and col_dir.exists():
        existing_chunks = _load_chunks(col_dir)
        meta = _load_meta(col_dir)
        existing_files = meta.get("files", [])

    all_chunks: List[Dict[str, Any]] = list(existing_chunks)
    new_files: List[str] = []
    errors: List[str] = []

    for raw_path in paths:
        p = pathlib.Path(raw_path)
        if not p.exists():
            errors.append(f"  not found: {raw_path}")
            continue
        if not p.is_file():
            errors.append(f"  not a file: {raw_path}")
            continue

        text = _parse_file(p)
        if not text.strip():
            errors.append(f"  empty/unreadable: {p.name}")
            continue

        file_chunks = _chunk_text(text, source=p.name, page=0)
        if not file_chunks:
            errors.append(f"  no chunks extracted: {p.name}")
            continue

        all_chunks.extend(file_chunks)
        new_files.append(str(p))

    all_files = existing_files + new_files
    _save_collection(col_dir, all_chunks, all_files)

    lines = [
        f"rag_index: collection '{collection}' indexed.",
        f"  Files: {len(new_files)} new ({len(all_files)} total)",
        f"  Chunks: {len(all_chunks)} total",
    ]
    if errors:
        lines.append(f"  Warnings ({len(errors)}):")
        lines.extend(errors)
    return "\n".join(lines)


def _rag_query(
    ctx: ToolContext,
    query: str,
    collection: str,
    top_k: int = 5,
    show_source: bool = True,
) -> str:
    """Search a collection by BM25 and return top-K chunks."""
    query = (query or "").strip()
    if not query:
        return "rag_query error: query must be non-empty."

    try:
        _validate_collection_name(collection)
    except ValueError as exc:
        return f"rag_query error: {exc}"

    col_dir = _collection_dir(ctx.drive_root, collection)
    chunks = _load_chunks(col_dir)
    if not chunks:
        return (
            f"rag_query: collection '{collection}' is empty or does not exist. "
            f"Run rag_index first."
        )

    top_k = max(1, min(20, int(top_k)))
    query_tokens = _tokenize(query)
    if not query_tokens:
        return "rag_query error: query contains no usable tokens."

    tokenized, idf, avgdl = _build_bm25_index(chunks)

    scored: List[Tuple[float, int]] = []
    for i, doc_tokens in enumerate(tokenized):
        s = _bm25_score(query_tokens, doc_tokens, idf, avgdl)
        if s > 0:
            scored.append((s, i))

    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]

    if not top:
        return f"rag_query: no results for '{query}' in collection '{collection}'."

    lines = [
        f"## rag_query: «{query}» in '{collection}' — {len(top)} result(s)\n"
    ]
    for rank, (score, idx) in enumerate(top, 1):
        chunk = chunks[idx]
        excerpt = chunk["text"][:400].strip()
        if len(chunk["text"]) > 400:
            excerpt += "…"
        if show_source:
            src_label = f"[{chunk['source']}]"
            if chunk.get("page"):
                src_label += f" p.{chunk['page']}"
            lines.append(f"**{rank}.** {src_label}  score={score:.3f}")
        else:
            lines.append(f"**{rank}.**")
        lines.append(excerpt)
        lines.append("")

    return "\n".join(lines)


def _rag_list(ctx: ToolContext) -> str:
    """List all indexed collections with stats."""
    rag_root = ctx.drive_root / "rag"
    if not rag_root.exists():
        return "rag_list: no collections indexed yet."

    collections = sorted(
        p for p in rag_root.iterdir() if p.is_dir()
    )
    if not collections:
        return "rag_list: no collections indexed yet."

    lines = [f"## RAG collections ({len(collections)} total)\n"]
    for col_dir in collections:
        meta = _load_meta(col_dir)
        name = col_dir.name
        fc = meta.get("file_count", "?")
        cc = meta.get("chunk_count", "?")
        created = (meta.get("created_at") or "")[:10]
        files = meta.get("files", [])
        file_names = ", ".join(pathlib.Path(f).name for f in files[:5])
        if len(files) > 5:
            file_names += f" (+{len(files)-5} more)"
        lines.append(f"**{name}** — {fc} files, {cc} chunks, indexed {created}")
        if file_names:
            lines.append(f"  Files: {file_names}")
    return "\n".join(lines)


def _rag_delete(ctx: ToolContext, collection: str) -> str:
    """Delete a collection and all its indexed data."""
    try:
        _validate_collection_name(collection)
    except ValueError as exc:
        return f"rag_delete error: {exc}"

    col_dir = _collection_dir(ctx.drive_root, collection)
    if not col_dir.exists():
        return f"rag_delete: collection '{collection}' not found."

    import shutil
    shutil.rmtree(col_dir)
    return f"rag_delete: collection '{collection}' deleted."


# ── Tool registry ─────────────────────────────────────────────────────────────


def get_tools() -> List[ToolEntry]:
    index_schema = {
        "name": "rag_index",
        "description": (
            "Index local files into a named collection for semantic search. "
            "Parses .txt .md .pdf .docx .py .json .csv and 40+ other formats. "
            "Splits content into overlapping chunks (~600 chars), builds a BM25 index. "
            "By default overwrites an existing collection (set overwrite=false to append). "
            "Use rag_query to search after indexing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of absolute file paths to index",
                },
                "collection": {
                    "type": "string",
                    "description": "Collection name (alphanumeric, hyphens, underscores, max 64)",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Overwrite existing collection (default true). Set false to append.",
                    "default": True,
                },
            },
            "required": ["paths", "collection"],
        },
    }

    query_schema = {
        "name": "rag_query",
        "description": (
            "Search an indexed collection using BM25 full-text ranking. "
            "Returns top-K most relevant text fragments from the indexed documents. "
            "Much more efficient than loading all files into context — "
            "10 large files become 5 focused paragraphs. "
            "Use after rag_index."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — topic, question, phrase, or keyword",
                },
                "collection": {
                    "type": "string",
                    "description": "Collection name to search in",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 20)",
                    "default": 5,
                },
                "show_source": {
                    "type": "boolean",
                    "description": "Show source file name and score (default true)",
                    "default": True,
                },
            },
            "required": ["query", "collection"],
        },
    }

    list_schema = {
        "name": "rag_list",
        "description": (
            "List all indexed RAG collections with stats: "
            "number of files, chunks, and indexed date."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }

    delete_schema = {
        "name": "rag_delete",
        "description": "Delete a RAG collection and all its indexed data.",
        "parameters": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection name to delete",
                },
            },
            "required": ["collection"],
        },
    }

    return [
        ToolEntry("rag_index", index_schema,
                  lambda ctx, **kw: _rag_index(ctx, **kw)),
        ToolEntry("rag_query", query_schema,
                  lambda ctx, **kw: _rag_query(ctx, **kw)),
        ToolEntry("rag_list", list_schema,
                  lambda ctx, **kw: _rag_list(ctx)),
        ToolEntry("rag_delete", delete_schema,
                  lambda ctx, **kw: _rag_delete(ctx, **kw)),
    ]

"""Memory search — TF-IDF search across all Veles memory sources.

Searches: chat.jsonl, knowledge/*.md, identity.md, scratchpad.md.
No external dependencies — pure Python TF-IDF + cosine-like scoring.

Usage:
    memory_search(query="3x-ui webBasePath")
    memory_search(query="ssh key deploy", top_k=10)
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# ── Tokenizer ─────────────────────────────────────────────────────────────────

# Common Russian + English stop-words (short/functional words)
_STOP_WORDS = frozenset("""
а б в г д е ё ж з и й к л м н о п р с т у ф х ц ч ш щ ъ ы ь э ю я
это как что для так да нет не из по на но или то есть от там где когда
если при уже было были были также через между после перед
the a an in on at to of and or is are was were be been being have has
had do does did will would could should may might shall can cannot with
that this they them their which from for into by up out about over just
""".split())


def _tokenize(text: str) -> List[str]:
    """Lowercase, extract word tokens ≥2 chars, remove stop-words."""
    text = text.lower()
    tokens = re.findall(r"[a-zа-яёa-z0-9_][a-zа-яёa-z0-9_]{1,}", text)
    return [t for t in tokens if t not in _STOP_WORDS]


# ── Chunk model ───────────────────────────────────────────────────────────────

class _Chunk:
    """A searchable text fragment with provenance."""

    __slots__ = ("source", "date", "text", "tokens")

    def __init__(self, source: str, date: str, text: str) -> None:
        self.source = source
        self.date = date
        self.text = text
        self.tokens = _tokenize(text)


# ── Loaders ───────────────────────────────────────────────────────────────────

def _split_into_chunks(text: str, source: str, date: str,
                       chunk_size: int = 600) -> List[_Chunk]:
    """Split text into overlapping chunks of ~chunk_size chars."""
    if len(text) <= chunk_size:
        c = _Chunk(source, date, text)
        return [c] if len(c.tokens) >= 3 else []
    chunks = []
    step = chunk_size - 100  # 100-char overlap
    for i in range(0, len(text), step):
        part = text[i : i + chunk_size].strip()
        if not part:
            continue
        c = _Chunk(source, date, part)
        if len(c.tokens) >= 3:
            chunks.append(c)
    return chunks


def _load_chat(drive_root: pathlib.Path, max_messages: int = 3000) -> List[_Chunk]:
    """Load recent chat messages from chat.jsonl."""
    chat_file = drive_root / "logs" / "chat.jsonl"
    if not chat_file.exists():
        return []
    try:
        with chat_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        log.warning("memory_search: cannot read chat.jsonl: %s", exc)
        return []

    lines = lines[-max_messages:]
    chunks: List[_Chunk] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
            text = (msg.get("text") or "").strip()
            if len(text) < 20:
                continue
            date = (msg.get("ts") or "")[:10]
            direction = msg.get("direction", "?")
            src = f"chat/{direction}"
            chunks.extend(_split_into_chunks(text, src, date))
        except Exception:
            continue
    return chunks


def _load_md(path: pathlib.Path, label: str) -> List[_Chunk]:
    """Load a markdown file and split by paragraphs."""
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("memory_search: cannot read %s: %s", path, exc)
        return []

    try:
        import datetime
        date = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except Exception:
        date = ""

    # Split by double newlines (paragraphs / sections)
    paragraphs = re.split(r"\n\s*\n", content)
    chunks: List[_Chunk] = []
    for para in paragraphs:
        para = para.strip()
        if len(para) < 30:
            continue
        chunks.extend(_split_into_chunks(para, label, date))
    return chunks


def _build_corpus(drive_root: pathlib.Path) -> List[_Chunk]:
    """Assemble the full searchable corpus from all memory sources."""
    corpus: List[_Chunk] = []

    # Chat history (most recent messages)
    corpus.extend(_load_chat(drive_root))

    # Knowledge base
    kb_dir = drive_root / "memory" / "knowledge"
    if kb_dir.is_dir():
        for md_file in sorted(kb_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue  # skip _index.md
            corpus.extend(_load_md(md_file, f"knowledge/{md_file.stem}"))

    # Identity manifest
    corpus.extend(_load_md(drive_root / "memory" / "identity.md", "identity"))

    # Working memory
    corpus.extend(_load_md(drive_root / "memory" / "scratchpad.md", "scratchpad"))

    return corpus


# ── TF-IDF engine ─────────────────────────────────────────────────────────────

def _compute_idf(corpus: List[_Chunk]) -> Dict[str, float]:
    """BM25-flavoured IDF: log((N+1)/(df+1)) + 1."""
    n = len(corpus)
    df: Counter = Counter()
    for chunk in corpus:
        for tok in set(chunk.tokens):
            df[tok] += 1
    return {tok: math.log((n + 1) / (freq + 1)) + 1.0 for tok, freq in df.items()}


def _score(query_tokens: List[str], chunk: _Chunk,
           idf: Dict[str, float]) -> float:
    """TF-IDF dot-product score (not normalised — fast enough for ranking)."""
    if not chunk.tokens or not query_tokens:
        return 0.0
    doc_tf = Counter(chunk.tokens)
    doc_len = len(chunk.tokens)
    return sum(
        (doc_tf[qt] / doc_len) * idf.get(qt, 1.0)
        for qt in query_tokens
        if qt in doc_tf
    )


def _run_search(
    query: str,
    drive_root: pathlib.Path,
    top_k: int = 5,
) -> List[Tuple[float, _Chunk]]:
    """Build corpus, score all chunks, return top-K."""
    corpus = _build_corpus(drive_root)
    if not corpus:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []
    idf = _compute_idf(corpus)
    scored = [
        (s, c)
        for c in corpus
        if (s := _score(query_tokens, c, idf)) > 0
    ]
    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


# ── Public tool ───────────────────────────────────────────────────────────────

def _memory_search(ctx: ToolContext, query: str, top_k: int = 5) -> str:
    """Search across all memory sources and return top-K relevant fragments."""
    query = (query or "").strip()
    if not query:
        return "⚠️ memory_search: query must be non-empty."
    top_k = max(1, min(20, int(top_k)))

    results = _run_search(query, ctx.drive_root, top_k)
    if not results:
        return f"memory_search: no results for query '{query}'."

    lines = [f"## memory_search: «{query}» — top {len(results)}\n"]
    for i, (score, chunk) in enumerate(results, 1):
        excerpt = chunk.text[:300].replace("\n", " ").strip()
        if len(chunk.text) > 300:
            excerpt += "…"
        lines.append(f"**{i}. [{chunk.source}]** {chunk.date}  score={score:.4f}")
        lines.append(f"   {excerpt}")
        lines.append("")

    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    schema = {
        "name": "memory_search",
        "description": (
            "TF-IDF search across all Veles memory: recent chat history, "
            "knowledge base topics, identity.md, and scratchpad. "
            "Use to recall past conversations, find relevant patterns, "
            "or locate prior decisions. Returns top-K fragments with "
            "source label, date, and relevance score."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — topic, keyword, question, or phrase",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 20)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    }
    return [ToolEntry("memory_search", schema, lambda ctx, **kw: _memory_search(ctx, **kw))]

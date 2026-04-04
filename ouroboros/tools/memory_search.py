"""Memory search — BM25 + fuzzy-expanded search across all Veles memory sources.

Searches: chat.jsonl, knowledge/*.md, identity.md, scratchpad.md,
          task_reflections.jsonl, dialogue_blocks.json, notes.jsonl.
No external dependencies — pure Python BM25 + difflib fuzzy query expansion.

Improvements over plain TF-IDF:
- BM25 scoring (k1=1.5, b=0.75) — proper document-length normalisation,
  saturating TF, standard IDF. Significantly better ranking on short chunks.
- Fuzzy query expansion — each query token is matched against corpus
  vocabulary via SequenceMatcher (threshold 0.82). Catches typos and
  near-synonyms: "evalution" → expands with "evolution".

Usage:
    memory_search(query="3x-ui webBasePath")
    memory_search(query="ssh key deploy", top_k=10)
    memory_search(query="reward model colon")   # finds notes tagged [ml]
    memory_search(query="evalution round limit")  # typo-tolerant
"""

from __future__ import annotations

import json
import logging
import math
import pathlib
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

# ── BM25 hyper-parameters ────────────────────────────────────────────────────

BM25_K1: float = 1.5   # term-frequency saturation
BM25_B: float = 0.75   # document-length normalisation factor

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


def _load_task_reflections(drive_root: pathlib.Path,
                           max_reflections: int = 200) -> List[_Chunk]:
    """Load task reflections from task_reflections.jsonl.

    Each reflection becomes a searchable chunk combining goal + reflection text.
    This covers evolution history, error patterns, and past task summaries.
    """
    ref_file = drive_root / "logs" / "task_reflections.jsonl"
    if not ref_file.exists():
        return []
    try:
        with ref_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        log.warning("memory_search: cannot read task_reflections.jsonl: %s", exc)
        return []

    lines = lines[-max_reflections:]
    chunks: List[_Chunk] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
            goal = (rec.get("goal") or "").strip()
            reflection = (rec.get("reflection") or "").strip()
            if not goal and not reflection:
                continue
            # Skip failed reflections (auth errors etc.)
            if reflection.startswith("(reflection generation failed"):
                reflection = ""
            date = (rec.get("ts") or "")[:10]
            task_id = rec.get("task_id", "")
            src = f"reflection/{task_id}" if task_id else "reflection"
            # Combine goal + reflection into one searchable text
            combined = goal
            if reflection:
                combined = f"{goal}\n\n{reflection}"
            chunks.extend(_split_into_chunks(combined, src, date))
        except Exception:
            continue
    return chunks


def _load_dialogue_blocks(drive_root: pathlib.Path) -> List[_Chunk]:
    """Load consolidated dialogue blocks from dialogue_blocks.json.

    These are auto-summarised historical episodes — condensed memory of
    past conversations that no longer fit in the live chat context.
    """
    blocks_file = drive_root / "memory" / "dialogue_blocks.json"
    if not blocks_file.exists():
        return []
    try:
        with blocks_file.open("r", encoding="utf-8") as f:
            blocks = json.load(f)
    except Exception as exc:
        log.warning("memory_search: cannot read dialogue_blocks.json: %s", exc)
        return []

    if not isinstance(blocks, list):
        return []

    chunks: List[_Chunk] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = (block.get("content") or "").strip()
        if not content:
            continue
        date = (block.get("ts") or "")[:10]
        block_range = block.get("range", "")
        src = f"dialogue/{block_range}" if block_range else "dialogue"
        chunks.extend(_split_into_chunks(content, src, date))
    return chunks


def _load_notes(drive_root: pathlib.Path) -> List[_Chunk]:
    """Load saved notes from notes.jsonl.

    Each note becomes a searchable chunk. Tags and source URL are prepended
    to the text so they participate in token scoring.
    Deleted notes are skipped.
    """
    notes_file = drive_root / "memory" / "notes.jsonl"
    if not notes_file.exists():
        return []
    try:
        with notes_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as exc:
        log.warning("memory_search: cannot read notes.jsonl: %s", exc)
        return []

    chunks: List[_Chunk] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            note = json.loads(raw)
            if note.get("deleted", False):
                continue
            text = (note.get("text") or "").strip()
            if not text:
                continue
            tags = note.get("tags") or []
            source_url = (note.get("source") or "").strip()
            note_id = note.get("id", "")
            date = (note.get("timestamp") or "")[:10]

            # Build searchable text: prepend tags + source so they are tokenized
            parts = []
            if tags:
                parts.append("tags: " + " ".join(tags))
            if source_url:
                parts.append(f"source: {source_url}")
            parts.append(text)
            combined = "\n".join(parts)

            src = f"note/{note_id}" if note_id else "note"
            chunks.extend(_split_into_chunks(combined, src, date))
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

    # Task reflections (evolution history, error patterns)
    corpus.extend(_load_task_reflections(drive_root))

    # Consolidated dialogue blocks (historical episodes)
    corpus.extend(_load_dialogue_blocks(drive_root))

    # Personal notes (saved via note_add)
    corpus.extend(_load_notes(drive_root))

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


# ── BM25 engine ───────────────────────────────────────────────────────────────

def _compute_idf(corpus: List[_Chunk]) -> Dict[str, float]:
    """BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1).

    More precise than the old log((N+1)/(df+1))+1 formula.
    Guarantees positive values even for very frequent terms (df close to N).
    """
    n = len(corpus)
    df: Counter = Counter()
    for chunk in corpus:
        for tok in set(chunk.tokens):
            df[tok] += 1
    return {
        tok: math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)
        for tok, freq in df.items()
    }


def _score(
    query_tokens: List[str],
    chunk: _Chunk,
    idf: Dict[str, float],
    avgdl: float = 50.0,
) -> float:
    """BM25 score for a set of query tokens against a single chunk.

    BM25 formula:
        score = Σ IDF(t) * TF_norm(t, d)
        TF_norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl/avgdl))

    Parameters
    ----------
    query_tokens:
        Tokenised query (may include fuzzy-expanded tokens).
    chunk:
        Document chunk with pre-tokenised `.tokens`.
    idf:
        Precomputed IDF mapping from `_compute_idf`.
    avgdl:
        Average document length across the corpus (in tokens).
        Defaults to 50 for backward-compat when called without corpus context.
    """
    if not chunk.tokens or not query_tokens:
        return 0.0
    doc_tf = Counter(chunk.tokens)
    doc_len = len(chunk.tokens)
    score = 0.0
    for qt in query_tokens:
        if qt not in doc_tf:
            continue
        tf = doc_tf[qt]
        tf_norm = (
            tf * (BM25_K1 + 1)
            / (tf + BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avgdl))
        )
        score += idf.get(qt, 0.0) * tf_norm
    return score


def _fuzzy_expand_query(
    query_tokens: List[str],
    idf_vocab: Dict[str, float],
    threshold: float = 0.82,
    max_per_token: int = 3,
) -> List[str]:
    """Expand query tokens with similar terms from the corpus vocabulary.

    Uses ``difflib.SequenceMatcher`` to find vocabulary words whose edit
    similarity to a query token is ≥ ``threshold``. Short tokens (< 4 chars)
    are skipped to avoid false positives.

    Example
    -------
    >>> _fuzzy_expand_query(["evalution"], {"evolution": 1.0, "evaluation": 1.0})
    ['evalution', 'evolution', 'evaluation']

    Parameters
    ----------
    query_tokens:
        Original tokenised query.
    idf_vocab:
        IDF dict — its keys form the vocabulary to search against.
    threshold:
        Minimum SequenceMatcher ratio (0–1). Default 0.82 ≈ 1-char difference
        on typical 5–8 char words.
    max_per_token:
        Maximum number of expansions added per query token.

    Returns
    -------
    Deduplicated list: original tokens first, then expansions ordered by
    similarity score descending.
    """
    expanded: List[str] = list(query_tokens)
    seen: set = set(query_tokens)

    for qt in query_tokens:
        if len(qt) < 4:
            continue  # too short — too many spurious matches
        candidates: List[Tuple[float, str]] = []
        for vocab_tok in idf_vocab:
            if vocab_tok in seen:
                continue
            # Quick length pre-filter before the O(n) ratio call
            if abs(len(vocab_tok) - len(qt)) > max(3, len(qt) // 3):
                continue
            ratio = SequenceMatcher(None, qt, vocab_tok, autojunk=False).ratio()
            if ratio >= threshold:
                candidates.append((ratio, vocab_tok))
        # Sort by ratio descending, take top N
        candidates.sort(key=lambda x: -x[0])
        for _, tok in candidates[:max_per_token]:
            if tok not in seen:
                expanded.append(tok)
                seen.add(tok)

    return expanded


def _run_search(
    query: str,
    drive_root: pathlib.Path,
    top_k: int = 5,
) -> List[Tuple[float, _Chunk]]:
    """Build corpus, score all chunks with BM25 + fuzzy expansion, return top-K.

    Pipeline:
    1. Build corpus from all memory sources.
    2. Compute BM25 IDF over corpus vocabulary.
    3. Expand query tokens via fuzzy matching against vocabulary.
    4. Score each chunk with BM25 using expanded tokens + true avgdl.
    5. Return top-k by score.
    """
    corpus = _build_corpus(drive_root)
    if not corpus:
        return []
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    idf = _compute_idf(corpus)

    # Compute average document length for BM25 length normalisation
    total_tokens = sum(len(c.tokens) for c in corpus)
    avgdl = total_tokens / len(corpus) if corpus else 50.0

    # Expand query with fuzzy matches from corpus vocabulary
    expanded_tokens = _fuzzy_expand_query(query_tokens, idf)

    scored = [
        (s, c)
        for c in corpus
        if (s := _score(expanded_tokens, c, idf, avgdl)) > 0
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
            "BM25 + fuzzy-expanded search across all Veles memory: recent chat history, "
            "task reflections (evolution history, error patterns), "
            "consolidated dialogue blocks (historical episodes), "
            "personal notes (saved via note_add — includes tags and source URLs), "
            "knowledge base topics, identity.md, and scratchpad. "
            "Uses BM25 scoring (better ranking than plain TF-IDF) and fuzzy query "
            "expansion (tolerates typos and near-synonyms). "
            "Use to recall past conversations, find relevant patterns, "
            "locate prior decisions, or retrieve saved research notes. "
            "Returns top-K fragments with source label, date, and relevance score."
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

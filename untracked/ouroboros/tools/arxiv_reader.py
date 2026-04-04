"""arxiv_reader — arXiv paper search and watchlist for ML/AI research monitoring.

Provides access to arXiv papers via the public Atom API (no auth required).
Supports full-text search, category browsing, and a watchlist for automatic
new-paper tracking with watermark-based deduplication.

No API key required — uses https://export.arxiv.org/api/query

Storage: /opt/veles-data/memory/arxiv_watchlist.json
Each entry stores:
  - query: optional keyword filter (e.g. 'language model')
  - category: arXiv category code (e.g. 'cs.LG', 'cs.CL', 'cs.AI')
  - label: human-readable label
  - added_at: ISO timestamp
  - last_checked: ISO timestamp
  - last_seen_ids: list of arXiv IDs already delivered

Tools:
    arxiv_search(query, category?, limit?)     — keyword search across arXiv
    arxiv_latest(category, limit?)             — latest papers in a category
    arxiv_watchlist_add(category, query?, label?)  — track a topic/category
    arxiv_watchlist_remove(category_or_label)  — stop tracking
    arxiv_watchlist_status()                   — list active entries
    arxiv_watchlist_check(limit?)              — fetch new papers since last check

arXiv categories of interest:
    cs.LG  — Machine Learning
    cs.AI  — Artificial Intelligence
    cs.CL  — Computation and Language / NLP
    cs.CV  — Computer Vision
    cs.NE  — Neural and Evolutionary Computing
    stat.ML — Statistics / ML
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.tools.registry import ToolContext

log = logging.getLogger(__name__)

_WATCHLIST_FILE = "memory/arxiv_watchlist.json"
_API_BASE = "https://export.arxiv.org/api/query"
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


# ---------------------------------------------------------------------------
# arXiv API helpers
# ---------------------------------------------------------------------------

def _fetch_arxiv(search_query: str, start: int = 0, max_results: int = 10) -> List[Dict[str, Any]]:
    """Call arXiv Atom API, return list of parsed paper dicts."""
    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    })
    url = f"{_API_BASE}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Veles-arxiv-reader/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        log.warning("arxiv_reader: API fetch failed: %s", exc)
        raise

    return _parse_feed(raw)


def _parse_feed(xml_text: str) -> List[Dict[str, Any]]:
    """Parse arXiv Atom feed XML into list of paper dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("arxiv_reader: XML parse error: %s", exc)
        return []

    papers = []
    for entry in root.findall("atom:entry", _NS):
        arxiv_id_url = (entry.findtext("atom:id", namespaces=_NS) or "").strip()
        # Extract short ID like 2604.01234 from full URL
        arxiv_id = re.sub(r"^.*/abs/([^v]+).*$", r"\1", arxiv_id_url)

        title = (entry.findtext("atom:title", namespaces=_NS) or "").strip()
        summary = (entry.findtext("atom:summary", namespaces=_NS) or "").strip()
        # Collapse whitespace in abstract
        summary = re.sub(r"\s+", " ", summary)
        published = (entry.findtext("atom:published", namespaces=_NS) or "").strip()
        updated = (entry.findtext("atom:updated", namespaces=_NS) or "").strip()

        authors = [a.findtext("atom:name", namespaces=_NS) or "" for a in entry.findall("atom:author", _NS)]

        primary_cat_el = entry.find("arxiv:primary_category", _NS)
        primary_cat = primary_cat_el.attrib.get("term", "") if primary_cat_el is not None else ""

        # PDF link
        pdf_url = ""
        for link in entry.findall("atom:link", _NS):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break

        abstract_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else arxiv_id_url
        comment = (entry.findtext("arxiv:comment", namespaces=_NS) or "").strip()

        papers.append({
            "id": arxiv_id,
            "title": title,
            "authors": authors[:5],  # cap at 5 authors
            "abstract": summary[:800],  # cap abstract length
            "published": published[:10],  # YYYY-MM-DD
            "updated": updated[:10],
            "category": primary_cat,
            "url": abstract_url,
            "pdf_url": pdf_url,
            "comment": comment,
        })

    return papers


# ---------------------------------------------------------------------------
# Watchlist storage
# ---------------------------------------------------------------------------

def _load_watchlist(ctx: ToolContext) -> List[Dict[str, Any]]:
    path = pathlib.Path(ctx.drive_root) / _WATCHLIST_FILE
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_watchlist(ctx: ToolContext, entries: List[Dict[str, Any]]) -> None:
    path = pathlib.Path(ctx.drive_root) / _WATCHLIST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _entry_key(entry: Dict[str, Any]) -> str:
    """Unique key for a watchlist entry."""
    return f"{entry.get('category','')}/{entry.get('query','')}"


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _arxiv_search(ctx: ToolContext, query: str, category: str = "", limit: int = 10) -> str:
    """Search arXiv papers by keyword (and optional category)."""
    limit = max(1, min(limit, 50))
    if category and query:
        search_q = f"cat:{category} AND all:{urllib.parse.quote(query)}"
    elif category:
        search_q = f"cat:{category}"
    else:
        search_q = f"all:{urllib.parse.quote(query)}"

    try:
        papers = _fetch_arxiv(search_q, max_results=limit)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    return json.dumps({
        "ok": True,
        "query": query,
        "category": category,
        "count": len(papers),
        "papers": papers,
    }, ensure_ascii=False)


def _arxiv_latest(ctx: ToolContext, category: str, limit: int = 10) -> str:
    """Fetch latest papers submitted to an arXiv category."""
    limit = max(1, min(limit, 50))
    try:
        papers = _fetch_arxiv(f"cat:{category}", max_results=limit)
    except Exception as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    return json.dumps({
        "ok": True,
        "category": category,
        "count": len(papers),
        "papers": papers,
    }, ensure_ascii=False)


def _arxiv_watchlist_add(ctx: ToolContext, category: str, query: str = "", label: str = "") -> str:
    """Add a category+query to the arXiv watchlist."""
    entries = _load_watchlist(ctx)
    key = f"{category}/{query}"
    if any(_entry_key(e) == key for e in entries):
        return json.dumps({"ok": False, "error": f"Already watching '{key}'"})

    entry = {
        "category": category,
        "query": query,
        "label": label or (f"{category} {query}".strip()),
        "added_at": datetime.now(tz=timezone.utc).isoformat(),
        "last_checked": "",
        "last_seen_ids": [],
    }
    entries.append(entry)
    _save_watchlist(ctx, entries)
    return json.dumps({"ok": True, "added": entry["label"], "key": key})


def _arxiv_watchlist_remove(ctx: ToolContext, category_or_label: str) -> str:
    """Remove an entry from the arXiv watchlist by category or label."""
    entries = _load_watchlist(ctx)
    before = len(entries)
    entries = [
        e for e in entries
        if e.get("category") != category_or_label and e.get("label") != category_or_label
    ]
    if len(entries) == before:
        return json.dumps({"ok": False, "error": f"Not found: '{category_or_label}'"})
    _save_watchlist(ctx, entries)
    return json.dumps({"ok": True, "removed": category_or_label, "remaining": len(entries)})


def _arxiv_watchlist_status(ctx: ToolContext) -> str:
    """List all active arXiv watchlist entries."""
    entries = _load_watchlist(ctx)
    return json.dumps({
        "ok": True,
        "count": len(entries),
        "entries": [
            {
                "label": e.get("label", ""),
                "category": e.get("category", ""),
                "query": e.get("query", ""),
                "last_checked": e.get("last_checked", ""),
                "seen_count": len(e.get("last_seen_ids", [])),
            }
            for e in entries
        ],
    }, ensure_ascii=False)


def _arxiv_watchlist_check(ctx: ToolContext, limit: int = 5) -> str:
    """Fetch new arXiv papers for all watchlist entries since last check."""
    entries = _load_watchlist(ctx)
    if not entries:
        return json.dumps({"ok": True, "new_papers": [], "sources_checked": 0})

    limit = max(1, min(limit, 20))
    all_new: List[Dict[str, Any]] = []
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    for entry in entries:
        category = entry.get("category", "")
        query = entry.get("query", "")
        seen_ids = set(entry.get("last_seen_ids", []))
        label = entry.get("label", category)

        try:
            if category and query:
                search_q = f"cat:{category} AND all:{urllib.parse.quote(query)}"
            elif category:
                search_q = f"cat:{category}"
            else:
                search_q = f"all:{urllib.parse.quote(query)}"

            papers = _fetch_arxiv(search_q, max_results=limit * 2)
        except Exception as exc:
            log.warning("arxiv watchlist check failed for '%s': %s", label, exc)
            continue

        new_papers = [p for p in papers if p["id"] not in seen_ids]
        for p in new_papers[:limit]:
            all_new.append({**p, "matched_label": label})
            seen_ids.add(p["id"])

        entry["last_checked"] = now_iso
        # Keep only last 200 seen IDs to bound storage
        entry["last_seen_ids"] = list(seen_ids)[-200:]

    _save_watchlist(ctx, entries)
    return json.dumps({
        "ok": True,
        "new_papers": all_new,
        "count": len(all_new),
        "sources_checked": len(entries),
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_SEARCH_SCHEMA = {
    "type": "object",
    "description": "Search arXiv papers by keyword and optional category (cs.LG, cs.AI, cs.CL, cs.CV, etc.).",
    "properties": {
        "query": {"type": "string", "description": "Search query (keywords, author name, title words)"},
        "category": {"type": "string", "description": "arXiv category code (e.g. 'cs.LG', 'cs.AI'). Optional."},
        "limit": {"type": "integer", "description": "Max results (1-50, default 10)"},
    },
    "required": ["query"],
}

_LATEST_SCHEMA = {
    "type": "object",
    "description": "Fetch the latest papers submitted to an arXiv category.",
    "properties": {
        "category": {"type": "string", "description": "arXiv category code (e.g. 'cs.LG', 'cs.AI')"},
        "limit": {"type": "integer", "description": "Max results (1-50, default 10)"},
    },
    "required": ["category"],
}

_WATCHLIST_ADD_SCHEMA = {
    "type": "object",
    "description": "Add an arXiv category+query to watchlist for automatic new-paper tracking.",
    "properties": {
        "category": {"type": "string", "description": "arXiv category (e.g. 'cs.LG')"},
        "query": {"type": "string", "description": "Optional keyword filter within the category"},
        "label": {"type": "string", "description": "Human-readable label for this entry"},
    },
    "required": ["category"],
}

_WATCHLIST_REMOVE_SCHEMA = {
    "type": "object",
    "description": "Remove an arXiv watchlist entry by category code or label.",
    "properties": {
        "category_or_label": {"type": "string", "description": "Category code or label to remove"},
    },
    "required": ["category_or_label"],
}

_WATCHLIST_STATUS_SCHEMA = {
    "type": "object",
    "description": "List all active arXiv watchlist subscriptions.",
    "properties": {},
}

_WATCHLIST_CHECK_SCHEMA = {
    "type": "object",
    "description": "Check all arXiv watchlist entries for new papers since last check.",
    "properties": {
        "limit": {"type": "integer", "description": "Max new papers per subscription (1-20, default 5)"},
    },
}


def get_tools():
    from ouroboros.tools.registry import ToolEntry

    return [
        ToolEntry(name="arxiv_search", schema=_SEARCH_SCHEMA, handler=lambda ctx, **kw: _arxiv_search(ctx, **kw), timeout_sec=30),
        ToolEntry(name="arxiv_latest", schema=_LATEST_SCHEMA, handler=lambda ctx, **kw: _arxiv_latest(ctx, **kw), timeout_sec=30),
        ToolEntry(name="arxiv_watchlist_add", schema=_WATCHLIST_ADD_SCHEMA, handler=lambda ctx, **kw: _arxiv_watchlist_add(ctx, **kw), timeout_sec=10),
        ToolEntry(name="arxiv_watchlist_remove", schema=_WATCHLIST_REMOVE_SCHEMA, handler=lambda ctx, **kw: _arxiv_watchlist_remove(ctx, **kw), timeout_sec=10),
        ToolEntry(name="arxiv_watchlist_status", schema=_WATCHLIST_STATUS_SCHEMA, handler=lambda ctx, **kw: _arxiv_watchlist_status(ctx, **kw), timeout_sec=10),
        ToolEntry(name="arxiv_watchlist_check", schema=_WATCHLIST_CHECK_SCHEMA, handler=lambda ctx, **kw: _arxiv_watchlist_check(ctx, **kw), timeout_sec=60),
    ]

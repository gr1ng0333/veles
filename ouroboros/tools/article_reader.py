"""article_reader — fetch and extract full article text from any URL.

Lightweight: zero external dependencies (stdlib only).
Uses a cascade of heuristics to find the main body text:
  1. <article> or <main> semantic HTML tags
  2. <div> with content-like class/id (content, post, article, entry, story, text)
  3. Meta og:description / twitter:description as summary fallback
  4. Paragraph density heuristic: largest cluster of <p> tags

Tools:
    article_fetch(url, max_chars?, include_links?)  — extract full article text from URL
    article_summary(url)                            — fast extract: title + first 300 chars

Usage:
    article_fetch(url="https://example.com/post/123")
    article_fetch(url="https://arxiv.org/abs/2503.00865", max_chars=3000)
    article_summary(url="https://techcrunch.com/article/...")
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)
_FETCH_TIMEOUT = 20
_MAX_BYTES = 1 * 1024 * 1024  # 1 MB max download
_MAX_CHARS_DEFAULT = 4000
_MIN_CONTENT_LEN = 100  # discard blocks shorter than this

# ── Regex patterns ──────────────────────────────────────────────────────────

_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NAV_RE = re.compile(r"<(nav|header|footer|aside|form)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")

# Semantic content containers — checked in order
_CONTENT_TAGS = [
    re.compile(r"<article[^>]*>(.*?)</article>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<main[^>]*>(.*?)</main>", re.DOTALL | re.IGNORECASE),
]

# Div/section class/id patterns that typically wrap article content
_CONTENT_DIV_RE = re.compile(
    r'<(?:div|section)[^>]+(?:class|id)=["\'][^"\']*'
    r'(?:article|post-content|post-body|entry-content|story|article-body'
    r'|article__content|content-body|article-text|page-content'
    r'|post__content|entry-body|text-content|main-content)'
    r'[^"\']*["\'][^>]*>(.*?)</(?:div|section)>',
    re.DOTALL | re.IGNORECASE,
)

# Meta tag extractors
_OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_OG_DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TW_DESC_RE = re.compile(r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{3,200})</title>", re.IGNORECASE)

# Link extractor for include_links mode
_LINK_RE = re.compile(r'<a[^>]+href=["\']([^"\'#][^"\']*)["\'][^>]*>([^<]{3,100})</a>', re.IGNORECASE)


# ── HTTP fetch ───────────────────────────────────────────────────────────────

def _fetch_url(url: str) -> Tuple[str, str]:
    """Fetch URL, return (html_content, final_url). Raises on error."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT, context=ctx) as resp:
        final_url = resp.url
        raw = resp.read(_MAX_BYTES)

    # Detect encoding
    charset = "utf-8"
    content_type = ""
    if hasattr(resp, "headers"):
        content_type = resp.headers.get("Content-Type", "") or ""
    m = re.search(r"charset=([^\s;\"']+)", content_type, re.IGNORECASE)
    if m:
        charset = m.group(1).strip()
    # Fallback: look for charset in meta tags on first 2KB
    snippet = raw[:2048].decode("ascii", errors="replace")
    m2 = re.search(r'charset=["\']?([a-z0-9\-]+)', snippet, re.IGNORECASE)
    if m2:
        charset = m2.group(1)

    try:
        content = raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        content = raw.decode("utf-8", errors="replace")

    return content, final_url


# ── HTML → text ──────────────────────────────────────────────────────────────

def _clean_html(fragment: str) -> str:
    """Strip tags, unescape entities, normalize whitespace."""
    text = _SCRIPT_RE.sub("", fragment)
    text = _STYLE_RE.sub("", text)
    text = _COMMENT_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    text = _MULTI_WS.sub(" ", text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    text = "\n".join(lines)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _extract_meta(html: str) -> Dict[str, str]:
    """Extract title and description from meta/title tags."""
    title = ""
    description = ""

    m = _OG_TITLE_RE.search(html)
    if m:
        title = html_mod.unescape(m.group(1).strip())

    if not title:
        m = _TITLE_RE.search(html)
        if m:
            title = html_mod.unescape(m.group(1).strip())
            # Remove site name suffix (e.g. "Article | Site Name")
            if " | " in title:
                title = title.rsplit(" | ", 1)[0].strip()
            elif " — " in title:
                title = title.rsplit(" — ", 1)[0].strip()
            elif " - " in title:
                title = title.rsplit(" - ", 1)[0].strip()

    m = _OG_DESC_RE.search(html)
    if m:
        description = html_mod.unescape(m.group(1).strip())
    if not description:
        m = _TW_DESC_RE.search(html)
        if m:
            description = html_mod.unescape(m.group(1).strip())

    return {"title": title, "description": description}


def _extract_links(html: str, base_url: str) -> List[str]:
    """Extract meaningful links from the page, resolved to absolute URLs."""
    parsed_base = urllib.parse.urlparse(base_url)
    base = f"{parsed_base.scheme}://{parsed_base.netloc}"

    links = []
    seen = set()
    for href, text in _LINK_RE.findall(html):
        href = href.strip()
        if href.startswith("//"):
            href = parsed_base.scheme + ":" + href
        elif href.startswith("/"):
            href = base + href
        elif not href.startswith("http"):
            continue
        if href not in seen and len(links) < 20:
            links.append(href)
            seen.add(href)
    return links


def _extract_content(html: str) -> str:
    """Extract main content text using cascade of heuristics."""
    # Remove navigation noise before searching
    cleaned = _NAV_RE.sub("", html)
    cleaned = _SCRIPT_RE.sub("", cleaned)
    cleaned = _STYLE_RE.sub("", cleaned)

    # 1. Try semantic tags: <article>, <main>
    for pattern in _CONTENT_TAGS:
        m = pattern.search(cleaned)
        if m:
            candidate = _clean_html(m.group(1))
            if len(candidate) >= _MIN_CONTENT_LEN:
                return candidate

    # 2. Try content-class div/section
    m = _CONTENT_DIV_RE.search(cleaned)
    if m:
        candidate = _clean_html(m.group(1))
        if len(candidate) >= _MIN_CONTENT_LEN:
            return candidate

    # 3. Paragraph density: find the <div> containing most <p> tags
    # Split by opening <div> and score each block
    div_blocks = re.split(r"<div[^>]*>", cleaned)
    best_text = ""
    best_score = 0
    for block in div_blocks:
        # Count <p> tags
        p_count = len(re.findall(r"<p[^>]*>", block, re.IGNORECASE))
        if p_count < 3:
            continue
        text = _clean_html(block)
        # Score = p_count * sqrt(text_length) — prefer dense paragraphs
        score = p_count * (len(text) ** 0.5)
        if score > best_score and len(text) >= _MIN_CONTENT_LEN:
            best_score = score
            best_text = text

    if best_text:
        return best_text

    # 4. Fallback: strip everything and return body text
    # Remove obvious boilerplate (navigation links etc.)
    body_m = re.search(r"<body[^>]*>(.*?)</body>", cleaned, re.DOTALL | re.IGNORECASE)
    body = body_m.group(1) if body_m else cleaned
    return _clean_html(body)


# ── Tool implementations ─────────────────────────────────────────────────────

def _article_fetch(
    ctx: ToolContext,
    url: str,
    max_chars: int = _MAX_CHARS_DEFAULT,
    include_links: bool = False,
) -> str:
    """Fetch and extract full article text from a URL."""
    if not url:
        return json.dumps({"error": "url is required"})

    try:
        html_content, final_url = _fetch_url(url)
    except urllib.error.HTTPError as e:
        return json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url})
    except urllib.error.URLError as e:
        return json.dumps({"error": f"URL error: {e.reason}", "url": url})
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})

    meta = _extract_meta(html_content)
    content = _extract_content(html_content)

    # Truncate if needed
    truncated = False
    if len(content) > max_chars:
        content = content[:max_chars]
        # Don't cut mid-word
        last_space = content.rfind(" ")
        if last_space > max_chars * 0.9:
            content = content[:last_space]
        content += "\n\n[… truncated]"
        truncated = True

    result: Dict[str, Any] = {
        "url": final_url,
        "title": meta["title"],
        "description": meta["description"],
        "content": content,
        "content_chars": len(content),
        "truncated": truncated,
    }

    if include_links:
        result["links"] = _extract_links(html_content, final_url)

    return json.dumps(result, ensure_ascii=False, indent=2)


def _article_summary(ctx: ToolContext, url: str) -> str:
    """Fast fetch: extract just title and first ~300 chars of article text."""
    if not url:
        return json.dumps({"error": "url is required"})

    try:
        html_content, final_url = _fetch_url(url)
    except urllib.error.HTTPError as e:
        return json.dumps({"error": f"HTTP {e.code}: {e.reason}", "url": url})
    except urllib.error.URLError as e:
        return json.dumps({"error": f"URL error: {e.reason}", "url": url})
    except Exception as e:
        return json.dumps({"error": str(e), "url": url})

    meta = _extract_meta(html_content)
    content = _extract_content(html_content)

    # Use og:description if content is short
    preview = content[:300].strip()
    if len(preview) < 50 and meta["description"]:
        preview = meta["description"][:300]

    return json.dumps({
        "url": final_url,
        "title": meta["title"],
        "preview": preview,
    }, ensure_ascii=False, indent=2)


# ── Tool schemas ─────────────────────────────────────────────────────────────

_SCHEMA_FETCH = {
    "name": "article_fetch",
    "description": (
        "Fetch and extract the full readable text from any article URL. "
        "Uses lightweight HTML parsing (no browser needed). "
        "Returns title, description, and main body text.\n\n"
        "Best for: blog posts, arXiv abstracts, news articles, documentation pages, HN submissions.\n"
        "Not suitable for: heavy SPA sites, paywalled content, login-required pages.\n\n"
        "Use article_summary() for a quick preview without reading the full text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL of the article to fetch",
            },
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to return (default 4000, max ~16000)",
                "default": 4000,
            },
            "include_links": {
                "type": "boolean",
                "description": "If true, include a list of links found on the page",
                "default": False,
            },
        },
        "required": ["url"],
    },
}

_SCHEMA_SUMMARY = {
    "name": "article_summary",
    "description": (
        "Fast extract: fetch article title + first 300 chars of text from URL. "
        "Cheaper than article_fetch when you only need a quick preview. "
        "Uses the same zero-dependency HTML parser."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to summarize",
            },
        },
        "required": ["url"],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(name="article_fetch", schema=_SCHEMA_FETCH, handler=_article_fetch),
        ToolEntry(name="article_summary", schema=_SCHEMA_SUMMARY, handler=_article_summary),
    ]

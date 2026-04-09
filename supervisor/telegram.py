"""
Supervisor — Telegram client + formatting.

TelegramClient, message splitting, markdown→HTML conversion, send_with_budget.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

from supervisor.state import load_state, save_state, append_jsonl
from ouroboros.utils import sanitize_owner_facing_text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT = None  # pathlib.Path
TOTAL_BUDGET_LIMIT: float = 0.0
BUDGET_REPORT_EVERY_MESSAGES: int = 10
_TG: Optional["TelegramClient"] = None


def init(drive_root, total_budget_limit: float, budget_report_every: int,
         tg_client: "TelegramClient") -> None:
    global DRIVE_ROOT, TOTAL_BUDGET_LIMIT, BUDGET_REPORT_EVERY_MESSAGES, _TG
    DRIVE_ROOT = drive_root
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _TG = tg_client


def get_tg() -> "TelegramClient":
    assert _TG is not None, "telegram.init() not called"
    return _TG


# ---------------------------------------------------------------------------
# TelegramClient
# ---------------------------------------------------------------------------

class TelegramClient:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self._token = token
        # Persistent HTTP session — reuses TCP+TLS connections.
        # Without this, every request opens a new connection (~10s on this VPS).
        self._session = requests.Session()

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = self._session.get(
                    f"{self.base}/getUpdates",
                    params={"offset": offset, "timeout": timeout,
                            "allowed_updates": ["message", "edited_message"]},
                    timeout=timeout + 5,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("ok") is not True:
                    raise RuntimeError(f"Telegram getUpdates failed: {data}")
                return data.get("result") or []
            except Exception as e:
                last_err = repr(e)
                if attempt < 2:
                    import time
                    time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Telegram getUpdates failed after retries: {last_err}")

    def send_message(self, chat_id: int, text: str, parse_mode: str = "") -> Tuple[bool, str]:
        last_err = "unknown"
        for attempt in range(3):
            try:
                payload: Dict[str, Any] = {"chat_id": chat_id, "text": text,
                                           "disable_web_page_preview": True}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                r = self._session.post(f"{self.base}/sendMessage", data=payload, timeout=30)
                r.raise_for_status()
                data = r.json()
                if data.get("ok") is True:
                    return True, "ok"
                last_err = f"telegram_api_error: {data}"
            except Exception as e:
                last_err = repr(e)
            if attempt < 2:
                import time
                time.sleep(0.8 * (attempt + 1))
        return False, last_err

    def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send chat action (typing indicator). Best-effort, no retries."""
        try:
            r = self._session.post(
                f"{self.base}/sendChatAction",
                data={"chat_id": chat_id, "action": action},
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            log.debug("Failed to send chat action to chat_id=%d", chat_id, exc_info=True)
            return False

    def send_photo(self, chat_id: int, photo_bytes: bytes,
                   caption: str = "") -> Tuple[bool, str]:
        """Send a photo to a chat. photo_bytes is raw PNG/JPEG data."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                files = {"photo": ("screenshot.png", photo_bytes, "image/png")}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                r = self._session.post(
                    f"{self.base}/sendPhoto",
                    data=data, files=files, timeout=30,
                )
                r.raise_for_status()
                resp = r.json()
                if resp.get("ok") is True:
                    return True, "ok"
                last_err = f"telegram_api_error: {resp}"
            except Exception as e:
                last_err = repr(e)
            if attempt < 2:
                import time
                time.sleep(0.8 * (attempt + 1))
        return False, last_err


    def send_document(self, chat_id: int, file_bytes: bytes, filename: str,
                      caption: str = "", mime_type: str = "application/octet-stream") -> Tuple[bool, str]:
        """Send a document/file to a chat."""
        last_err = "unknown"
        for attempt in range(3):
            try:
                safe_filename = filename or "file.bin"
                files = {"document": (safe_filename, file_bytes, mime_type or "application/octet-stream")}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                r = self._session.post(
                    f"{self.base}/sendDocument",
                    data=data, files=files, timeout=30,
                )
                r.raise_for_status()
                resp = r.json()
                if resp.get("ok") is True:
                    return True, "ok"
                last_err = f"telegram_api_error: {resp}"
            except Exception as e:
                last_err = repr(e)
            if attempt < 2:
                import time
                time.sleep(0.8 * (attempt + 1))
        return False, last_err

    def download_file_base64(self, file_id: str, max_bytes: int = 10_000_000) -> Tuple[Optional[str], str]:
        """Download a file from Telegram and return (base64_data, mime_type). Returns (None, "") on failure."""
        try:
            # Get file path
            r = self._session.get(f"{self.base}/getFile", params={"file_id": file_id}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return None, ""
            file_path = data["result"].get("file_path", "")
            file_size = int(data["result"].get("file_size") or 0)
            if file_size > max_bytes:
                return None, ""

            # Download file
            download_url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
            r2 = self._session.get(download_url, timeout=30)
            r2.raise_for_status()

            import base64
            b64 = base64.b64encode(r2.content).decode("ascii")

            # Guess mime type from extension
            ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
            mime_map = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
                "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
                "mp4": "video/mp4",
            }
            mime = mime_map.get(ext, "application/octet-stream")

            return b64, mime
        except Exception:
            log.warning("Failed to download file_id=%s from Telegram", file_id, exc_info=True)
            return None, ""


# ---------------------------------------------------------------------------
# Message splitting + formatting
# ---------------------------------------------------------------------------

def split_telegram(text: str, limit: int = 3800) -> List[str]:
    chunks: List[str] = []
    s = text
    while len(s) > limit:
        cut = s.rfind("\n", 0, limit)
        if cut < 100:
            cut = limit
        chunks.append(s[:cut])
        s = s[cut:]
    chunks.append(s)
    return chunks


def _sanitize_telegram_text(text: str) -> str:
    if text is None:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        c for c in text
        if (ord(c) >= 32 or c in ("\n", "\t")) and not (0xD800 <= ord(c) <= 0xDFFF)
    )


def _tg_utf16_len(text: str) -> int:
    if not text:
        return 0
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _strip_markdown(text: str) -> str:
    """Strip all markdown formatting markers, leaving only plain text."""
    # Fenced code blocks (keep content)
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    # Inline code (keep content)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Bold+italic (***text***)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    # Bold (**text**)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # Italic (*text* or _text_)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # Strikethrough (~~text~~)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Headers (# text -> text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # List markers (- or * at start of line, keep bullet but remove markdown)
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    # Clean up any remaining stray markdown markers
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("`", "")
    return text


def _markdown_to_telegram_html(md: str) -> str:
    """Convert Markdown to Telegram-safe HTML.

    Supported: fenced code, inline code, **bold**, *italic*, _italic_,
    ~~strikethrough~~, [links](url), # headers, list items.
    Handles unmatched markers gracefully. Telegram only allows: b, i, u, s, code, pre, a.
    """
    import html as _html
    md = md or ""

    # --- Step 1: extract fenced code blocks into placeholders ---
    # Match ``` with optional language, then content, then closing ```
    fence_re = re.compile(r"```[^\n]*\n([\s\S]*?)```", re.MULTILINE)
    fenced: list = []

    def _save_fence(m: re.Match) -> str:
        code_content = m.group(1)
        # Remove trailing newline if present
        if code_content.endswith("\n"):
            code_content = code_content[:-1]
        code_esc = _html.escape(code_content, quote=False)
        placeholder = f"\x00FENCE{len(fenced)}\x00"
        fenced.append(f"<pre>{code_esc}</pre>")
        return placeholder

    text = fence_re.sub(_save_fence, md)

    # --- Step 2: extract inline code into placeholders ---
    inline_code_re = re.compile(r"`([^`\n]+)`")
    inlines: list = []

    def _save_inline(m: re.Match) -> str:
        code_esc = _html.escape(m.group(1), quote=False)
        placeholder = f"\x00CODE{len(inlines)}\x00"
        inlines.append(f"<code>{code_esc}</code>")
        return placeholder

    text = inline_code_re.sub(_save_inline, text)

    # --- Step 3: HTML-escape remaining text (before adding HTML tags) ---
    text = _html.escape(text, quote=False)

    # --- Step 4: apply markdown formatting (order matters) ---
    # Headers: # at start of line -> bold with newline
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # Links: [text](url) - escape the URL too
    def _replace_link(m: re.Match) -> str:
        link_text = m.group(1)
        url = m.group(2)
        # URL must not contain quotes or special chars that break HTML
        url_safe = url.replace('"', '%22').replace('<', '%3C').replace('>', '%3E')
        return f'<a href="{url_safe}">{link_text}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _replace_link, text)

    # Bold+italic: ***text*** (must come before ** and *)
    # Use non-greedy match, handle line breaks
    text = re.sub(r"\*\*\*([^*\n]+?)\*\*\*", r"<b><i>\1</i></b>", text)

    # Bold: **text** (non-greedy, single line)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", text)

    # Strikethrough: ~~text~~ (non-greedy, single line)
    text = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", text)

    # Italic: *text* (single *, not adjacent to another *, single line)
    # Lookahead/lookbehind to avoid matching ** or *** remnants
    text = re.sub(r"(?<![*\w])\*([^*\n]+?)\*(?![*\w])", r"<i>\1</i>", text)

    # Italic: _text_ (word-boundary to avoid matching snake_case, single line)
    text = re.sub(r"\b_([^_\n]+?)_\b", r"<i>\1</i>", text)

    # List items: convert - or * at line start to •
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    # --- Step 5: restore placeholders ---
    for i, code in enumerate(inlines):
        text = text.replace(f"\x00CODE{i}\x00", code)
    for i, block in enumerate(fenced):
        text = text.replace(f"\x00FENCE{i}\x00", block)

    return text


def _chunk_markdown_for_telegram(md: str, max_chars: int = 3500) -> List[str]:
    md = md or ""
    max_chars = max(256, min(4096, int(max_chars)))
    lines = md.splitlines(keepends=True)
    chunks: List[str] = []
    cur = ""
    in_fence = False
    fence_open = "```\n"
    fence_close = "```\n"

    def _flush() -> None:
        nonlocal cur
        if cur and cur.strip():
            chunks.append(cur)
        cur = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if in_fence:
                fence_open = line if line.endswith("\n") else (line + "\n")

        reserve = _tg_utf16_len(fence_close) if in_fence else 0
        if _tg_utf16_len(cur) + _tg_utf16_len(line) > max_chars - reserve:
            if in_fence and cur:
                cur += fence_close
            _flush()
            cur = fence_open if in_fence else ""
        cur += line

    if in_fence:
        cur += fence_close
    _flush()
    return chunks or [md]


def _send_markdown_telegram(chat_id: int, text: str) -> Tuple[bool, str]:
    """Send markdown text as Telegram HTML, with plain-text fallback."""
    tg = get_tg()
    chunks = _chunk_markdown_for_telegram(text or "", max_chars=3200)
    chunks = [c for c in chunks if isinstance(c, str) and c.strip()]
    if not chunks:
        return False, "empty_chunks"
    last_err = "ok"
    for md_part in chunks:
        html_text = _markdown_to_telegram_html(md_part)
        ok, err = tg.send_message(chat_id, _sanitize_telegram_text(html_text), parse_mode="HTML")
        if not ok:
            plain = _strip_markdown(md_part)
            ok2, err2 = tg.send_message(chat_id, _sanitize_telegram_text(plain))
            if not ok2:
                last_err = err2
    return True, last_err


# ---------------------------------------------------------------------------
# send_with_budget – the one function all outgoing messages go through
# ---------------------------------------------------------------------------

def send_with_budget(chat_id: int, text: str, *, force: bool = False,
                     progress: bool = False, parse_mode: str = "") -> bool:
    """Send a message; increment budget counter; handle budget limit."""
    if not text or not str(text).strip():
        return False
    import pathlib
    tg = get_tg()
    drive = DRIVE_ROOT or pathlib.Path("/opt/veles-data")

    # --- sanitize ---
    text = sanitize_owner_facing_text(text)

    # --- budget gate ---
    st = load_state()
    remaining = TOTAL_BUDGET_LIMIT - float(st.get("spent_usd") or 0)
    if remaining <= 0 and not force:
        log.warning("Budget exhausted — suppressing outgoing message")
        return False

    # --- try Markdown→HTML first, then plain-text fallback ---
    ok, err = _send_markdown_telegram(chat_id, text)
    if not ok:
        for chunk in split_telegram(_sanitize_telegram_text(_strip_markdown(text))):
            tg.send_message(chat_id, chunk)

    # --- persist ---
    if not progress:
        append_jsonl(
            drive / "logs" / "chat.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "role": "out",
                "chat_id": chat_id,
                "text": text[:1000],
            },
        )
        st["last_outgoing_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save_state(st)

    return True


def log_chat(direction: str, chat_id: int, user_id: int, text: str) -> None:
    """Log a chat message to chat.jsonl."""
    import pathlib
    drive = DRIVE_ROOT or pathlib.Path("/opt/veles-data")
    append_jsonl(
        drive / "logs" / "chat.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": direction,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": (text or "")[:1000],
        },
    )

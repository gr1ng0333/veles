from __future__ import annotations

import base64
import json
import pathlib
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from ouroboros.tools.registry import ToolContext

BROWSER_FAILURE_CLASSES = {
    "timeout_wait_selector",
    "empty_dom",
    "redirect_loop",
    "hydration_incomplete",
    "anti_bot_suspected",
    "captcha_present",
    "blocked_or_challenge_page",
    "interaction_intercepted",
    "stale_selector",
    "content_not_rendered",
}

_CHALLENGE_PATTERNS = [
    "just a moment", "checking your browser", "verify you are human", "attention required",
    "cf-challenge", "captcha", "cloudflare", "access denied", "bot check",
    "security check", "challenge", "ddos-guard", "perimeterx",
]
_INTERACTION_PATTERNS = [
    "element is not attached", "element is detached", "element is outside of the viewport",
    "another element would receive the click", "element is not visible", "element is obscured",
    "intercepts pointer events",
]
_STALE_SELECTOR_PATTERNS = [
    "strict mode violation", "failed to find element matching selector", "resolved to 0 elements",
    "no node found for selector",
]


def classify_browser_failure(
    *,
    message: str,
    final_url: str,
    title: str,
    ready_state: str,
    visible_text: str,
    dom_size: int,
    selector_waited: str = "",
    matched_selectors: Optional[Iterable[str]] = None,
    body_child_count: int = 0,
    has_app_root: bool = False,
    script_count: int = 0,
) -> Dict[str, str]:
    msg = (message or "").lower()
    text = (visible_text or "").strip()
    title_l = (title or "").lower()
    final_url_l = (final_url or "").lower()
    matched = list(matched_selectors or [])
    looks_like_captcha = any(token in text.lower() for token in ["captcha", "verify you are human", "verification code", "security code"])
    looks_like_challenge = any(pattern in f"{title}\n{text}".lower() for pattern in _CHALLENGE_PATTERNS)

    if any(p in msg for p in _INTERACTION_PATTERNS):
        return {
            "probable_failure_class": "interaction_intercepted",
            "short_reason": "Элемент найден, но взаимодействие с ним перехвачено перекрытием, невидимостью или другим слоем UI.",
        }
    if any(p in msg for p in _STALE_SELECTOR_PATTERNS):
        return {
            "probable_failure_class": "stale_selector",
            "short_reason": "Селектор больше не соответствует живому DOM или элемент исчез/перерисовался.",
        }
    if looks_like_challenge:
        cls = "anti_bot_suspected" if any(x in f"{title_l}\n{text.lower()}" for x in ["cloudflare", "bot", "human", "challenge"]) else "blocked_or_challenge_page"
        return {
            "probable_failure_class": cls,
            "short_reason": "Похоже, сайт отдал anti-bot challenge вместо нормальной страницы." if cls == "anti_bot_suspected" else "Страница похожа на блокировку или challenge вместо целевого контента.",
        }
    if looks_like_captcha:
        return {
            "probable_failure_class": "captcha_present",
            "short_reason": "На странице видна captcha/verification, она блокирует штатный сценарий.",
        }
    if "timeout" in msg and selector_waited:
        if ready_state in {"interactive", "complete"} and has_app_root and len(text) < 40 and dom_size < 1500 and script_count > 0:
            return {
                "probable_failure_class": "hydration_incomplete",
                "short_reason": "SPA-каркас загрузился, но гидратация/рендер не завершились до полезного контента.",
            }
        if ready_state == "complete" and dom_size < 200 and len(text) < 50:
            return {
                "probable_failure_class": "content_not_rendered",
                "short_reason": "Страница загрузилась, но полезный контент так и не дорендерился.",
            }
        return {
            "probable_failure_class": "timeout_wait_selector",
            "short_reason": f"Не дождался селектора {selector_waited}: страница не показала ожидаемый элемент вовремя.",
        }
    if final_url_l and final_url_l.count("redirect") >= 2:
        return {
            "probable_failure_class": "redirect_loop",
            "short_reason": "Похоже на redirect-loop или повторные переадресации вместо выхода на целевую страницу.",
        }
    if ready_state == "complete" and dom_size < 80 and len(text) == 0:
        return {
            "probable_failure_class": "empty_dom",
            "short_reason": "Страница открылась почти пустой: DOM практически пуст или body без текста.",
        }
    if ready_state == "complete" and dom_size > 0 and len(text) < 40 and body_child_count > 0:
        return {
            "probable_failure_class": "content_not_rendered",
            "short_reason": "DOM есть, но видимого полезного контента почти нет — похоже, он не дорендерился.",
        }
    return {
        "probable_failure_class": "content_not_rendered",
        "short_reason": "Браузерная операция сорвалась, но страница не дала достаточно явных сигналов; нужен снимок состояния.",
    }


def capture_browser_failure_diagnostics(
    ctx: ToolContext,
    *,
    page: Any,
    operation: str,
    selector_waited: str = "",
    attempted_selectors: Optional[Iterable[str]] = None,
    exception: Optional[BaseException] = None,
) -> Dict[str, Any]:
    try:
        final_url = getattr(page, "url", "") or ""
    except Exception:
        final_url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        ready_state = page.evaluate("() => document.readyState") or ""
    except Exception:
        ready_state = ""
    try:
        visible_text = page.inner_text("body") or ""
    except Exception:
        visible_text = ""
    try:
        html = page.content() or ""
    except Exception:
        html = ""
    try:
        body_info = page.evaluate(
            """() => ({
                bodyChildCount: document.body ? document.body.children.length : 0,
                hasRoot: !!document.querySelector('#__next, #root, [data-reactroot], [ng-version], [id*=app], [class*=app]'),
                scriptCount: document.scripts ? document.scripts.length : 0
            })"""
        ) or {}
    except Exception:
        body_info = {}

    attempted: List[str] = []
    for item in [selector_waited, *(attempted_selectors or [])]:
        s = str(item or "").strip()
        if s and s not in attempted:
            attempted.append(s)

    matched: List[str] = []
    for selector in attempted:
        try:
            if page.locator(selector).count() > 0:
                matched.append(selector)
                continue
        except Exception:
            pass
        try:
            if page.evaluate("(sel) => !!document.querySelector(sel)", selector):
                matched.append(selector)
        except Exception:
            pass

    classification = classify_browser_failure(
        message=str(exception or ""),
        final_url=final_url,
        title=title,
        ready_state=ready_state,
        visible_text=visible_text,
        dom_size=len(html),
        selector_waited=selector_waited,
        matched_selectors=matched,
        body_child_count=int(body_info.get("bodyChildCount") or 0),
        has_app_root=bool(body_info.get("hasRoot")),
        script_count=int(body_info.get("scriptCount") or 0),
    )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    task_id = str(getattr(ctx, "task_id", "task") or "task")
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{stamp}-{task_id}-{operation}").strip("-")
    base = ctx.drive_path("logs") / "browser_failures" / slug
    base.parent.mkdir(parents=True, exist_ok=True)
    html_path = str(base.with_suffix(".html"))
    text_path = str(base.with_suffix(".txt"))
    attempts_path = str(base.with_suffix(".json"))
    pathlib.Path(html_path).write_text(html, encoding="utf-8")
    pathlib.Path(text_path).write_text(visible_text, encoding="utf-8")
    pathlib.Path(attempts_path).write_text(json.dumps({
        "operation": operation,
        "selector_waited": selector_waited,
        "attempted_selectors": attempted,
        "matched_selectors": matched,
        "exception": str(exception or ""),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    screenshot_path = ""
    try:
        data = page.screenshot(type="png", full_page=False)
        ctx.browser_state.last_screenshot_b64 = base64.b64encode(data).decode()
        screenshot_path = str(base.with_suffix(".png"))
        pathlib.Path(screenshot_path).write_bytes(data)
    except Exception:
        pass

    diagnostic = {
        "final_url": final_url,
        "title": title,
        "ready_state": ready_state,
        "visible_text_size": len(visible_text.strip()),
        "dom_size": len(html),
        "selector_waited": selector_waited,
        "matched_selectors": matched,
        "probable_failure_class": classification["probable_failure_class"],
        "short_reason": classification["short_reason"],
        "artifacts": {
            "html_snapshot": html_path,
            "text_snapshot": text_path,
            "screenshot": screenshot_path,
            "attempts": attempts_path,
        },
    }
    ctx.browser_state.last_failure_diagnostics = diagnostic
    return diagnostic

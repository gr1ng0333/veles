"""FatSecret nutrition tools with OAuth2 token caching and optional RU→EN translation."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
_API_URL = "https://platform.fatsecret.com/rest/server.api"
_DEFAULT_TIMEOUT = 20
_DEFAULT_MODEL = "codex/gpt-5.4-mini"
_MAX_RESULTS = 20
_TOKEN_SKEW_SEC = 30

_token_lock = threading.Lock()
_token_cache: Dict[str, Any] = {
    "access_token": "",
    "expires_at": 0.0,
}


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _looks_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    if not usage:
        return
    event = {
        "type": "llm_usage",
        "category": "fatsecret_translate",
        "task_id": ctx.task_id,
        "task_type": ctx.current_task_type or "task",
        "model": model,
        "usage": {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "cached_tokens": int(usage.get("cached_tokens", 0) or 0),
            "cost": float(usage.get("cost", 0.0) or 0.0),
        },
    }
    if ctx.event_queue is not None:
        try:
            ctx.event_queue.put_nowait(event)
            return
        except Exception:
            log.debug("fatsecret translate: event_queue put failed", exc_info=True)
    ctx.pending_events.append(event)



def _translate_query(ctx: ToolContext, query: str) -> Tuple[str, Optional[str]]:
    if not query.strip() or not _looks_cyrillic(query):
        return query.strip(), None
    try:
        from ouroboros.llm import LLMClient

        model = _DEFAULT_MODEL
        client = LLMClient()
        message, usage = client.chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate food and nutrition search queries from Russian to concise English. "
                        "Return only the English query, no quotes, no explanation."
                    ),
                },
                {"role": "user", "content": query.strip()},
            ],
            model=model,
            reasoning_effort="low",
            max_tokens=64,
            tools=None,
            tool_choice="none",
        )
        _emit_usage(ctx, usage, model)
        content = (message.get("content") or "").strip()
        if content:
            return content.splitlines()[0].strip(), model
    except Exception as exc:
        log.warning("FatSecret translation failed, using raw query: %s", exc)
    return query.strip(), None



def _request_json(url: str, *, data: bytes | None = None, headers: Dict[str, str] | None = None, timeout: int = _DEFAULT_TIMEOUT) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError("FatSecret response is not a JSON object")
    return parsed



def _get_access_token() -> str:
    now = time.time()
    with _token_lock:
        cached = str(_token_cache.get("access_token") or "")
        expires_at = float(_token_cache.get("expires_at") or 0.0)
        if cached and expires_at - _TOKEN_SKEW_SEC > now:
            return cached

        client_id = _env("FATSECRET_CLIENT_ID")
        client_secret = _env("FATSECRET_CLIENT_SECRET")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": "basic",
        }).encode("utf-8")
        payload = _request_json(
            _TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        token = str(payload.get("access_token") or "").strip()
        expires_in = int(payload.get("expires_in") or 3600)
        if not token:
            raise RuntimeError("FatSecret token response missing access_token")
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + max(expires_in, 60)
        return token



def _api_call(method: str, **params: Any) -> Dict[str, Any]:
    token = _get_access_token()
    body = {"method": method, "format": "json"}
    for key, value in params.items():
        if value is None or value == "":
            continue
        body[key] = str(value)
    payload = urllib.parse.urlencode(body).encode("utf-8")
    return _request_json(
        _API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )



def _coerce_food_items(raw_foods: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_foods, list):
        foods = raw_foods
    elif isinstance(raw_foods, dict):
        one = raw_foods.get("food")
        if isinstance(one, list):
            foods = one
        elif isinstance(one, dict):
            foods = [one]
        else:
            foods = []
    else:
        foods = []

    items: List[Dict[str, Any]] = []
    for item in foods:
        if not isinstance(item, dict):
            continue
        items.append({
            "food_id": str(item.get("food_id") or ""),
            "food_name": str(item.get("food_name") or ""),
            "food_type": str(item.get("food_type") or ""),
            "food_url": str(item.get("food_url") or ""),
            "brand_name": str(item.get("brand_name") or ""),
            "food_description": str(item.get("food_description") or ""),
        })
    return items



def _normalize_serving(serving: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "serving_id", "serving_description", "serving_url", "metric_serving_amount",
        "metric_serving_unit", "number_of_units", "measurement_description",
        "calories", "carbohydrate", "protein", "fat", "saturated_fat",
        "polyunsaturated_fat", "monounsaturated_fat", "cholesterol", "sodium",
        "potassium", "fiber", "sugar", "vitamin_a", "vitamin_c", "calcium", "iron",
    ]
    return {field: str(serving.get(field) or "") for field in fields if field in serving}



def _coerce_servings(raw_servings: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_servings, list):
        servings = raw_servings
    elif isinstance(raw_servings, dict):
        one = raw_servings.get("serving")
        if isinstance(one, list):
            servings = one
        elif isinstance(one, dict):
            servings = [one]
        else:
            servings = []
    else:
        servings = []
    return [_normalize_serving(item) for item in servings if isinstance(item, dict)]



def _fatsecret_search(ctx: ToolContext, query: str, max_results: int = 10) -> str:
    original = (query or "").strip()
    if not original:
        return json.dumps({"error": "query is required"}, ensure_ascii=False)

    translated, translation_model = _translate_query(ctx, original)
    limit = max(1, min(int(max_results or 10), _MAX_RESULTS))
    payload = _api_call("foods.search", search_expression=translated, max_results=limit)
    foods_root = payload.get("foods") if isinstance(payload, dict) else None
    foods = _coerce_food_items(foods_root)
    return json.dumps({
        "query": original,
        "normalized_query": translated,
        "translation_model": translation_model,
        "count": len(foods),
        "foods": foods,
    }, ensure_ascii=False)



def _fatsecret_food(ctx: ToolContext, food_id: str) -> str:
    ident = str(food_id or "").strip()
    if not ident:
        return json.dumps({"error": "food_id is required"}, ensure_ascii=False)

    payload = _api_call("food.get.v4", food_id=ident)
    food = payload.get("food") if isinstance(payload, dict) else None
    if not isinstance(food, dict):
        return json.dumps({"error": "food not found", "food_id": ident}, ensure_ascii=False)

    servings = _coerce_servings(food.get("servings"))
    return json.dumps({
        "food_id": ident,
        "food_name": str(food.get("food_name") or ""),
        "food_type": str(food.get("food_type") or ""),
        "brand_name": str(food.get("brand_name") or ""),
        "food_url": str(food.get("food_url") or ""),
        "servings": servings,
    }, ensure_ascii=False)



def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="fatsecret_search",
            schema={
                "type": "function",
                "function": {
                    "name": "fatsecret_search",
                    "description": "Search foods in FatSecret by free-text query. Automatically translates Russian queries to concise English before API lookup.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "Food query, can be Russian or English."},
                            "max_results": {"type": "integer", "description": "Maximum number of results (1-20).", "default": 10},
                        },
                        "required": ["query"],
                    },
                },
            },
            handler=_fatsecret_search,
        ),
        ToolEntry(
            name="fatsecret_food",
            schema={
                "type": "function",
                "function": {
                    "name": "fatsecret_food",
                    "description": "Fetch detailed nutrition data and servings for a FatSecret food id.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "food_id": {"type": "string", "description": "FatSecret food_id returned by fatsecret_search."},
                        },
                        "required": ["food_id"],
                    },
                },
            },
            handler=_fatsecret_food,
        ),
    ]

# Codex Integration Architecture (Veles)

Updated: 2026-03-04

## Scope
This document explains how Veles routes LLM calls through **Codex OAuth** and how `codex_proxy.py`, `llm.py`, and `loop.py` work together.

Primary files:
- `ouroboros/codex_proxy.py`
- `ouroboros/llm.py`
- `ouroboros/loop.py`
- `ouroboros/agent.py` (entry/orchestration context)

## High-level flow
1. `agent.py` builds context and starts `run_llm_loop(...)`.
2. `loop.py` picks model + effort, then calls `_call_llm_with_retry(...)`.
3. `_call_llm_with_retry(...)` calls `LLMClient.chat(...)`.
4. In `llm.py`:
   - `codex/*` models -> `codex_proxy.call_codex(...)`
   - all others -> normal OpenRouter API path.
5. `codex_proxy.py` converts message/tool format, sends request to Codex endpoint via OAuth token, parses SSE `response.completed`, and returns Chat-style assistant message.
6. `loop.py` executes tool calls and continues rounds until final text.

So Codex OAuth is a **transport branch inside `LLMClient.chat()`**, not a separate agent loop.

## Routing rule
In `LLMClient.chat()`:
- if `model.startswith("codex/")` -> strip prefix and call proxy
- else -> OpenRouter route

Example: `codex/gpt-5.3-codex` becomes OAuth model `gpt-5.3-codex`.

## OAuth token lifecycle (`codex_proxy.py`)
Token sources (env first, then file fallback):
- `CODEX_ACCESS_TOKEN`
- `CODEX_REFRESH_TOKEN`
- `CODEX_TOKEN_EXPIRES`
- `CODEX_ACCOUNT_ID`

Persistent file:
- `/opt/veles-data/state/codex_tokens.json`

Refresh behavior (`refresh_token_if_needed()`):
- Reuse access token if expiry is more than 1 hour away.
- Otherwise refresh via `POST https://auth.openai.com/oauth/token` with `grant_type=refresh_token`.
- Persist new tokens to env + file.
- On 401/403 request failure, force token expiry (`CODEX_TOKEN_EXPIRES=0`) and retry.

## Format adaptation layer
`codex_proxy.py` maps Chat-Completions shape <-> Responses shape:

- `_messages_to_input(...)`:
  - `system` -> merged `instructions`
  - `user` text/image -> `input_text` / `input_image`
  - assistant tool calls -> `function_call`
  - tool messages -> `function_call_output`

- `_tools_to_responses_format(...)`:
  - Chat tool schema -> Responses tool schema (`type=function`, `name`, `description`, `parameters`)

- `_output_to_chat_message(...)`:
  - `output_text` -> assistant `content`
  - `function_call` -> Chat-style `tool_calls`

This keeps `loop.py` tool executor unchanged across providers.

## Network + SSE protocol
Request:
- `POST https://chatgpt.com/backend-api/codex/responses`
- `Authorization: Bearer <access_token>`
- `Accept: text/event-stream`
- payload includes `model`, `input`, `instructions`, `tools`, `tool_choice=auto`, `stream=true`, `store=false`, `reasoning={effort:"medium"}`

SSE parser accepts `event: response.completed` and parses that JSON payload.

## Retry and resilience
Inside proxy (`call_codex`, `MAX_RETRIES=2`):
- retries auth errors (401/403) with forced refresh,
- retries network/timeouts with backoff,
- retries SSE parse errors.

Outside proxy, `loop.py` also retries generic LLM calls (`_call_llm_with_retry`, default 3 attempts).

## Usage and cost semantics
Proxy maps usage from Responses API:
- `input_tokens` -> `prompt_tokens`
- `output_tokens` -> `completion_tokens`
- `total_tokens` -> `total_tokens`
- `cost = 0.0` (Codex OAuth path is treated as free in budget accounting)

## Tool-use behavior
When tools are available, proxy appends an explicit instruction hint telling model to use tools for actionable requests.
Purpose: increase tool-call reliability under very large prompts.

Returned tool calls are then handled by normal `loop.py` tool execution.

## Observability
Debug artifacts (best-effort):
- `/tmp/codex_debug.json` — outgoing payload + tool conversion sample
- `/tmp/codex_sse_raw.txt` — raw SSE body snapshot
- `/tmp/codex_response_debug.json` — parsed output summary

Plus normal loop event logging for tool calls, usage, retries.

## Key gotchas
1. Hardcoded OAuth `client_id` is an upstream coupling risk.
2. Dual token state (env + file) can cause staleness if ops overwrite env.
3. SSE parser is strict (`response.completed` required).
4. Codex reasoning effort in proxy is currently fixed to `medium`.
5. `/tmp` debug dumps may contain sensitive operational traces and need hygiene.

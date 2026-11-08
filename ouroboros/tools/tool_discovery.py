"""Discover callable tools and attach their schemas for the current task."""

from __future__ import annotations

from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry, ToolRegistry


def _list_available_tools(ctx: ToolContext, query: str = "", offset: int = 0) -> str:
    return "⚠️ Tool discovery is available in an active tool loop."


def _enable_tools(ctx: ToolContext, tools: str) -> str:
    return "⚠️ Tool loading is available in an active tool loop."


def bind_tool_discovery(registry: ToolRegistry, resident: list) -> None:
    def list_tools(ctx: ToolContext, query: str = "", offset: int = 0) -> str:
        if not isinstance(query, str) or type(offset) is not int or offset < 0:
            return "⚠️ Provide a text query and a nonnegative offset."
        search = query.strip().casefold()
        loaded = {schema["name"] if "name" in schema else schema["function"]["name"] for schema in resident}
        matches = []
        for schema in sorted(registry.schemas(), key=lambda row: row["name"]):
            name = schema["name"]
            description = " ".join(str(schema["function"].get("description", "")).split())
            if search in f"{name} {description}".casefold():
                state = "loaded" if name in loaded else "available"
                matches.append(f"- {name} [{state}]: {description[:100]}")
        if not matches:
            return "No callable tools match. Try another query or omit it to browse."
        page = matches[offset:offset + 10]
        next_offset = offset + len(page)
        footer = f"\nNext page: offset={next_offset} with the same query." if next_offset < len(matches) else ""
        return (f"{len(matches)} callable tools match. Use enable_tools for available schemas.\n"
                + "\n".join(page) + footer)

    def enable_tools(ctx: ToolContext, tools: str) -> str:
        allowed = {schema["name"]: schema for schema in registry.schemas()}
        loaded = {schema["name"] if "name" in schema else schema["function"]["name"] for schema in resident}
        enabled, already, missing = [], [], []
        for name in dict.fromkeys(item.strip() for item in tools.split(",") if item.strip()):
            if name not in allowed:
                missing.append(name)
            elif name in loaded:
                already.append(name)
            else:
                resident.append(allowed[name])
                enabled.append(name)
        parts = []
        if enabled:
            parts.append("✅ Available from the next request: " + ", ".join(enabled))
        if already:
            parts.append("Already loaded: " + ", ".join(already))
        if missing:
            parts.append("⚠️ Unavailable for this task: " + ", ".join(missing))
        return "\n".join(parts) if parts else "⚠️ Provide one or more tool names."

    registry.override_handler("list_available_tools", list_tools)
    registry.override_handler("enable_tools", enable_tools)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="list_available_tools",
            schema={
                "name": "list_available_tools",
                "description": (
                    "Find tools callable in this task, including schemas that are not loaded yet. "
                    "Search by name or purpose, or browse with an empty query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Optional name or purpose to search for."},
                        "offset": {"type": "integer", "minimum": 0, "description": "Offset for the next page."},
                    },
                    "required": [],
                },
            },
            handler=_list_available_tools,
        ),
        ToolEntry(
            name="enable_tools",
            schema={
                "name": "enable_tools",
                "description": (
                    "Enable specific additional tools by name (comma-separated). "
                    "Their schemas will be added to your active tool set for the "
                    "remainder of this task. Example: enable_tools(tools='multi_model_review,generate_evolution_stats')"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tools": {
                            "type": "string",
                            "description": "Comma-separated tool names to enable",
                        }
                    },
                    "required": ["tools"],
                },
            },
            handler=_enable_tools,
        ),
    ]

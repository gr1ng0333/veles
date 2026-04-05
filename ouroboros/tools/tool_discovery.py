"""Tool discovery meta-tools — lets the agent see and enable non-core tools."""

from __future__ import annotations
import json
import logging
import pathlib
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ouroboros.tools.registry import ToolContext, ToolEntry, CORE_TOOL_NAMES

if TYPE_CHECKING:
    from ouroboros.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

# Module-level registry reference — set by set_registry() after ToolRegistry is created.
# loop.py also overrides these handlers with closures that have access to per-loop state
# (e.g. the _enabled_extra_tools set); the module-level ref serves as a fallback for
# any context where the tool is called without going through run_llm_loop.
_registry: Optional["ToolRegistry"] = None


def set_registry(reg: "ToolRegistry") -> None:
    global _registry
    _registry = reg


def _list_available_tools(ctx: ToolContext, **kwargs) -> str:
    if _registry is None:
        return "Tool discovery not available in this context."
    non_core = _registry.list_non_core_tools()
    # Exclude the meta-tools themselves from the listing
    non_core = [t for t in non_core if t["name"] not in ("list_available_tools", "enable_tools")]
    if not non_core:
        return "All tools are already in your active set."
    lines = [f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"]
    for t in non_core:
        lines.append(f"- **{t['name']}**: {t['description'][:120]}")
    return "\n".join(lines)


def _enable_tools(ctx: ToolContext, tools: str = "", **kwargs) -> str:
    if _registry is None:
        return "Tool enablement not available in this context."
    names = [n.strip() for n in tools.split(",") if n.strip()]
    if not names:
        return "No tools specified."
    found = []
    not_found = []
    for name in names:
        schema = _registry.get_schema_by_name(name)
        if schema:
            found.append(f"{name}: {schema['function'].get('description', '')[:100]}")
        else:
            not_found.append(name)
    parts = []
    if found:
        parts.append("✅ Tools are registered and callable:\n" + "\n".join(f"  - {s}" for s in found))
    if not_found:
        parts.append(f"❌ Not found: {', '.join(not_found)}")
    return "\n".join(parts)


def _tool_map(ctx: ToolContext, filter: str = "", format: str = "text") -> str:
    """Show all registered tools grouped by source module.

    Provides complete self-awareness of the toolset:
    - All 290+ tools visible in one call (not just non-core)
    - Grouped by source module for navigation
    - Core vs extended distinction
    - Optional name filter for quick lookup

    Use cases:
      - "Which module provides tool X?" → tool_map(filter="X")
      - "Which modules have the most tools?" → tool_map()
      - "Is hot_spots registered?" → tool_map(filter="hot_spots")
      - Pre-evolution targeting: see which modules are richest
    """
    if _registry is None:
        return "tool_map: registry not available in this context."

    import importlib
    import pkgutil
    import ouroboros.tools as tools_pkg

    # Build module → [tool_name, ...] map
    module_tools: Dict[str, List[Dict[str, Any]]] = {}
    module_load_errors: List[str] = []

    for _importer, modname, _ispkg in pkgutil.iter_modules(tools_pkg.__path__):
        if modname.startswith("_") or modname == "registry":
            continue
        try:
            mod = importlib.import_module(f"ouroboros.tools.{modname}")
            if not hasattr(mod, "get_tools"):
                continue
            entries = mod.get_tools()
            tool_info = []
            for entry in entries:
                name = entry.name
                desc = entry.schema.get("description", "")
                # First line of description only
                desc_short = desc.split("\n")[0].strip()[:100]
                is_core = name in CORE_TOOL_NAMES
                if filter and filter.lower() not in name.lower() and filter.lower() not in modname.lower():
                    continue
                tool_info.append({
                    "name": name,
                    "desc": desc_short,
                    "core": is_core,
                })
            if tool_info:
                module_tools[modname] = tool_info
        except Exception as exc:
            module_load_errors.append(f"{modname}: {exc}")

    if not module_tools:
        if filter:
            return f"tool_map: no tools matching '{filter}' found."
        return "tool_map: no tools discovered."

    if format == "json":
        return json.dumps({
            "modules": module_tools,
            "load_errors": module_load_errors,
            "total_modules": len(module_tools),
            "total_tools": sum(len(v) for v in module_tools.values()),
        }, ensure_ascii=False, indent=2)

    # Text format — grouped by module, sorted by tool count desc
    lines: List[str] = []
    total_tools = sum(len(v) for v in module_tools.values())
    core_count = sum(1 for tools in module_tools.values() for t in tools if t["core"])
    header = f"## Tool Map — {total_tools} tools in {len(module_tools)} modules"
    if filter:
        header += f"  (filter: '{filter}')"
    lines.append(header)
    lines.append(f"   Core: {core_count}  Extended: {total_tools - core_count}\n")

    sorted_modules = sorted(module_tools.items(), key=lambda x: -len(x[1]))
    for modname, tools in sorted_modules:
        lines.append(f"📦 {modname}  ({len(tools)} tools)")
        for t in tools:
            marker = "●" if t["core"] else "○"
            lines.append(f"   {marker} {t['name']:<40}  {t['desc'][:80]}")
        lines.append("")

    if module_load_errors:
        lines.append(f"⚠ {len(module_load_errors)} modules failed to load:")
        for err in module_load_errors[:5]:
            lines.append(f"   {err}")

    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="list_available_tools",
            schema={
                "name": "list_available_tools",
                "description": (
                    "List all additional tools not currently in your active tool set. "
                    "Returns name + description for each. Use this to discover tools "
                    "you might need for specific tasks."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
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
        ToolEntry(
            name="tool_map",
            schema={
                "name": "tool_map",
                "description": (
                    "Show ALL registered tools grouped by source module. "
                    "Provides complete toolset self-awareness in one call: "
                    "290+ tools visible, grouped by module, core vs extended distinction.\n\n"
                    "Use cases:\n"
                    "- 'Which module provides tool X?' → tool_map(filter='X')\n"
                    "- 'Is hot_spots registered?' → tool_map(filter='hot_spots')\n"
                    "- See which modules are richest → tool_map()\n"
                    "- Evolution targeting: find under-tested modules\n\n"
                    "Unlike list_available_tools (shows non-core only), tool_map shows everything.\n"
                    "Parameters:\n"
                    "- filter: optional name/module substring filter\n"
                    "- format: 'text' (default) or 'json'"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filter": {
                            "type": "string",
                            "description": "Optional substring filter for tool name or module name.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format: 'text' (default) or 'json'.",
                        },
                    },
                    "required": [],
                },
            },
            handler=lambda ctx, **kw: _tool_map(ctx, **kw),
        ),
    ]

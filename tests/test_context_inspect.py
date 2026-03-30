"""Smoke tests for context_inspect tool."""
from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.tools.context_inspect import get_tools
from ouroboros.tools.registry import ToolContext


@pytest.fixture
def ctx():
    return ToolContext(
        repo_dir=pathlib.Path("/opt/veles"),
        drive_root=pathlib.Path("/opt/veles-data"),
    )


def test_tool_registration():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "context_inspect"
    assert tools[0].timeout_sec == 30


def test_schema_fields():
    tool = get_tools()[0]
    params = tool.schema["parameters"]["properties"]
    assert "include_preview" in params
    assert "task_type" in params


def test_basic_output(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx, task_type="user")
    data = json.loads(result)

    # Required top-level keys
    assert "grand_total_est" in data
    assert "soft_cap_tokens" in data
    assert "soft_cap_usage_pct" in data
    assert "dominant_block" in data
    assert "blocks" in data
    assert "note" in data

    blocks = data["blocks"]
    assert "Block0_Static_cached_1h" in blocks
    assert "Block1_SemiStable_cached" in blocks
    assert "Block2_Dynamic_uncached" in blocks


def test_block_structure(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx, task_type="user")
    data = json.loads(result)

    for bname, bdata in data["blocks"].items():
        assert "tokens_est" in bdata, f"{bname} missing tokens_est"
        assert "chars" in bdata, f"{bname} missing chars"
        assert "top_sections" in bdata, f"{bname} missing top_sections"
        assert "sections" in bdata, f"{bname} missing sections"
        assert bdata["tokens_est"] > 0, f"{bname} has 0 tokens"


def test_evolution_task_includes_readme(ctx):
    tool = get_tools()[0]

    result_user = tool.handler(ctx, task_type="user")
    result_evo = tool.handler(ctx, task_type="evolution")

    d_user = json.loads(result_user)
    d_evo = json.loads(result_evo)

    b0_user = d_user["blocks"]["Block0_Static_cached_1h"]
    b0_evo = d_evo["blocks"]["Block0_Static_cached_1h"]

    sections_user = {s["section"] for s in b0_user["sections"]}
    sections_evo = {s["section"] for s in b0_evo["sections"]}

    # evolution should include README.md in block0
    assert "README.md" in sections_evo
    # total tokens should be larger for evolution
    assert b0_evo["tokens_est"] >= b0_user["tokens_est"]


def test_total_is_sum_of_blocks(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx, task_type="user")
    data = json.loads(result)

    blocks = data["blocks"]
    block_sum = sum(b["tokens_est"] for b in blocks.values())
    # grand_total includes small user message stub (+10)
    assert abs(data["grand_total_est"] - block_sum) <= 20


def test_preview_off_by_default(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx)
    data = json.loads(result)

    for bdata in data["blocks"].values():
        for section in bdata["sections"]:
            assert "preview" not in section, f"preview should be absent by default in {section['section']}"


def test_preview_on(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx, include_preview=True)
    data = json.loads(result)

    found_preview = False
    for bdata in data["blocks"].values():
        for section in bdata["sections"]:
            if section["chars"] > 0 and "preview" in section:
                found_preview = True
                break
    assert found_preview, "Expected at least one section with preview when include_preview=True"

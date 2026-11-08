"""Smoke tests for context_inspect tool."""
from __future__ import annotations

import json

import pytest

from ouroboros.tools.context_inspect import get_tools
from ouroboros.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(
        repo_dir=tmp_path / "repo",
        drive_root=tmp_path / "drive",
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
    assert data["startup_target_tokens"] > 0
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


def test_evolution_and_user_share_static_startup(ctx):
    tool = get_tools()[0]

    result_user = tool.handler(ctx, task_type="user")
    result_evo = tool.handler(ctx, task_type="evolution")

    d_user = json.loads(result_user)
    d_evo = json.loads(result_evo)

    b0_user = d_user["blocks"]["Block0_Static_cached_1h"]
    b0_evo = d_evo["blocks"]["Block0_Static_cached_1h"]

    sections_user = {s["section"] for s in b0_user["sections"]}
    sections_evo = {s["section"] for s in b0_evo["sections"]}

    assert sections_evo == sections_user
    assert "README.md" not in sections_evo


def test_total_is_sum_of_blocks(ctx):
    tool = get_tools()[0]
    result = tool.handler(ctx, task_type="user")
    data = json.loads(result)

    blocks = data["blocks"]
    block_sum = sum(b["tokens_est"] for b in blocks.values())
    assert data["grand_total_est"] == block_sum


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

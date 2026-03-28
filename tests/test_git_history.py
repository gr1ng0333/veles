"""Tests for git_history tool."""
import json
from pathlib import Path

import pytest

from ouroboros.tools.git_history import (
    _log_mode, _reflog_mode, _tags_mode, _show_mode,
    _file_log_mode, _branch_compare_mode, get_tools,
)

REPO = Path("/opt/veles")

class FakeCtx:
    repo_dir = str(REPO)

ctx = FakeCtx()


def test_get_tools_registers_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "git_history"


def test_log_basic():
    r = _log_mode(ctx, branch="", limit=5, since="", until="", author="",
                  grep="", path="", include_stats=False, include_body=False)
    d = json.loads(r)
    assert d["mode"] == "log"
    assert d["count"] >= 1
    commits = d["commits"]
    assert len(commits) >= 1
    c = commits[0]
    assert "sha" in c and len(c["sha"]) == 40
    assert "subject" in c
    assert "date" in c
    assert "author" in c


def test_log_sha_short():
    r = _log_mode(ctx, branch="", limit=3, since="", until="", author="",
                  grep="", path="", include_stats=False, include_body=False)
    d = json.loads(r)
    for c in d["commits"]:
        assert len(c["sha_short"]) == 8
        assert c["sha"].startswith(c["sha_short"])


def test_log_grep_filter():
    r = _log_mode(ctx, branch="", limit=20, since="", until="", author="",
                  grep="feat", path="", include_stats=False, include_body=False)
    d = json.loads(r)
    for c in d["commits"]:
        assert "feat" in c["subject"].lower()


def test_log_with_stats():
    r = _log_mode(ctx, branch="", limit=3, since="", until="", author="",
                  grep="", path="", include_stats=True, include_body=False)
    d = json.loads(r)
    # At least one commit should have stats
    has_stats = any("stats" in c for c in d["commits"])
    assert has_stats
    # Verify stats structure
    for c in d["commits"]:
        if "stats" in c:
            s = c["stats"]
            assert "insertions" in s
            assert "deletions" in s
            assert "files" in s


def test_reflog_basic():
    r = _reflog_mode(ctx, limit=10, since="")
    d = json.loads(r)
    assert d["mode"] == "reflog"
    assert d["count"] >= 1
    e = d["entries"][0]
    assert "sha" in e and "ref" in e and "action" in e


def test_tags_basic():
    r = _tags_mode(ctx, limit=5, pattern="")
    d = json.loads(r)
    assert d["mode"] == "tags"
    assert d["count"] >= 1
    t = d["tags"][0]
    assert "tag" in t and "date" in t


def test_tags_pattern():
    r = _tags_mode(ctx, limit=10, pattern="v6.*")
    d = json.loads(r)
    for t in d["tags"]:
        assert t["tag"].startswith("v6.")


def test_show_head():
    r = _show_mode(ctx, ref="HEAD", include_diff=False)
    d = json.loads(r)
    assert d["mode"] == "show"
    assert "commit" in d
    c = d["commit"]
    assert "sha" in c and len(c["sha"]) == 40
    assert "stats" in c


def test_show_with_diff():
    r = _show_mode(ctx, ref="HEAD", include_diff=True)
    d = json.loads(r)
    assert d["mode"] == "show"
    c = d["commit"]
    assert "diff" in c
    assert len(c["diff"]) > 10


def test_show_invalid_ref():
    r = _show_mode(ctx, ref="this-ref-does-not-exist-xyz", include_diff=False)
    d = json.loads(r)
    assert "error" in d


def test_file_log():
    r = _file_log_mode(ctx, path="VERSION", limit=5, include_diff=False)
    d = json.loads(r)
    assert d["mode"] == "file_log"
    assert d["file"] == "VERSION"
    assert d["count"] >= 1


def test_file_log_no_path():
    r = _file_log_mode(ctx, path="", limit=5, include_diff=False)
    d = json.loads(r)
    assert "error" in d


def test_compare_head_vs_origin():
    r = _branch_compare_mode(ctx, ref1="HEAD", ref2="origin/veles", limit=10)
    d = json.loads(r)
    assert d["mode"] == "compare"
    assert "ahead" in d and "behind" in d
    assert d["ref1"] == "HEAD"
    assert d["ref2"] == "origin/veles"


def test_log_limit_respected():
    r = _log_mode(ctx, branch="", limit=3, since="", until="", author="",
                  grep="", path="", include_stats=False, include_body=False)
    d = json.loads(r)
    assert d["count"] <= 3


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as ex:
            print(f"  FAIL  {name}: {ex}")
    print(f"\n{passed}/{len(tests)} passed")

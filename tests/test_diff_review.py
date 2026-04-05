"""Tests for ouroboros/tools/diff_review.py"""
from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.tools.diff_review import (
    _Finding,
    _check_complexity,
    _check_docs,
    _check_exceptions,
    _check_security,
    _check_types,
    _cyclomatic_complexity,
    _diff_review,
    _format_text,
    _is_public,
    _overlaps,
    _parse_changed_lines,
    get_tools,
)
from ouroboros.tools.registry import ToolContext


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_func(src: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Parse a single function definition from source string."""
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise ValueError("No function found in source")


def _parse_class(src: str) -> ast.ClassDef:
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise ValueError("No class found in source")


def _mock_ctx(tmp_path: Path) -> ToolContext:
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    return ctx


# ── _parse_changed_lines ───────────────────────────────────────────────────────

SAMPLE_DIFF = """\
diff --git a/ouroboros/foo.py b/ouroboros/foo.py
index aaa..bbb 100644
--- a/ouroboros/foo.py
+++ b/ouroboros/foo.py
@@ -10,6 +10,8 @@ def old():
     x = 1
+    y = 2
+    z = 3
     return x
"""

SAMPLE_DIFF_MULTI = """\
diff --git a/ouroboros/a.py b/ouroboros/a.py
index 000..111 100644
--- a/ouroboros/a.py
+++ b/ouroboros/a.py
@@ -1,3 +1,4 @@
+import os
 x = 1
 y = 2
 z = 3
diff --git a/ouroboros/b.py b/ouroboros/b.py
index 222..333 100644
--- a/ouroboros/b.py
+++ b/ouroboros/b.py
@@ -5,3 +5,4 @@
 a = 1
+b = 2
 c = 3
"""

NON_PY_DIFF = """\
diff --git a/README.md b/README.md
index aaa..bbb 100644
--- a/README.md
+++ b/README.md
@@ -1,2 +1,3 @@
+# New heading
 hello
"""


def test_parse_changed_lines_basic():
    result = _parse_changed_lines(SAMPLE_DIFF)
    assert "ouroboros/foo.py" in result
    lines = result["ouroboros/foo.py"]
    # Lines 11 and 12 should be in the added set
    assert 11 in lines or 12 in lines


def test_parse_changed_lines_multi_file():
    result = _parse_changed_lines(SAMPLE_DIFF_MULTI)
    assert "ouroboros/a.py" in result
    assert "ouroboros/b.py" in result
    # a.py: line 1 added (import os)
    assert 1 in result["ouroboros/a.py"]
    # b.py: line 6 added (b = 2)
    assert 6 in result["ouroboros/b.py"]


def test_parse_changed_lines_non_python_ignored():
    result = _parse_changed_lines(NON_PY_DIFF)
    assert len(result) == 0


def test_parse_changed_lines_empty():
    assert _parse_changed_lines("") == {}


def test_parse_changed_lines_only_removals():
    diff = """\
diff --git a/x.py b/x.py
index aaa..bbb 100644
--- a/x.py
+++ b/x.py
@@ -1,3 +1,2 @@
 keep = 1
-removed = 2
 also_keep = 3
"""
    result = _parse_changed_lines(diff)
    # Only removals — no added lines
    assert result.get("x.py", set()) == set()


# ── _overlaps ─────────────────────────────────────────────────────────────────

def test_overlaps_exact():
    assert _overlaps(5, 10, {7})


def test_overlaps_boundary_start():
    assert _overlaps(5, 10, {5})


def test_overlaps_boundary_end():
    assert _overlaps(5, 10, {10})


def test_no_overlap():
    assert not _overlaps(5, 10, {11, 12, 4})


def test_overlaps_empty_set():
    assert not _overlaps(1, 100, set())


# ── _is_public ────────────────────────────────────────────────────────────────

def test_is_public_normal():
    assert _is_public("my_function")


def test_is_public_private():
    assert not _is_public("_helper")


def test_is_public_dunder():
    assert not _is_public("__init__")


# ── _cyclomatic_complexity ────────────────────────────────────────────────────

def test_complexity_simple():
    func = _parse_func("def f(): return 1")
    assert _cyclomatic_complexity(func) == 1


def test_complexity_if():
    func = _parse_func("""
    def f(x):
        if x:
            return 1
        return 2
    """)
    assert _cyclomatic_complexity(func) == 2


def test_complexity_nested():
    func = _parse_func("""
    def f(x, y):
        if x:
            if y:
                return 1
            return 2
        return 3
    """)
    assert _cyclomatic_complexity(func) == 3


def test_complexity_for_loop():
    func = _parse_func("""
    def f(items):
        for item in items:
            pass
    """)
    assert _cyclomatic_complexity(func) == 2


def test_complexity_bool_op():
    func = _parse_func("""
    def f(a, b, c):
        return a and b and c
    """)
    # BoolOp with 3 values adds 2
    assert _cyclomatic_complexity(func) == 3


# ── _check_security ───────────────────────────────────────────────────────────

def test_security_eval_dynamic():
    func = _parse_func("""
    def f(user_input):
        return eval(user_input)
    """)
    findings = _check_security(func, "x.py", [], {"security"})
    assert any("eval" in f.message.lower() for f in findings)


def test_security_eval_static_ok():
    func = _parse_func("""
    def f():
        return eval("1+1")
    """)
    findings = _check_security(func, "x.py", [], {"security"})
    assert not findings


def test_security_subprocess_shell_true():
    func = _parse_func("""
    import subprocess
    def f(cmd):
        subprocess.run(cmd, shell=True)
    """)
    findings = _check_security(func, "x.py", [], {"security"})
    assert any("shell=True" in f.message for f in findings)


def test_security_subprocess_no_shell():
    func = _parse_func("""
    import subprocess
    def f(cmd):
        subprocess.run(["ls", "-la"])
    """)
    findings = _check_security(func, "x.py", [], {"security"})
    assert not any("shell=True" in f.message for f in findings)


def test_security_pickle():
    func = _parse_func("""
    import pickle
    def f(data):
        return pickle.loads(data)
    """)
    findings = _check_security(func, "x.py", [], {"security"})
    assert any("pickle" in f.message.lower() for f in findings)


def test_security_hardcoded_secret():
    func = _parse_func("""
    def f():
        password = "supersecret123"
        return password
    """)
    source_lines = textwrap.dedent("""
    def f():
        password = "supersecret123"
        return password
    """).splitlines()
    findings = _check_security(func, "x.py", source_lines, {"security"})
    assert any("password" in f.message.lower() for f in findings)


def test_security_env_var_not_flagged():
    func = _parse_func("""
    def f():
        password = os.environ.get("PASSWORD")
        return password
    """)
    source_lines = textwrap.dedent("""
    def f():
        password = os.environ.get("PASSWORD")
        return password
    """).splitlines()
    findings = _check_security(func, "x.py", source_lines, {"security"})
    assert not any("hardcoded" in f.message.lower() for f in findings)


def test_security_check_disabled():
    func = _parse_func("""
    def f(user_input):
        eval(user_input)
    """)
    findings = _check_security(func, "x.py", [], set())
    assert findings == []


# ── _check_exceptions ─────────────────────────────────────────────────────────

def test_exceptions_bare_except():
    func = _parse_func("""
    def f():
        try:
            pass
        except:
            pass
    """)
    findings = _check_exceptions(func, "x.py", {"exceptions"})
    assert any("bare except" in f.message.lower() for f in findings)


def test_exceptions_broad_no_reraise():
    func = _parse_func("""
    def f():
        try:
            pass
        except Exception:
            print("error")
    """)
    findings = _check_exceptions(func, "x.py", {"exceptions"})
    assert any("without re-raise" in f.message for f in findings)


def test_exceptions_broad_with_reraise_ok():
    func = _parse_func("""
    def f():
        try:
            pass
        except Exception:
            raise
    """)
    findings = _check_exceptions(func, "x.py", {"exceptions"})
    # Should not flag broad_except when re-raise is present
    broad = [f for f in findings if "without re-raise" in f.message]
    assert not broad


def test_exceptions_silent():
    func = _parse_func("""
    def f():
        try:
            pass
        except ValueError:
            pass
    """)
    findings = _check_exceptions(func, "x.py", {"exceptions"})
    assert any("silently discarded" in f.message for f in findings)


def test_exceptions_check_disabled():
    func = _parse_func("""
    def f():
        try:
            pass
        except:
            pass
    """)
    findings = _check_exceptions(func, "x.py", set())
    assert findings == []


# ── _check_types ─────────────────────────────────────────────────────────────

def test_types_missing_all():
    func = _parse_func("""
    def public_func(x, y):
        return x + y
    """)
    findings = _check_types(func, "x.py", {"types"})
    messages = [f.message for f in findings]
    assert any("'x'" in m for m in messages)
    assert any("'y'" in m for m in messages)
    assert any("return" in m.lower() for m in messages)


def test_types_fully_annotated_ok():
    func = _parse_func("""
    def public_func(x: int, y: int) -> int:
        return x + y
    """)
    findings = _check_types(func, "x.py", {"types"})
    assert findings == []


def test_types_private_skipped():
    func = _parse_func("""
    def _private(x, y):
        return x + y
    """)
    findings = _check_types(func, "x.py", {"types"})
    assert findings == []


def test_types_self_cls_skipped():
    func = _parse_func("""
    def method(self, x: int) -> None:
        pass
    """)
    findings = _check_types(func, "x.py", {"types"})
    # self should not be flagged, only missing return annotation matters
    param_findings = [f for f in findings if "self" in f.message]
    assert not param_findings


def test_types_check_disabled():
    func = _parse_func("""
    def public_func(x, y):
        return x + y
    """)
    findings = _check_types(func, "x.py", set())
    assert findings == []


# ── _check_docs ──────────────────────────────────────────────────────────────

def test_docs_missing_function():
    func = _parse_func("""
    def public_func(x):
        return x
    """)
    findings = _check_docs(func, "x.py", {"docs"})
    assert any("docstring" in f.message.lower() for f in findings)


def test_docs_present_ok():
    func = _parse_func("""
    def public_func(x):
        \"\"\"Does something useful.\"\"\"
        return x
    """)
    findings = _check_docs(func, "x.py", {"docs"})
    assert findings == []


def test_docs_private_skipped():
    func = _parse_func("""
    def _helper(x):
        return x
    """)
    findings = _check_docs(func, "x.py", {"docs"})
    assert findings == []


def test_docs_class_missing():
    cls = _parse_class("""
    class MyService:
        pass
    """)
    findings = _check_docs(cls, "x.py", {"docs"})
    assert any("class" in f.message.lower() for f in findings)


def test_docs_check_disabled():
    func = _parse_func("""
    def public_func(x):
        return x
    """)
    findings = _check_docs(func, "x.py", set())
    assert findings == []


# ── _check_complexity ─────────────────────────────────────────────────────────

def test_complexity_above_threshold():
    # Build a function with CC > 10
    branches = "\n    ".join(f"if x == {i}: pass" for i in range(12))
    src = f"""
def complex_func(x):
    {branches}
"""
    func = _parse_func(src)
    findings = _check_complexity(func, "x.py", {"complexity"})
    assert findings
    assert findings[0].severity == "medium"


def test_complexity_at_threshold_ok():
    func = _parse_func("""
    def simple(x):
        if x:
            return 1
        return 2
    """)
    findings = _check_complexity(func, "x.py", {"complexity"})
    assert findings == []


def test_complexity_check_disabled():
    branches = "\n    ".join(f"if x == {i}: pass" for i in range(12))
    src = f"""
def complex_func(x):
    {branches}
"""
    func = _parse_func(src)
    findings = _check_complexity(func, "x.py", set())
    assert findings == []


# ── _format_text ─────────────────────────────────────────────────────────────

def test_format_text_no_findings():
    result = _format_text([], 3, 5, "HEAD", {})
    assert "✅" in result
    assert "HEAD" in result


def test_format_text_with_findings():
    findings = [
        _Finding(file="a.py", line=10, check="security", severity="high",
                 message="eval() danger", function="foo"),
        _Finding(file="b.py", line=20, check="types", severity="low",
                 message="Missing annotation", function="bar"),
    ]
    result = _format_text(findings, 2, 3, "HEAD~1", {})
    assert "a.py:10" in result
    assert "b.py:20" in result
    assert "HIGH" in result
    assert "LOW" in result


def test_format_text_severity_order():
    findings = [
        _Finding(file="a.py", line=1, check="types", severity="low",
                 message="low issue", function="f"),
        _Finding(file="a.py", line=2, check="security", severity="high",
                 message="high issue", function="f"),
    ]
    result = _format_text(findings, 1, 1, "HEAD", {})
    # HIGH section must come before LOW section
    assert result.index("HIGH") < result.index("LOW")


# ── get_tools ────────────────────────────────────────────────────────────────

def test_get_tools_returns_one():
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "diff_review"


def test_get_tools_schema_valid():
    tool = get_tools()[0]
    schema = tool.schema
    assert schema["name"] == "diff_review"
    params = schema["parameters"]["properties"]
    assert "ref" in params
    assert "checks" in params
    assert "min_severity" in params
    assert "format" in params


def test_get_tools_handler_callable():
    tool = get_tools()[0]
    assert callable(tool.handler)


# ── Integration: _diff_review with mocked git ─────────────────────────────────

FAKE_DIFF_WITH_FUNC = """\
diff --git a/ouroboros/sample.py b/ouroboros/sample.py
index 000..111 100644
--- a/ouroboros/sample.py
+++ b/ouroboros/sample.py
@@ -1,3 +1,10 @@
+def insecure(user_input):
+    return eval(user_input)
+
+def annotated(x: int) -> int:
+    \"\"\"Does stuff.\"\"\"
+    return x
"""


def test_diff_review_no_diff(tmp_path):
    """When git returns no diff, tool should report 'nothing to review'."""
    ctx = _mock_ctx(tmp_path)
    with patch("ouroboros.tools.diff_review._run_git_diff", return_value=""):
        result = _diff_review(ctx, ref="HEAD")
    assert "No diff found" in result or "Nothing to review" in result


def test_diff_review_bad_severity(tmp_path):
    ctx = _mock_ctx(tmp_path)
    result = _diff_review(ctx, min_severity="extreme")
    assert "Unknown min_severity" in result


def test_diff_review_bad_checks(tmp_path):
    ctx = _mock_ctx(tmp_path)
    result = _diff_review(ctx, checks="magic,unknown")
    assert "Unknown checks" in result


def test_diff_review_json_output(tmp_path):
    """JSON output should be valid and contain expected keys."""
    ctx = _mock_ctx(tmp_path)

    # Create a fake Python file with the insecure function
    sample_file = tmp_path / "ouroboros" / "sample.py"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text(
        "def insecure(user_input):\n    return eval(user_input)\n"
    )

    with patch("ouroboros.tools.diff_review._run_git_diff", return_value=FAKE_DIFF_WITH_FUNC):
        result = _diff_review(ctx, ref="HEAD", format="json")

    data = json.loads(result)
    assert "findings" in data
    assert "files_changed" in data
    assert "functions_reviewed" in data
    assert "total_findings" in data


def test_diff_review_detects_security_issue(tmp_path):
    """Security check should catch eval() in a changed function."""
    ctx = _mock_ctx(tmp_path)

    sample_file = tmp_path / "ouroboros" / "sample.py"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text(
        "def insecure(user_input):\n    return eval(user_input)\n"
    )

    with patch("ouroboros.tools.diff_review._run_git_diff", return_value=FAKE_DIFF_WITH_FUNC):
        result = _diff_review(ctx, ref="HEAD", checks="security")

    assert "eval" in result.lower() or "injection" in result.lower() or "SECURITY" in result


def test_diff_review_min_severity_filters(tmp_path):
    """min_severity=high should suppress low/medium findings."""
    ctx = _mock_ctx(tmp_path)

    sample_file = tmp_path / "ouroboros" / "sample.py"
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    sample_file.write_text(
        "def annotated(x: int) -> int:\n    return x\n"
    )

    # A diff that touches only the annotated function (no issues)
    diff = """\
diff --git a/ouroboros/sample.py b/ouroboros/sample.py
index 000..111 100644
--- a/ouroboros/sample.py
+++ b/ouroboros/sample.py
@@ -1,2 +1,3 @@
 def annotated(x: int) -> int:
+    \"\"\"Updated docstring.\"\"\"
     return x
"""
    with patch("ouroboros.tools.diff_review._run_git_diff", return_value=diff):
        result = _diff_review(ctx, ref="HEAD", min_severity="high")

    # Should find no HIGH findings — file is clean
    assert "✅" in result or "0 finding" in result or "No issues" in result

"""module_health — Aggregated health card for a single module.

Combines five analysis dimensions into one report:
  1. Tech debt    — oversized functions, high complexity, deep nesting, too many params
  2. Dead code    — unused imports, dead private symbols
  3. Change impact — blast-radius risk tier, direct/transitive dependents
  4. Git churn    — commit frequency in the last N days (instability signal)
  5. Test coverage — which test files exist for this module

Use when you want a complete picture of a module before modifying it,
or to decide which modules to refactor next.  Instead of calling four separate
tools and assembling the picture manually, one call gives you everything.

Health score: 0–100
  90–100 : A — clean and stable
  75–89  : B — minor issues
  60–74  : C — needs attention
  40–59  : D — significant problems
  0–39   : F — critical state

Penalty model (deducted from 100):
  tech_debt:
    - each oversized function  : -3 (cap -15)
    - each high-complexity fn  : -3 (cap -15)
    - each deep-nesting fn     : -2 (cap -10)
    - each too-many-params fn  : -2 (cap -10)
    - module is oversized      : -10
  dead_code:
    - each unused import       : -1 (cap -10)
    - each dead private        : -2 (cap -10)
  change_impact:
    - CRITICAL risk tier       : -8
    - HIGH risk tier           : -4
    - >50 transitive deps      : -5
  churn:
    - >20 commits / window     : -8
    - 10–20 commits / window   : -5
    - 5–9 commits / window     : -2
  coverage:
    - no test file found       : -10
    - test file exists         : +0

Examples:
    module_health(target="ouroboros/tools/registry.py")
    module_health(target="ouroboros/loop_runtime.py")
    module_health(target="ouroboros/context.py", days=14)
    module_health(target="ouroboros/tools/registry.py", format="json")
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

# ── Constants ─────────────────────────────────────────────────────────────────

_REPO_DIR = pathlib.Path(os.environ.get("REPO_DIR", "/opt/veles"))
_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache",
               "node_modules", ".venv", "venv", "dist", "build"}

# BIBLE P5 thresholds
_MAX_FUNCTION_LINES = 150
_MAX_PARAMS = 8
_MAX_MODULE_LINES = 1000
_HIGH_COMPLEXITY_THRESHOLD = 15
_MAX_NESTING = 5


# ── File resolution ───────────────────────────────────────────────────────────

def _resolve_file(target: str, repo_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Resolve target (file path or module name) to an absolute Path."""
    # Direct path
    candidate = repo_dir / target
    if candidate.exists() and candidate.suffix == ".py":
        return candidate

    # Try without leading slash
    if target.startswith("/"):
        candidate = pathlib.Path(target)
        if candidate.exists():
            return candidate

    # Dotted module name: ouroboros.tools.registry → ouroboros/tools/registry.py
    dotted = target.replace(".", "/")
    for base in [repo_dir, repo_dir / "ouroboros", repo_dir / "supervisor"]:
        candidate = base / f"{dotted}.py"
        if candidate.exists():
            return candidate

    # Stem search (tools.registry → .../tools/registry.py)
    stem = target.split(".")[-1]
    for pkg in [repo_dir / "ouroboros", repo_dir / "supervisor"]:
        if not pkg.exists():
            continue
        for f in pkg.rglob(f"{stem}.py"):
            if any(p in _SKIP_DIRS for p in f.parts):
                continue
            return f

    return None


def _relative(path: pathlib.Path, repo_dir: pathlib.Path) -> str:
    try:
        return str(path.relative_to(repo_dir))
    except ValueError:
        return str(path)


# ── Signal 1: Tech debt ───────────────────────────────────────────────────────

def _cyclomatic(node: ast.AST) -> int:
    count = 0
    for n in ast.walk(node):
        if isinstance(n, (ast.If, ast.For, ast.While, ast.With,
                          ast.Try, ast.ExceptHandler, ast.Assert,
                          ast.comprehension)):
            count += 1
        elif isinstance(n, ast.BoolOp):
            count += len(n.values) - 1
    return count


def _max_nesting(node: ast.AST) -> int:
    _NESTING = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.ExceptHandler)

    def _depth(n: ast.AST, cur: int) -> int:
        if isinstance(n, _NESTING):
            cur += 1
        return max((cur,) + tuple(_depth(c, cur) for c in ast.iter_child_nodes(n)))

    return _depth(node, 0)


def _param_count(args: ast.arguments) -> int:
    all_args = args.posonlyargs + args.args + args.kwonlyargs
    params = [a for a in all_args if a.arg not in ("self", "cls")]
    if args.vararg:
        params.append(args.vararg)
    if args.kwarg:
        params.append(args.kwarg)
    return len(params)


def _scan_tech_debt(path: pathlib.Path) -> Dict[str, Any]:
    """Return tech-debt issues for a single file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"error": str(exc), "oversized_module": False,
                "oversized_functions": [], "high_complexity": [],
                "deep_nesting": [], "too_many_params": [], "loc": 0}

    lines = source.splitlines()
    loc = len(lines)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return {"error": f"SyntaxError: {exc}", "oversized_module": loc > _MAX_MODULE_LINES,
                "oversized_functions": [], "high_complexity": [],
                "deep_nesting": [], "too_many_params": [], "loc": loc}

    oversized_funcs: List[Dict] = []
    high_cx: List[Dict] = []
    deep_nest: List[Dict] = []
    too_many: List[Dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start)
        fn_lines = end - start + 1
        cx = _cyclomatic(node)
        depth = _max_nesting(node)
        nparams = _param_count(node.args)

        if fn_lines > _MAX_FUNCTION_LINES:
            oversized_funcs.append({"name": node.name, "line": start, "lines": fn_lines})
        if cx >= _HIGH_COMPLEXITY_THRESHOLD:
            high_cx.append({"name": node.name, "line": start, "complexity": cx})
        if depth > _MAX_NESTING:
            deep_nest.append({"name": node.name, "line": start, "depth": depth})
        if nparams > _MAX_PARAMS:
            too_many.append({"name": node.name, "line": start, "params": nparams})

    return {
        "loc": loc,
        "oversized_module": loc > _MAX_MODULE_LINES,
        "oversized_functions": oversized_funcs,
        "high_complexity": high_cx,
        "deep_nesting": deep_nest,
        "too_many_params": too_many,
    }


# ── Signal 2: Dead code ───────────────────────────────────────────────────────

def _load_names(tree: ast.Module) -> set:
    used: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            curr = node
            while isinstance(curr, ast.Attribute):
                curr = curr.value
            if isinstance(curr, ast.Name) and isinstance(curr.ctx, ast.Load):
                used.add(curr.id)
    return used


def _all_export_names(tree: ast.Module) -> set:
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                names.add(elt.value)
    return names


def _type_checking_names(tree: ast.Module) -> set:
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (
            (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        )
        if not is_tc:
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
    return names


def _scan_dead_code(path: pathlib.Path, repo_dir: pathlib.Path) -> Dict[str, Any]:
    """Return dead-code issues for a single file."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception as exc:
        return {"error": str(exc), "unused_imports": [], "dead_privates": []}

    exported = _all_export_names(tree)
    tc_names = _type_checking_names(tree)
    used_names = _load_names(tree)

    # Build cross-repo external-import set for privates
    externally_used: set = set()
    for pkg in [repo_dir / "ouroboros", repo_dir / "supervisor"]:
        if not pkg.exists():
            continue
        for f in pkg.rglob("*.py"):
            if any(p in _SKIP_DIRS for p in f.parts):
                continue
            try:
                s = f.read_text(encoding="utf-8", errors="replace")
                t = ast.parse(s)
            except Exception:
                continue
            for node in ast.walk(t):
                if not isinstance(node, ast.ImportFrom):
                    continue
                for alias in node.names:
                    nm = alias.name
                    if nm.startswith("_") and not nm.startswith("__"):
                        externally_used.add(nm)
                    if alias.asname and alias.asname.startswith("_") and not alias.asname.startswith("__"):
                        externally_used.add(alias.asname)

    # Unused imports
    unused_imports: List[Dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                stmt = f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                if (name not in exported and name not in tc_names
                        and name != "_" and not (name.startswith("__") and name.endswith("__"))
                        and name not in used_names):
                    unused_imports.append({"name": name, "line": node.lineno, "stmt": stmt})
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                stmt = f"from {node.module or ''} import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
                if (name not in exported and name not in tc_names
                        and name != "_" and not (name.startswith("__") and name.endswith("__"))
                        and name not in used_names):
                    unused_imports.append({"name": name, "line": node.lineno, "stmt": stmt})

    # Dead privates (top-level only)
    dead_privates: List[Dict] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not node.name.startswith("__"):
                if node.name not in exported and node.name not in externally_used and node.name not in used_names:
                    dead_privates.append({"name": node.name, "line": node.lineno, "kind": "function"})
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_") and not node.name.startswith("__"):
                if node.name not in exported and node.name not in externally_used and node.name not in used_names:
                    dead_privates.append({"name": node.name, "line": node.lineno, "kind": "class"})

    return {"unused_imports": unused_imports, "dead_privates": dead_privates}


# ── Signal 3: Change impact ───────────────────────────────────────────────────

def _scan_change_impact(path: pathlib.Path, repo_dir: pathlib.Path) -> Dict[str, Any]:
    """Return simplified blast-radius info for this file."""
    try:
        rel = _relative(path, repo_dir)
        from ouroboros.tools.change_impact import _change_impact as _ci
        result_json = _ci(None, target=rel, format="json", depth=5)  # type: ignore[arg-type]
        data = json.loads(result_json)
        return {
            "overall_risk": data.get("overall_risk", "UNKNOWN"),
            "direct_count": data.get("direct_count", 0),
            "transitive_count": data.get("transitive_count", 0),
            "recommended_tests": data.get("recommended_tests", []),
        }
    except Exception as exc:
        return {
            "overall_risk": "UNKNOWN",
            "direct_count": 0,
            "transitive_count": 0,
            "recommended_tests": [],
            "error": str(exc),
        }


# ── Signal 4: Git churn ───────────────────────────────────────────────────────

def _scan_churn(rel_path: str, repo_dir: pathlib.Path, days: int) -> int:
    """Return commit count for this file in the last N days."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--format=", "--name-only", "--", rel_path],
            capture_output=True, text=True, cwd=str(repo_dir), timeout=10,
        )
        # Each non-empty line in output is one file reference per commit
        # git log --name-only prints filename once per commit
        hits = [ln.strip() for ln in result.stdout.splitlines()
                if ln.strip() == rel_path or ln.strip().endswith("/" + rel_path.split("/")[-1])]
        return len(hits)
    except Exception:
        return 0


# ── Signal 5: Test coverage ───────────────────────────────────────────────────

def _scan_tests(path: pathlib.Path, repo_dir: pathlib.Path) -> List[str]:
    """Find test files that likely cover this module."""
    stem = path.stem  # e.g. "registry"
    tests_dir = repo_dir / "tests"
    if not tests_dir.exists():
        return []
    found = []
    for f in sorted(tests_dir.glob("test_*.py")):
        if f.stem == f"test_{stem}" or stem in f.stem:
            found.append(_relative(f, repo_dir))
    return found


# ── Scoring ───────────────────────────────────────────────────────────────────

def _compute_score(
    debt: Dict[str, Any],
    dead: Dict[str, Any],
    impact: Dict[str, Any],
    churn: int,
    tests: List[str],
) -> Tuple[int, str, List[str]]:
    """Compute health score (0–100), grade (A–F), and penalty breakdown."""
    penalty = 0
    breakdown: List[str] = []

    # ── Tech debt ──
    n_oversize = len(debt.get("oversized_functions", []))
    if n_oversize > 0:
        p = min(15, n_oversize * 3)
        penalty += p
        breakdown.append(f"oversized_functions: {n_oversize} → -{p}")

    n_hcx = len(debt.get("high_complexity", []))
    if n_hcx > 0:
        p = min(15, n_hcx * 3)
        penalty += p
        breakdown.append(f"high_complexity: {n_hcx} → -{p}")

    n_nest = len(debt.get("deep_nesting", []))
    if n_nest > 0:
        p = min(10, n_nest * 2)
        penalty += p
        breakdown.append(f"deep_nesting: {n_nest} → -{p}")

    n_params = len(debt.get("too_many_params", []))
    if n_params > 0:
        p = min(10, n_params * 2)
        penalty += p
        breakdown.append(f"too_many_params: {n_params} → -{p}")

    if debt.get("oversized_module"):
        penalty += 10
        breakdown.append(f"oversized_module: {debt.get('loc', '?')} lines → -10")

    # ── Dead code ──
    n_imports = len(dead.get("unused_imports", []))
    if n_imports > 0:
        p = min(10, n_imports)
        penalty += p
        breakdown.append(f"unused_imports: {n_imports} → -{p}")

    n_privates = len(dead.get("dead_privates", []))
    if n_privates > 0:
        p = min(10, n_privates * 2)
        penalty += p
        breakdown.append(f"dead_privates: {n_privates} → -{p}")

    # ── Change impact ──
    risk = impact.get("overall_risk", "UNKNOWN")
    if risk == "CRITICAL":
        penalty += 8
        breakdown.append(f"impact_risk: {risk} → -8")
    elif risk == "HIGH":
        penalty += 4
        breakdown.append(f"impact_risk: {risk} → -4")

    trans = impact.get("transitive_count", 0)
    if trans > 50:
        penalty += 5
        breakdown.append(f"transitive_deps: {trans} → -5")

    # ── Git churn ──
    if churn > 20:
        penalty += 8
        breakdown.append(f"churn_{churn}x → -8")
    elif churn >= 10:
        penalty += 5
        breakdown.append(f"churn_{churn}x → -5")
    elif churn >= 5:
        penalty += 2
        breakdown.append(f"churn_{churn}x → -2")

    # ── Test coverage ──
    if not tests:
        penalty += 10
        breakdown.append("no_test_file → -10")

    score = max(0, 100 - penalty)
    if score >= 90:
        grade = "A"
    elif score >= 75:
        grade = "B"
    elif score >= 60:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    return score, grade, breakdown


# ── Recommendations ───────────────────────────────────────────────────────────

def _make_recommendations(
    debt: Dict[str, Any],
    dead: Dict[str, Any],
    impact: Dict[str, Any],
    tests: List[str],
    churn: int,
) -> List[str]:
    """Generate top 3 actionable recommendations."""
    recs: List[Tuple[int, str]] = []  # (priority, text)

    if not tests:
        recs.append((0, "🔴 No test file found — add tests before any modification"))

    risk = impact.get("overall_risk", "UNKNOWN")
    trans = impact.get("transitive_count", 0)
    if risk in ("CRITICAL", "HIGH") and trans > 20:
        recs.append((1, f"⚠️  High blast radius ({trans} transitive deps) — run full test suite before committing"))

    oversize = debt.get("oversized_functions", [])
    if oversize:
        top = sorted(oversize, key=lambda x: -x["lines"])[:2]
        names = ", ".join(f"{f['name']}() [{f['lines']}L]" for f in top)
        recs.append((2, f"📏 Decompose oversized functions: {names}"))

    hcx = debt.get("high_complexity", [])
    if hcx:
        top = sorted(hcx, key=lambda x: -x["complexity"])[:1]
        recs.append((3, f"🔀 Reduce complexity in {top[0]['name']}() (cx={top[0]['complexity']})"))

    n_imports = len(dead.get("unused_imports", []))
    n_priv = len(dead.get("dead_privates", []))
    if n_imports + n_priv > 0:
        recs.append((4, f"🗑  Remove dead code: {n_imports} unused import(s), {n_priv} dead private(s)"))

    if churn > 15:
        recs.append((5, f"🔄 High churn ({churn} commits) — consider stabilizing interface or adding changelog"))

    # Sort by priority and return top 3
    recs.sort(key=lambda x: x[0])
    return [r[1] for r in recs[:3]]


# ── Text formatter ────────────────────────────────────────────────────────────

_GRADE_EMOJI = {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "F": "💀"}
_RISK_EMOJI = {"CRITICAL": "💥", "HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢", "UNKNOWN": "❓"}


def _format_text(
    rel_path: str,
    score: int,
    grade: str,
    breakdown: List[str],
    debt: Dict[str, Any],
    dead: Dict[str, Any],
    impact: Dict[str, Any],
    churn: int,
    tests: List[str],
    recommendations: List[str],
    days: int,
) -> str:
    lines: List[str] = []
    g_emoji = _GRADE_EMOJI.get(grade, "❓")
    r_emoji = _RISK_EMOJI.get(impact.get("overall_risk", "UNKNOWN"), "❓")

    lines.append(f"## Module Health: {rel_path}")
    lines.append(f"   Score: {score}/100  Grade: {g_emoji} {grade}")
    lines.append("")

    # Summary row
    loc = debt.get("loc", "?")
    risk = impact.get("overall_risk", "UNKNOWN")
    direct = impact.get("direct_count", 0)
    trans = impact.get("transitive_count", 0)
    lines.append(
        f"   LOC: {loc}  |  Risk: {r_emoji} {risk} ({direct} direct, {trans} transitive)  |  "
        f"Churn: {churn}x/{days}d  |  Tests: {len(tests)}"
    )
    lines.append("")

    # Debt section
    debt_items = (
        debt.get("oversized_functions", []) +
        debt.get("high_complexity", []) +
        debt.get("deep_nesting", []) +
        debt.get("too_many_params", [])
    )
    if debt.get("oversized_module"):
        lines.append(f"📐 Module too large: {loc} lines (limit {_MAX_MODULE_LINES})")
    if debt.get("oversized_functions"):
        for f in debt["oversized_functions"][:3]:
            lines.append(f"   📏 {f['name']}() — {f['lines']} lines (line {f['line']})")
    if debt.get("high_complexity"):
        for f in debt["high_complexity"][:3]:
            lines.append(f"   🔀 {f['name']}() — cx={f['complexity']} (line {f['line']})")
    if debt.get("deep_nesting"):
        for f in debt["deep_nesting"][:2]:
            lines.append(f"   🏔  {f['name']}() — nesting depth {f['depth']} (line {f['line']})")
    if debt.get("too_many_params"):
        for f in debt["too_many_params"][:2]:
            lines.append(f"   🔧 {f['name']}() — {f['params']} params (line {f['line']})")
    if debt_items or debt.get("oversized_module"):
        lines.append("")

    # Dead code section
    if dead.get("unused_imports"):
        lines.append(f"🗑  Unused imports ({len(dead['unused_imports'])}):")
        for imp in dead["unused_imports"][:5]:
            lines.append(f"   line {imp['line']}:  {imp['stmt']}")
        if len(dead["unused_imports"]) > 5:
            lines.append(f"   ... and {len(dead['unused_imports']) - 5} more")
        lines.append("")
    if dead.get("dead_privates"):
        lines.append(f"👻 Dead privates ({len(dead['dead_privates'])}):")
        for priv in dead["dead_privates"][:5]:
            lines.append(f"   line {priv['line']}:  {priv['kind']} {priv['name']}")
        if len(dead["dead_privates"]) > 5:
            lines.append(f"   ... and {len(dead['dead_privates']) - 5} more")
        lines.append("")

    # Test coverage
    if tests:
        lines.append(f"🧪 Tests ({len(tests)}):")
        for t in tests[:5]:
            lines.append(f"   {t}")
    else:
        lines.append("🧪 Tests: none found")
    lines.append("")

    # Penalty breakdown
    if breakdown:
        lines.append(f"📊 Penalty breakdown (total: {100 - score}):")
        for item in breakdown:
            lines.append(f"   {item}")
        lines.append("")

    # Recommendations
    if recommendations:
        lines.append("💡 Recommendations:")
        for rec in recommendations:
            lines.append(f"   {rec}")

    return "\n".join(lines)


# ── Tool entry point ──────────────────────────────────────────────────────────

def _module_health(
    ctx: ToolContext,
    target: str = "",
    days: int = 7,
    format: str = "text",
) -> str:
    """Compute aggregated health card for a single module."""
    if not target:
        return "Error: target is required (file path or dotted module name)"

    repo_dir = pathlib.Path(ctx.repo_dir if ctx and ctx.repo_dir else _REPO_DIR).resolve()

    path = _resolve_file(target, repo_dir)
    if path is None:
        return f"Error: cannot resolve '{target}' to a Python file in {repo_dir}"

    rel_path = _relative(path, repo_dir)

    # Gather all signals
    debt = _scan_tech_debt(path)
    dead = _scan_dead_code(path, repo_dir)
    impact = _scan_change_impact(path, repo_dir)
    churn = _scan_churn(rel_path, repo_dir, days=days)
    tests = _scan_tests(path, repo_dir)

    score, grade, breakdown = _compute_score(debt, dead, impact, churn, tests)
    recommendations = _make_recommendations(debt, dead, impact, tests, churn)

    if format == "json":
        return json.dumps({
            "target": rel_path,
            "score": score,
            "grade": grade,
            "tech_debt": debt,
            "dead_code": dead,
            "change_impact": impact,
            "churn": {"commits": churn, "days": days},
            "tests": tests,
            "penalty_breakdown": breakdown,
            "recommendations": recommendations,
        }, ensure_ascii=False, indent=2)

    return _format_text(
        rel_path, score, grade, breakdown,
        debt, dead, impact, churn, tests, recommendations, days,
    )


# ── Tool registration ─────────────────────────────────────────────────────────

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="module_health",
            schema={
                "name": "module_health",
                "description": (
                    "Aggregated health card for a single module. Combines five signals:\n"
                    "  1. Tech debt — oversized functions, high complexity, deep nesting\n"
                    "  2. Dead code — unused imports, dead private symbols\n"
                    "  3. Change impact — blast-radius risk tier and transitive dependents\n"
                    "  4. Git churn — commit frequency in the last N days\n"
                    "  5. Test coverage — which test files exist\n\n"
                    "Returns a health score (0–100), letter grade (A–F), penalty breakdown, "
                    "and top-3 actionable recommendations.\n\n"
                    "Use before modifying a module, or to compare modules when deciding what to refactor.\n\n"
                    "Examples:\n"
                    "  module_health(target='ouroboros/tools/registry.py')\n"
                    "  module_health(target='ouroboros/loop_runtime.py', days=14)\n"
                    "  module_health(target='ouroboros/context.py', format='json')"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": (
                                "File path relative to repo root "
                                "(e.g. 'ouroboros/tools/registry.py') "
                                "or dotted module name (e.g. 'ouroboros.tools.registry' "
                                "or 'tools.registry')."
                            ),
                        },
                        "days": {
                            "type": "integer",
                            "description": "Git churn window in days. Default: 7.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                    },
                    "required": ["target"],
                },
            },
            handler=_module_health,
        )
    ]

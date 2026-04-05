"""coupling_map — Robert Martin coupling metrics for every module.

Computes Ca/Ce/Instability for each module in the codebase.

  Ca  (Afferent Coupling)  — how many modules import THIS one.
                             High Ca = many dependents = stable, hard to change.
  Ce  (Efferent Coupling)  — how many modules THIS one imports.
                             High Ce = many dependencies = brittle, likely to break.
  I   (Instability)        — Ce / (Ca + Ce), range [0, 1].
                             I=0: maximally stable (everything depends on it).
                             I=1: maximally unstable (depends on everything, nobody needs it).

Robert Martin's main-sequence principle: stable modules should be abstract;
unstable modules should be concrete.  Modules far from the ideal I≈A balance
are "zones of pain" (I=0, A=0 — rigid) or "zones of uselessness" (I=1, A=1).

In Python, abstractness is hard to measure, so this tool focuses on
Ca/Ce/I as the actionable signal:

  - I > 0.8 + Ce > 10  →  UNSTABLE: depends on much, rarely imported.
                            Good refactor candidate: extract shared logic.
  - I < 0.1 + Ca > 15  →  RIGID: many things depend on it, change is risky.
                            Needs stability through a stable public API / facade.
  - 0.3 ≤ I ≤ 0.7      →  BALANCED: healthy middle ground.

Examples:
    coupling_map()                              # full codebase, text output
    coupling_map(top=20)                        # top 20 most unstable
    coupling_map(sort="ca")                     # sort by afferent coupling
    coupling_map(filter_zone="unstable")        # only unstable modules
    coupling_map(path="ouroboros/tools/")       # limit to subdirectory
    coupling_map(format="json")                 # machine-readable
"""
from __future__ import annotations

import ast
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

_REPO_DIR = Path(os.environ.get("OUROBOROS_REPO_DIR", "/opt/veles"))

_SKIP_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    "node_modules", ".venv", "venv", "dist", "build",
}

# Risk classification — mirrors dep_cycles/change_impact
_CRITICAL_KEYWORDS = frozenset({
    "agent", "loop", "context", "llm", "registry", "safety",
    "memory", "supervisor", "consolidator", "consciousness",
})
_HIGH_KEYWORDS = frozenset({
    "tools", "copilot_proxy", "codex_proxy", "loop_runtime", "loop_copilot",
})

# Zone thresholds
_UNSTABLE_I = 0.75   # I ≥ this → UNSTABLE zone
_RIGID_I = 0.25      # I ≤ this AND high Ca → RIGID zone
_RIGID_CA_MIN = 5    # min afferent coupling for RIGID label


# ── File / module helpers ─────────────────────────────────────────────────────

def _collect_py_files(root: Path) -> List[Path]:
    py_files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                py_files.append(Path(dirpath) / fname)
    return py_files


def _module_key(path: Path, root: Path) -> str:
    """Relative dotted module name."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else root.name


def _classify_tier(mod: str) -> str:
    parts = set(mod.replace("/", ".").split("."))
    if parts & _CRITICAL_KEYWORDS:
        return "CRITICAL"
    if parts & _HIGH_KEYWORDS:
        return "HIGH"
    return "MEDIUM"


# ── AST import parsing ────────────────────────────────────────────────────────

def _parse_imports(path: Path) -> List[Tuple[str, int]]:
    """Returns (raw_module_or_relative, lineno) for each import in file."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, OSError):
        return []
    results: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                results.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                dots = "." * (node.level or 0)
                results.append((dots + node.module, node.lineno))
            elif node.level:
                # bare relative: `from . import X`
                results.append(("." * node.level, node.lineno))
    return results


def _resolve_relative(importer: str, raw: str) -> Optional[str]:
    if not raw.startswith("."):
        return None
    dots = len(raw) - len(raw.lstrip("."))
    rest = raw.lstrip(".")
    parts = importer.split(".")
    base = parts[: max(0, len(parts) - dots)]
    return ".".join(base + [rest]) if rest else (".".join(base) or None)


# ── Graph builder ─────────────────────────────────────────────────────────────

def _build_coupling_graph(
    py_files: List[Path],
    root: Path,
) -> Tuple[
    Dict[str, Set[str]],   # forward adjacency: mod → set of imported mods (internal only)
    Dict[str, int],        # ce_ext: count of external imports per mod
    Dict[str, Path],       # mod_name → Path
]:
    """
    Build directed import graph.
    Internal edges: both src and dst are in the codebase.
    External imports: counted per module as ce_ext (not tracked as edges).
    """
    mod_map: Dict[str, Path] = {}
    for f in py_files:
        key = _module_key(f, root)
        mod_map[key] = f

    known = set(mod_map.keys())
    pkg_prefix = root.name + "."

    fwd: Dict[str, Set[str]] = defaultdict(set)
    ce_ext: Dict[str, int] = defaultdict(int)

    for mod, path in mod_map.items():
        for raw, _lineno in _parse_imports(path):
            if not raw:
                continue

            # Relative import
            if raw.startswith("."):
                resolved = _resolve_relative(mod, raw)
                if resolved and resolved in known:
                    if resolved != mod:
                        fwd[mod].add(resolved)
                continue

            # Internal absolute import (e.g. "ouroboros.tools.registry")
            if raw.startswith(pkg_prefix):
                suffix = raw[len(pkg_prefix):]
                # Match longest known module prefix
                matched = False
                for n_parts in range(suffix.count(".") + 1, 0, -1):
                    candidate = ".".join(suffix.split(".")[:n_parts])
                    if candidate in known and candidate != mod:
                        fwd[mod].add(candidate)
                        matched = True
                        break
                if not matched:
                    ce_ext[mod] += 1
                continue

            # Direct match (shorter name without package prefix)
            if raw in known and raw != mod:
                fwd[mod].add(raw)
                continue

            # Try suffix matching (e.g. "tools.registry" → "tools.registry")
            matched = False
            for n_parts in range(raw.count(".") + 1, 0, -1):
                candidate = ".".join(raw.split(".")[:n_parts])
                if candidate in known and candidate != mod:
                    fwd[mod].add(candidate)
                    matched = True
                    break

            if not matched:
                # External dependency
                ce_ext[mod] += 1

    return dict(fwd), dict(ce_ext), mod_map


# ── Metric computation ────────────────────────────────────────────────────────

def _compute_metrics(
    fwd: Dict[str, Set[str]],
    ce_ext: Dict[str, int],
    mod_map: Dict[str, Path],
    include_external_ce: bool = False,
) -> List[Dict[str, Any]]:
    """
    For each module compute Ca, Ce, I, zone.

    Ca = number of INTERNAL modules that import this module.
    Ce = number of INTERNAL modules this module imports
         (optionally +external import count if include_external_ce=True).
    I  = Ce / (Ca + Ce), or None if Ca+Ce==0.
    """
    all_mods = set(mod_map.keys())

    # Build reverse: mod → set of modules that import it (internal only)
    rev: Dict[str, Set[str]] = defaultdict(set)
    for src, targets in fwd.items():
        for tgt in targets:
            rev[tgt].add(src)

    records: List[Dict[str, Any]] = []
    for mod in sorted(all_mods):
        ca = len(rev.get(mod, set()))
        ce_int = len(fwd.get(mod, set()))
        ce_ext_count = ce_ext.get(mod, 0)
        ce = ce_int + (ce_ext_count if include_external_ce else 0)

        total = ca + ce
        instability: Optional[float] = (ce / total) if total > 0 else None

        # Zone classification
        if instability is None:
            zone = "ISOLATED"
        elif instability >= _UNSTABLE_I:
            zone = "UNSTABLE"
        elif instability <= _RIGID_I and ca >= _RIGID_CA_MIN:
            zone = "RIGID"
        elif 0.3 <= instability <= 0.7:
            zone = "BALANCED"
        else:
            zone = "STABLE"

        tier = _classify_tier(mod)

        records.append({
            "module": mod,
            "ca": ca,           # afferent coupling (who imports me)
            "ce": ce,           # efferent coupling (who I import)
            "ce_internal": ce_int,
            "ce_external": ce_ext_count,
            "instability": round(instability, 3) if instability is not None else None,
            "zone": zone,
            "tier": tier,
            "importers": sorted(rev.get(mod, set())),
            "imports": sorted(fwd.get(mod, set())),
        })

    return records


# ── Filtering / sorting ───────────────────────────────────────────────────────

_SORT_KEYS = {
    "instability": lambda r: (r["instability"] if r["instability"] is not None else 0.5, -r["ce"], r["module"]),
    "ca":          lambda r: (-r["ca"], r["module"]),
    "ce":          lambda r: (-r["ce"], r["module"]),
    "module":      lambda r: r["module"],
    "zone":        lambda r: (["RIGID", "STABLE", "BALANCED", "UNSTABLE", "ISOLATED"].index(r["zone"]), r["module"]),
}

_ZONE_ORDER = {"RIGID": 0, "STABLE": 1, "BALANCED": 2, "UNSTABLE": 3, "ISOLATED": 4}


def _filter_records(
    records: List[Dict[str, Any]],
    path: Optional[str],
    filter_zone: Optional[str],
    min_ce: int,
    min_ca: int,
) -> List[Dict[str, Any]]:
    out = records
    if path:
        clean = path.rstrip("/").lstrip("/").replace("/", ".").replace(".py", "")
        # Strip package prefix if present (e.g. "ouroboros/tools" → "tools")
        pkg_prefixes = ["ouroboros.", "supervisor."]
        for pfx in pkg_prefixes:
            if clean.startswith(pfx):
                clean = clean[len(pfx):]
                break
        out = [r for r in out if r["module"].startswith(clean) or clean in r["module"]]
    if filter_zone:
        zone_upper = filter_zone.upper()
        out = [r for r in out if r["zone"] == zone_upper]
    if min_ce > 0:
        out = [r for r in out if r["ce"] >= min_ce]
    if min_ca > 0:
        out = [r for r in out if r["ca"] >= min_ca]
    return out


# ── Text formatter ─────────────────────────────────────────────────────────────

_ZONE_ICONS = {
    "RIGID":     "🔒",
    "STABLE":    "✅",
    "BALANCED":  "⚖️ ",
    "UNSTABLE":  "⚠️ ",
    "ISOLATED":  "🔇",
}

_ZONE_DESC = {
    "RIGID":     "stable but hard-to-change (many dependents)",
    "STABLE":    "low instability",
    "BALANCED":  "healthy balance",
    "UNSTABLE":  "brittle — many deps, few dependents",
    "ISOLATED":  "no internal edges",
}


def _format_text(
    records: List[Dict[str, Any]],
    top: int,
    total_scanned: int,
    sort_key: str,
    filter_zone: Optional[str],
    path: Optional[str],
) -> str:
    lines: List[str] = []

    filters: List[str] = []
    if path:
        filters.append(f"path={path}")
    if filter_zone:
        filters.append(f"zone={filter_zone}")
    filter_note = (", " + ", ".join(filters)) if filters else ""

    zone_counts: Dict[str, int] = defaultdict(int)
    for r in records:
        zone_counts[r["zone"]] += 1

    lines.append(f"## Coupling Map — {total_scanned} modules scanned{filter_note}")
    lines.append(
        "   "
        + "  ".join(
            f"{_ZONE_ICONS.get(z, '?')} {z}: {zone_counts.get(z, 0)}"
            for z in ["RIGID", "STABLE", "BALANCED", "UNSTABLE", "ISOLATED"]
        )
    )
    lines.append("")
    lines.append(f"{'Module':<45} {'Ca':>5} {'Ce':>5} {'I':>6}  Zone")
    lines.append("─" * 72)

    shown = records[:top]
    for r in shown:
        inst = f"{r['instability']:.2f}" if r["instability"] is not None else "  —  "
        zone_icon = _ZONE_ICONS.get(r["zone"], " ")
        mod_short = r["module"]
        if len(mod_short) > 44:
            mod_short = "…" + mod_short[-43:]
        lines.append(
            f"{mod_short:<45} {r['ca']:>5} {r['ce']:>5} {inst:>6}  {zone_icon}{r['zone']}"
        )

    if len(records) > top:
        lines.append(f"  … {len(records) - top} more (increase top= to see all)")

    # Legend
    lines.append("")
    lines.append("Legend  Ca=afferent (importers)  Ce=efferent (imports)  I=instability=Ce/(Ca+Ce)")
    for zone, desc in _ZONE_DESC.items():
        lines.append(f"  {_ZONE_ICONS.get(zone, ' ')}{zone:<10} {desc}")

    # Actionable recommendations
    rigid = [r for r in records if r["zone"] == "RIGID"]
    unstable = [r for r in records if r["zone"] == "UNSTABLE" and r["ce"] >= 5]
    if rigid or unstable:
        lines.append("")
        lines.append("Recommendations:")
        for r in rigid[:3]:
            lines.append(
                f"  🔒 {r['module']} (Ca={r['ca']}) — "
                f"expose a stable facade/interface to shield dependents from churn"
            )
        for r in unstable[:5]:
            lines.append(
                f"  ⚠️  {r['module']} (I={r['instability']:.2f}, Ce={r['ce']}) — "
                f"extract shared logic into a stable module, reduce outgoing deps"
            )

    return "\n".join(lines)


# ── Handler ───────────────────────────────────────────────────────────────────

def _coupling_map(
    ctx: ToolContext,
    path: str = "",
    top: int = 30,
    sort: str = "instability",
    filter_zone: str = "",
    min_ce: int = 0,
    min_ca: int = 0,
    include_external: bool = False,
    format: str = "text",
    _repo_dir: Optional[Path] = None,
) -> str:
    """Compute Ca/Ce/Instability coupling metrics for every module."""
    repo_root = (_repo_dir or _REPO_DIR).resolve()

    # Collect files from ouroboros + supervisor (same as dep_cycles)
    py_files: List[Path] = []
    scan_root: Optional[Path] = None
    for pkg_name in ["ouroboros", "supervisor"]:
        pkg_dir = repo_root / pkg_name
        if pkg_dir.exists():
            py_files.extend(_collect_py_files(pkg_dir))
            if scan_root is None:
                scan_root = pkg_dir

    if not py_files:
        py_files = _collect_py_files(repo_root)
        scan_root = repo_root

    assert scan_root is not None

    # Use ouroboros as canonical root for module naming
    ouro = repo_root / "ouroboros"
    graph_root = ouro if ouro.exists() else scan_root

    fwd, ce_ext, mod_map = _build_coupling_graph(py_files, graph_root)
    records = _compute_metrics(fwd, ce_ext, mod_map, include_external_ce=include_external)

    # Filter
    records = _filter_records(
        records,
        path=path or None,
        filter_zone=filter_zone or None,
        min_ce=min_ce,
        min_ca=min_ca,
    )

    # Sort
    sort_fn = _SORT_KEYS.get(sort, _SORT_KEYS["instability"])
    records.sort(key=sort_fn)

    total = len(mod_map)

    if format == "json":
        return json.dumps(
            {
                "total_modules": total,
                "filtered": len(records),
                "records": records[:top],
            },
            ensure_ascii=False,
            indent=2,
        )

    return _format_text(records, top=top, total_scanned=total, sort_key=sort, filter_zone=filter_zone or None, path=path or None)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="coupling_map",
            schema={
                "name": "coupling_map",
                "description": (
                    "Robert Martin coupling metrics for every module: "
                    "Ca (afferent — how many modules import this one), "
                    "Ce (efferent — how many modules this one imports), "
                    "I=instability=Ce/(Ca+Ce). "
                    "Zones: RIGID (Ca high, I low — stable but brittle dependency), "
                    "UNSTABLE (Ce high, I high — brittle, refactor candidate), "
                    "BALANCED (healthy). "
                    "Use to find which modules to make stable and which to refactor."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Limit to modules under this path prefix (e.g. 'ouroboros/tools/'). Default: all.",
                        },
                        "top": {
                            "type": "integer",
                            "description": "Max rows to show. Default: 30.",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["instability", "ca", "ce", "module", "zone"],
                            "description": "Sort order. Default: instability (most unstable first).",
                        },
                        "filter_zone": {
                            "type": "string",
                            "enum": ["RIGID", "STABLE", "BALANCED", "UNSTABLE", "ISOLATED"],
                            "description": "Show only modules in this zone. Default: all zones.",
                        },
                        "min_ce": {
                            "type": "integer",
                            "description": "Min efferent coupling to include. Default: 0.",
                        },
                        "min_ca": {
                            "type": "integer",
                            "description": "Min afferent coupling to include. Default: 0.",
                        },
                        "include_external": {
                            "type": "boolean",
                            "description": "Count external (stdlib/third-party) imports in Ce. Default: false.",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["text", "json"],
                            "description": "Output format. Default: text.",
                        },
                    },
                    "required": [],
                },
            },
            handler=_coupling_map,
        )
    ]

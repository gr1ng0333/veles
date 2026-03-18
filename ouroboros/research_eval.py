from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ouroboros.tools.registry import ToolContext
from ouroboros.tools.search import _research_run

DEFAULT_BENCHMARK_PATH = Path(__file__).resolve().parent / "benchmarks" / "research_eval_cases.json"



def run_benchmark_eval(
    ctx: ToolContext,
    cases: Optional[List[Dict[str, Any]]] = None,
    runner: Optional[Callable[[ToolContext, str], str]] = None,
    limit: Optional[int] = None,
    dataset_path: Optional[str] = None,
) -> Dict[str, Any]:
    selected_cases = list(cases or json.loads(Path(dataset_path or DEFAULT_BENCHMARK_PATH).read_text()))
    if limit is not None:
        selected_cases = selected_cases[: max(0, int(limit))]
    execute = runner or (lambda run_ctx, query: _research_run(run_ctx, query=query))
    results = []
    for case in selected_cases:
        payload = json.loads(execute(ctx, str(case.get("query") or "")))
        final_answer = str(payload.get("final_answer") or "")
        synthesis = payload.get("synthesis") or {}
        findings = payload.get("findings") or []
        contradictions = payload.get("contradictions") or []
        uncertainty_notes = payload.get("uncertainty_notes") or []
        sources = (synthesis.get("sources") or []) if isinstance(synthesis, dict) else []
        answer_text = (final_answer + "\n" + json.dumps(synthesis, ensure_ascii=False)).lower()
        source_urls = [str(row.get("url") or "") for row in sources if isinstance(row, dict)]
        expected_terms = [str(term).lower() for term in case.get("must_include_any") or []]
        preferred_domains = [str(domain) for domain in case.get("preferred_domains") or []]
        min_citations = max(1, int(case.get("min_citations", 1)))
        has_expected_terms = any(term in answer_text for term in expected_terms)
        has_preferred_source = any(any(domain in url for domain in preferred_domains) for url in source_urls)
        citation_count = sum(1 for row in sources if isinstance(row, dict) and str(row.get("url") or "").strip())
        findings_with_evidence = sum(
            1
            for row in findings
            if str(row.get("claim") or "").strip()
            and str(row.get("evidence_snippet") or "").strip()
            and str(row.get("source_url") or "").strip()
        )
        freshness = payload.get("freshness_summary") or {}
        freshness_ok = not case.get("requires_freshness") or bool(freshness.get("known_dated_findings"))
        uncertainty_ok = not contradictions or bool(uncertainty_notes)
        subscores = {
            "correctness": 1.0 if has_expected_terms else 0.0,
            "freshness": 1.0 if freshness_ok else 0.0,
            "source_quality": 1.0 if has_preferred_source else 0.0,
            "completeness": 1.0 if findings_with_evidence >= min_citations else 0.0,
            "hallucination_resistance": 1.0 if uncertainty_ok else 0.0,
            "citation_quality": 1.0 if citation_count >= min_citations else 0.0,
        }
        results.append({
            "id": case.get("id"),
            "query": case.get("query"),
            "category": case.get("category"),
            "score": round(sum(subscores.values()) / len(subscores), 3),
            "subscores": subscores,
            "signals": {
                "has_expected_terms": has_expected_terms,
                "has_preferred_source": has_preferred_source,
                "citation_count": citation_count,
                "findings_with_evidence": findings_with_evidence,
                "freshness_ok": freshness_ok,
                "contradictions_count": len(contradictions),
                "uncertainty_notes": len(uncertainty_notes),
            },
        })
    category_summary: Dict[str, Dict[str, Any]] = {}
    for row in results:
        bucket = category_summary.setdefault(str(row.get("category") or "uncategorized"), {"cases": 0, "score_total": 0.0})
        bucket["cases"] += 1
        bucket["score_total"] += float(row.get("score") or 0.0)
    for bucket in category_summary.values():
        bucket["avg_score"] = round(bucket["score_total"] / max(1, bucket["cases"]), 3)
        bucket.pop("score_total", None)
    return {
        "benchmark_version": 1,
        "cases_total": len(results),
        "overall_score": round(sum(float(row.get("score") or 0.0) for row in results) / max(1, len(results)), 3),
        "category_summary": category_summary,
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run research benchmark eval against research_run.")
    parser.add_argument("--dataset", default="", help="Optional path to benchmark cases JSON")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit of cases to run")
    parser.add_argument("--output", default="", help="Optional path to save scorecard JSON")
    parser.add_argument("--repo-dir", default=".")
    parser.add_argument("--drive-root", default="/opt/veles-data")
    args = parser.parse_args()
    ctx = ToolContext(repo_dir=Path(args.repo_dir).resolve(), drive_root=Path(args.drive_root).resolve())
    scorecard = run_benchmark_eval(ctx, limit=(args.limit or None), dataset_path=(args.dataset or None))
    rendered = json.dumps(scorecard, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(rendered)
    raise SystemExit(0)

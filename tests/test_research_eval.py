import json
from pathlib import Path

from ouroboros.research_eval import DEFAULT_BENCHMARK_PATH, run_benchmark_eval
from ouroboros.tools.registry import ToolContext


def test_research_eval_dataset_and_scorecard(tmp_path: Path):
    cases = json.loads(DEFAULT_BENCHMARK_PATH.read_text())
    assert 30 <= len(cases) <= 50
    categories = {case["category"] for case in cases}
    assert {"fresh_news", "api_docs_lookup", "ecosystem_comparison", "pricing_release_policy", "find_primary_source", "exact_fact"} <= categories

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    eval_cases = [
        {
            "id": "case-1",
            "query": "Find official docs for Example API rate limits",
            "category": "api_docs_lookup",
            "must_include_any": ["example api", "rate limit"],
            "preferred_domains": ["docs.example.com"],
            "requires_freshness": False,
            "min_citations": 1,
        },
        {
            "id": "case-2",
            "query": "Latest ExampleAI pricing",
            "category": "pricing_release_policy",
            "must_include_any": ["exampleai", "pricing"],
            "preferred_domains": ["exampleai.com"],
            "requires_freshness": True,
            "min_citations": 1,
        },
    ]
    payloads = {
        "Find official docs for Example API rate limits": {
            "final_answer": "Example API rate limit docs confirm the current limit.",
            "findings": [{"claim": "Example API rate limit is documented.", "evidence_snippet": "The docs list the rate limit.", "source_url": "https://docs.example.com/rate-limits"}],
            "contradictions": [],
            "uncertainty_notes": [],
            "freshness_summary": {"known_dated_findings": 0},
            "synthesis": {"sources": [{"url": "https://docs.example.com/rate-limits"}]},
        },
        "Latest ExampleAI pricing": {
            "final_answer": "ExampleAI pricing was updated in 2026.",
            "findings": [{"claim": "ExampleAI pricing updated in 2026.", "evidence_snippet": "Updated 2026 pricing page.", "source_url": "https://exampleai.com/pricing"}],
            "contradictions": [{"kind": "numeric_mismatch"}],
            "uncertainty_notes": ["Источники расходятся."],
            "freshness_summary": {"known_dated_findings": 1},
            "synthesis": {"sources": [{"url": "https://exampleai.com/pricing"}]},
        },
    }
    scorecard = run_benchmark_eval(ctx, cases=eval_cases, runner=lambda _ctx, query: json.dumps(payloads[query], ensure_ascii=False))
    assert scorecard["cases_total"] == 2
    assert 0 <= scorecard["overall_score"] <= 1
    assert set(scorecard["category_summary"]) == {"api_docs_lookup", "pricing_release_policy"}
    assert len(scorecard["results"]) == 2
    assert scorecard["results"][0]["subscores"]["citation_quality"] == 1.0

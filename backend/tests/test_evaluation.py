from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.rag import RerankTrace
from app.services.vector_store import VectorStore
from scripts import evaluate_rag


def test_evaluation_reuses_online_pipeline_and_cleans_private_qdrant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    calls: list[tuple[tuple[str, ...], str, str]] = []
    original = VectorStore.query_many

    async def observed_query_many(
        self,
        semantic_queries,
        top_k,
        knowledge_base_id=None,
        *,
        lexical_query=None,
        strategy=None,
    ):
        calls.append((tuple(semantic_queries), lexical_query or "", strategy.version))
        return await original(
            self,
            semantic_queries,
            top_k,
            knowledge_base_id,
            lexical_query=lexical_query,
            strategy=strategy,
        )

    monkeypatch.setattr(VectorStore, "query_many", observed_query_many)
    report = asyncio.run(evaluate_rag.evaluate(temp_parent=tmp_path))

    assert len(calls) == report["metrics"]["cases"]
    assert all(semantic and lexical and version for semantic, lexical, version in calls)
    assert report["pipeline"] == [
        "build_retrieval_plan",
        "VectorStore.query_many[dense+sparse+weighted_rrf]",
        "rank_evidence",
        "reranker_ablation_disabled",
        "build_context",
    ]
    assert report["generation_invoked"] is False
    assert report["metrics"]["quality_gate_passed"] is True
    assert set(report["metrics"]["layers"]) == {
        "candidate",
        "fusion",
        "rerank",
        "context",
        "answerability",
    }
    assert report["metrics"]["layers"]["candidate"]["recall_at_k"] == 1.0
    assert report["metrics"]["layers"]["fusion"]["mrr"] == 1.0
    assert report["metrics"]["layers"]["answerability"]["abstention_f1"] == 1.0
    assert report["metrics"]["critical_recall"] == 1.0
    assert report["metrics"]["latency_ms"]["candidate_and_fusion"]["p95_ms"] >= 0
    assert not list(tmp_path.iterdir())


def test_with_reranker_calls_online_maybe_rerank(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "退款规则.md").write_text(
        "# 退款规则\n\n订单退款到账时间取决于支付平台，不能承诺固定小时数。",
        encoding="utf-8",
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "refund-time",
                    "question": "订单退款多久到账，能承诺固定小时数吗？",
                    "answerable": True,
                    "expected_sources_any": ["退款规则.md"],
                    "required_phrases": ["取决于支付平台", "不能承诺固定小时数"],
                    "critical_rule": True,
                    "tags": ["unit"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = 0

    async def fake_maybe_rerank(question, evidence, *, final_k=None, force=False, strategy=None):
        nonlocal calls
        calls += 1
        assert question
        assert strategy is not None
        return list(evidence[:final_k]), RerankTrace(
            attempted=True,
            applied=True,
            reason="test_native_reranker",
        )

    monkeypatch.setattr(evaluate_rag, "maybe_rerank", fake_maybe_rerank)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = asyncio.run(
        evaluate_rag.evaluate(
            cases,
            corpus,
            with_reranker=True,
            temp_parent=scratch,
        )
    )

    assert calls == 1
    assert report["metrics"]["layers"]["rerank"]["attempt_rate"] == 1.0
    assert report["metrics"]["layers"]["rerank"]["apply_rate"] == 1.0
    assert report["results"][0]["rerank_trace"]["reason"] == "test_native_reranker"
    assert report["generation_invoked"] is False
    assert not list(scratch.iterdir())


def test_case_schema_rejects_answerable_case_without_source(tmp_path: Path):
    cases = tmp_path / "invalid.json"
    cases.write_text(
        json.dumps(
            [
                {
                    "id": "invalid",
                    "question": "问题",
                    "answerable": True,
                    "expected_sources_any": [],
                    "required_phrases": ["不存在的答案"],
                    "critical_rule": False,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="必须声明期望来源"):
        evaluate_rag.load_cases(cases)


def test_run_atomically_replaces_complete_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    report = {
        "ablation_id": "unit-ablation",
        "metrics": {"quality_gate_passed": True, "cases": 30},
        "results": [{"id": f"case-{index}"} for index in range(30)],
    }

    async def fake_evaluate(*args, **kwargs):
        return report

    monkeypatch.setattr(evaluate_rag, "evaluate", fake_evaluate)
    output = tmp_path / "latest-report.json"
    output.write_text('{"stale":true}\n', encoding="utf-8")
    args = SimpleNamespace(
        cases=tmp_path / "cases.json",
        corpus=tmp_path / "corpus",
        output=output,
        rag_version="hybrid_rrf_v2",
        embedding_pool="stable",
        with_reranker=False,
    )

    assert asyncio.run(evaluate_rag.run(args)) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert not (tmp_path / ".latest-report.json.tmp").exists()

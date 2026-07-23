from __future__ import annotations

# ruff: noqa: E402 -- direct script execution establishes the backend import root first.

import argparse
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any, Sequence
import uuid


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings
from app.services.chunking import TextChunk, chunk_text
from app.services.embeddings import create_embedding_service
from app.services.query_planner import build_retrieval_plan
from app.services.rag import RerankTrace, build_context, maybe_rerank, rank_evidence
from app.services.rag_strategy import RagStrategyRegistry, get_rag_strategy
from app.services.vector_store import INDEX_SCHEMA_VERSION, SearchHit, VectorStore


DEFAULT_CASES = BACKEND_ROOT / "evaluation" / "rag_cases.json"
DEFAULT_CORPUS = BACKEND_ROOT / "sample_data"
EVALUATION_KNOWLEDGE_BASE = "offline-rag-evaluation"


@dataclass(frozen=True)
class CorpusDocument:
    document_id: str
    source_name: str
    chunks: list[TextChunk]


@dataclass(frozen=True)
class CaseResult:
    id: str
    question: str
    tags: list[str]
    answerable: bool
    predicted_answerable: bool
    abstained: bool
    candidate_count: int
    candidate_source_count: int
    candidate_recall_at_k: float
    fusion_count: int
    fusion_source_count: int
    fusion_recall_at_k: float
    fusion_precision_at_k: float
    fusion_reciprocal_rank: float
    rerank_count: int
    rerank_source_count: int
    rerank_recall_at_k: float
    rerank_precision_at_k: float
    rerank_reciprocal_rank: float
    selected_sources: list[str]
    context_source_count: int
    context_source_recall: float
    context_precision: float
    context_chars: int
    required_phrases_found: int
    child_phrases_found: int
    required_phrases_total: int
    critical_rule_found: bool | None
    rerank_trace: dict[str, bool | str]
    latency_ms: dict[str, float]
    passed: bool


def _sha256_file_set(paths: Sequence[Path], *, root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_cases(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("评测集必须是非空数组")
    required = {
        "id",
        "question",
        "answerable",
        "expected_sources_any",
        "required_phrases",
        "critical_rule",
    }
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"第 {index + 1} 条评测用例字段不完整")
        case_id = str(item["id"]).strip()
        if not case_id or case_id in seen_ids:
            raise ValueError(f"第 {index + 1} 条评测用例 ID 为空或重复")
        seen_ids.add(case_id)
        if not isinstance(item["answerable"], bool):
            raise ValueError(f"用例 {case_id} 的 answerable 必须是布尔值")
        for field in ("expected_sources_any", "required_phrases"):
            if not isinstance(item[field], list) or not all(
                isinstance(value, str) and value.strip() for value in item[field]
            ):
                raise ValueError(f"用例 {case_id} 的 {field} 必须是非空字符串数组")
        expected_all = item.get("expected_sources_all", [])
        if not isinstance(expected_all, list) or not all(
            isinstance(value, str) and value.strip() for value in expected_all
        ):
            raise ValueError(f"用例 {case_id} 的 expected_sources_all 必须是字符串数组")
        if item["answerable"] and not (item["expected_sources_any"] or expected_all):
            raise ValueError(f"可回答用例 {case_id} 必须声明期望来源")
        if not item["answerable"] and (
            item["expected_sources_any"] or expected_all or item["required_phrases"]
        ):
            raise ValueError(f"拒答用例 {case_id} 不应声明答案或来源")
        tags = item.get("tags", [])
        if not isinstance(tags, list) or not all(
            isinstance(value, str) and value.strip() for value in tags
        ):
            raise ValueError(f"用例 {case_id} 的 tags 必须是字符串数组")
    return raw


def load_corpus(path: Path) -> tuple[list[CorpusDocument], str]:
    documents = sorted(item for item in path.iterdir() if item.suffix.lower() in {".txt", ".md"})
    loaded: list[CorpusDocument] = []
    for document in documents:
        chunks = chunk_text(document.read_text(encoding="utf-8"))
        if not chunks:
            continue
        document_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"aegis-offline-evaluation:{document.name}")
        )
        loaded.append(
            CorpusDocument(
                document_id=document_id,
                source_name=document.name,
                chunks=chunks,
            )
        )
    if not loaded:
        raise ValueError("评测语料为空")
    return loaded, _sha256_file_set(documents, root=path)


def _expected_sources(case: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    expected_any = {str(item) for item in case["expected_sources_any"]}
    expected_all = {str(item) for item in case.get("expected_sources_all", [])}
    return expected_any, expected_all, expected_any | expected_all


def _source_recall(sources: Sequence[str], case: dict[str, Any]) -> float:
    """Score one optional-source group plus every mandatory source independently."""
    expected_any, expected_all, _ = _expected_sources(case)
    if not expected_any and not expected_all:
        return 1.0 if not sources else 0.0
    observed = set(sources)
    numerator = len(expected_all & observed)
    denominator = len(expected_all)
    if expected_any:
        numerator += int(bool(expected_any & observed))
        denominator += 1
    return numerator / max(1, denominator)


def _source_precision(sources: Sequence[str], case: dict[str, Any]) -> float:
    if not sources:
        return 0.0
    _, _, relevant = _expected_sources(case)
    return sum(source in relevant for source in sources) / len(sources)


def _reciprocal_rank(sources: Sequence[str], case: dict[str, Any]) -> float:
    _, _, relevant = _expected_sources(case)
    for index, source in enumerate(sources, start=1):
        if source in relevant:
            return 1.0 / index
    return 0.0


def _unique_sources(hits: Sequence[SearchHit]) -> list[str]:
    return list(dict.fromkeys(item.source_name for item in hits))


def _percentile95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999999) - 1))
    return ordered[index]


def _latency_summary(results: Sequence[CaseResult]) -> dict[str, dict[str, float]]:
    stages = list(results[0].latency_ms) if results else []
    return {
        stage: {
            "mean_ms": round(statistics.fmean(item.latency_ms[stage] for item in results), 3),
            "p95_ms": round(_percentile95([item.latency_ms[stage] for item in results]), 3),
            "max_ms": round(max(item.latency_ms[stage] for item in results), 3),
        }
        for stage in stages
    }


def _mean(results: Sequence[CaseResult], field: str) -> float:
    return round(statistics.fmean(float(getattr(item, field)) for item in results), 4) if results else 0.0


async def _index_corpus(store: VectorStore, corpus: Sequence[CorpusDocument]) -> None:
    for document in corpus:
        await store.upsert_document(
            document.document_id,
            EVALUATION_KNOWLEDGE_BASE,
            document.source_name,
            document.chunks,
        )


async def _evaluate_case(
    case: dict[str, Any],
    *,
    store: VectorStore,
    strategy,
    with_reranker: bool,
) -> CaseResult:
    question = str(case["question"])
    total_started = time.perf_counter()

    started = time.perf_counter()
    plan = build_retrieval_plan(question, strategy)
    planner_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    hits = await store.query_many(
        plan.semantic_queries,
        strategy.top_k,
        EVALUATION_KNOWLEDGE_BASE,
        lexical_query=plan.lexical_query,
        strategy=strategy,
    )
    # query_many intentionally owns dense/sparse candidate retrieval and weighted
    # RRF fusion in one production call, so their wall time is reported together.
    candidate_fusion_ms = (time.perf_counter() - started) * 1000
    candidate_hits = hits[: strategy.top_k]
    candidate_sources = _unique_sources(candidate_hits)

    reranker_active = with_reranker and strategy.rerank_policy != "disabled"
    candidate_limit = strategy.candidate_k if reranker_active else strategy.final_k
    fusion_hits = candidate_hits[:candidate_limit]
    fusion_sources = [item.source_name for item in fusion_hits]

    started = time.perf_counter()
    evidence = rank_evidence(plan.display_query, candidate_hits, limit=candidate_limit)
    deterministic_rank_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    if with_reranker:
        evidence, rerank_trace = await maybe_rerank(
            plan.display_query,
            evidence,
            final_k=strategy.final_k,
            strategy=strategy,
        )
    else:
        evidence = evidence[: strategy.final_k]
        rerank_trace = RerankTrace(reason="ablation_disabled")
    rerank_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    context = build_context(evidence, strategy)
    context_ms = (time.perf_counter() - started) * 1000

    selected_hits = [item.hit for item in evidence]
    selected_sources = [item.source_name for item in selected_hits]
    unique_selected_sources = list(dict.fromkeys(selected_sources))
    required_phrases = [str(item) for item in case["required_phrases"]]
    child_text = "\n".join(item.content for item in selected_hits)
    child_phrases_found = sum(phrase in child_text for phrase in required_phrases)
    context_phrases_found = sum(phrase in context for phrase in required_phrases)
    context_source_recall = _source_recall(unique_selected_sources, case)
    context_precision = _source_precision(selected_sources, case)
    predicted_answerable = bool(evidence and context.strip())

    critical_rule_found: bool | None = None
    if bool(case["critical_rule"]):
        _, _, relevant_sources = _expected_sources(case)
        critical_rule_found = (
            context_source_recall == 1.0
            and context_phrases_found == len(required_phrases)
            and any(
                item.hit.source_name in relevant_sources and item.hit.priority > 0
                for item in evidence
            )
        )

    passed = predicted_answerable == bool(case["answerable"])
    if bool(case["answerable"]):
        passed = (
            passed
            and context_source_recall == 1.0
            and context_phrases_found == len(required_phrases)
            and (critical_rule_found is not False)
        )

    latency = {
        "query_planner": round(planner_ms, 3),
        "candidate_and_fusion": round(candidate_fusion_ms, 3),
        "deterministic_rank": round(deterministic_rank_ms, 3),
        "rerank": round(rerank_ms, 3),
        "context_build": round(context_ms, 3),
        "total": round((time.perf_counter() - total_started) * 1000, 3),
    }
    return CaseResult(
        id=str(case["id"]),
        question=question,
        tags=[str(item) for item in case.get("tags", [])],
        answerable=bool(case["answerable"]),
        predicted_answerable=predicted_answerable,
        abstained=not predicted_answerable,
        candidate_count=len(candidate_hits),
        candidate_source_count=len(candidate_sources),
        candidate_recall_at_k=round(_source_recall(candidate_sources, case), 4),
        fusion_count=len(fusion_hits),
        fusion_source_count=len(set(fusion_sources)),
        fusion_recall_at_k=round(_source_recall(fusion_sources, case), 4),
        fusion_precision_at_k=round(_source_precision(fusion_sources, case), 4),
        fusion_reciprocal_rank=round(_reciprocal_rank(fusion_sources, case), 4),
        rerank_count=len(evidence),
        rerank_source_count=len(unique_selected_sources),
        rerank_recall_at_k=round(context_source_recall, 4),
        rerank_precision_at_k=round(context_precision, 4),
        rerank_reciprocal_rank=round(_reciprocal_rank(selected_sources, case), 4),
        selected_sources=unique_selected_sources,
        context_source_count=len(unique_selected_sources),
        context_source_recall=round(context_source_recall, 4),
        context_precision=round(context_precision, 4),
        context_chars=len(context),
        required_phrases_found=context_phrases_found,
        child_phrases_found=child_phrases_found,
        required_phrases_total=len(required_phrases),
        critical_rule_found=critical_rule_found,
        rerank_trace=asdict(rerank_trace),
        latency_ms=latency,
        passed=passed,
    )


def _aggregate_metrics(results: Sequence[CaseResult]) -> dict[str, Any]:
    answerable = [item for item in results if item.answerable]
    critical = [item for item in results if item.critical_rule_found is not None]
    total_phrases = sum(item.required_phrases_total for item in answerable)
    context_phrases = sum(item.required_phrases_found for item in answerable)
    child_phrases = sum(item.child_phrases_found for item in answerable)

    # In abstention metrics the positive class is "the system should abstain".
    abstention_tp = sum(not item.answerable and item.abstained for item in results)
    abstention_fp = sum(item.answerable and item.abstained for item in results)
    abstention_fn = sum(not item.answerable and not item.abstained for item in results)
    abstention_precision = abstention_tp / max(1, abstention_tp + abstention_fp)
    abstention_recall = abstention_tp / max(1, abstention_tp + abstention_fn)
    abstention_f1 = (
        2 * abstention_precision * abstention_recall
        / max(1e-12, abstention_precision + abstention_recall)
    )

    layers = {
        "candidate": {
            "recall_at_k": _mean(answerable, "candidate_recall_at_k"),
            "mean_candidate_count": _mean(results, "candidate_count"),
            "mean_source_count": _mean(results, "candidate_source_count"),
        },
        "fusion": {
            "recall_at_k": _mean(answerable, "fusion_recall_at_k"),
            "mrr": _mean(answerable, "fusion_reciprocal_rank"),
            "precision_at_k": _mean(answerable, "fusion_precision_at_k"),
            "mean_source_count": _mean(results, "fusion_source_count"),
        },
        "rerank": {
            "recall_at_k": _mean(answerable, "rerank_recall_at_k"),
            "mrr": _mean(answerable, "rerank_reciprocal_rank"),
            "precision_at_k": _mean(answerable, "rerank_precision_at_k"),
            "mean_source_count": _mean(results, "rerank_source_count"),
            "attempt_rate": round(sum(item.rerank_trace["attempted"] for item in results) / len(results), 4),
            "apply_rate": round(sum(item.rerank_trace["applied"] for item in results) / len(results), 4),
        },
        "context": {
            "source_recall": _mean(answerable, "context_source_recall"),
            "context_precision": _mean(answerable, "context_precision"),
            "required_phrase_recall": round(context_phrases / max(1, total_phrases), 4),
            "child_phrase_recall": round(child_phrases / max(1, total_phrases), 4),
            "parent_expansion_gain": round((context_phrases - child_phrases) / max(1, total_phrases), 4),
            "mean_source_count": _mean(results, "context_source_count"),
            "mean_context_chars": _mean(results, "context_chars"),
        },
        "answerability": {
            "accuracy": round(
                sum(item.predicted_answerable == item.answerable for item in results)
                / len(results),
                4,
            ),
            "abstention_precision": round(abstention_precision, 4),
            "abstention_recall": round(abstention_recall, 4),
            "abstention_f1": round(abstention_f1, 4),
            "true_abstentions": abstention_tp,
            "false_abstentions": abstention_fp,
            "missed_abstentions": abstention_fn,
        },
    }
    metrics: dict[str, Any] = {
        "cases": len(results),
        "answerable_cases": len(answerable),
        "unanswerable_cases": len(results) - len(answerable),
        "case_pass_rate": round(sum(item.passed for item in results) / len(results), 4),
        "critical_recall": round(
            sum(bool(item.critical_rule_found) for item in critical) / max(1, len(critical)), 4
        ),
        "layers": layers,
        "latency_ms": _latency_summary(results),
        # Backward-compatible names retained for CI/report consumers.
        "retrieval_source_recall": layers["candidate"]["recall_at_k"],
        "mean_reciprocal_rank": layers["rerank"]["mrr"],
        "required_phrase_coverage": layers["context"]["required_phrase_recall"],
        "child_phrase_coverage": layers["context"]["child_phrase_recall"],
        "parent_expansion_gain": layers["context"]["parent_expansion_gain"],
        "extractive_answer_phrase_coverage": layers["context"]["required_phrase_recall"],
        "answerability_accuracy": layers["answerability"]["accuracy"],
        "critical_rule_recall": round(
            sum(bool(item.critical_rule_found) for item in critical) / max(1, len(critical)), 4
        ),
    }
    # ``expected_sources_*`` are positive labels, not an exhaustive judgement
    # over every corpus chunk. Strict context precision therefore treats an
    # unlabelled-but-useful support chunk as a false positive and is a lower
    # bound. Keep it in the gate, but use a calibrated floor while recall,
    # critical facts, and abstention remain deliberately strict.
    thresholds = {
        "case_pass_rate": 0.90,
        "candidate_recall_at_k": 0.95,
        "rerank_recall_at_k": 0.95,
        "required_phrase_recall": 0.95,
        "context_precision_lower_bound": 0.65,
        "abstention_f1": 0.95,
        "critical_recall": 0.95,
    }
    metrics["quality_gate_thresholds"] = thresholds
    metrics["quality_gate_passed"] = (
        metrics["case_pass_rate"] >= thresholds["case_pass_rate"]
        and layers["candidate"]["recall_at_k"] >= thresholds["candidate_recall_at_k"]
        and layers["rerank"]["recall_at_k"] >= thresholds["rerank_recall_at_k"]
        and layers["context"]["required_phrase_recall"]
        >= thresholds["required_phrase_recall"]
        and layers["context"]["context_precision"]
        >= thresholds["context_precision_lower_bound"]
        and layers["answerability"]["abstention_f1"] >= thresholds["abstention_f1"]
        and metrics["critical_recall"] >= thresholds["critical_recall"]
    )
    return metrics


async def evaluate(
    cases_path: Path = DEFAULT_CASES,
    corpus_path: Path = DEFAULT_CORPUS,
    *,
    rag_version: str | None = None,
    embedding_pool: str = "stable",
    with_reranker: bool = False,
    temp_parent: Path | None = None,
) -> dict[str, Any]:
    if embedding_pool not in {"stable", "canary"}:
        raise ValueError("embedding_pool 必须为 stable 或 canary")
    settings = get_settings()
    selected_rag_version = rag_version or settings.rag_stable_version
    strategy = get_rag_strategy(selected_rag_version, settings)
    cases = load_cases(cases_path)
    corpus, corpus_sha256 = load_corpus(corpus_path)
    dimensions = (
        settings.embedding_canary_dimensions
        if embedding_pool == "canary"
        else settings.embedding_dimensions
    )
    embedding_version = (
        settings.embedding_canary_version
        if embedding_pool == "canary"
        else settings.embedding_stable_version
    )
    embedding_model = (
        settings.embedding_canary_model
        if embedding_pool == "canary"
        else settings.embedding_model
    )
    if settings.embedding_provider == "local_hash":
        embedding_model = f"local_hash_{dimensions}d"

    ablation_payload = {
        "rag_version": strategy.version,
        "rag_strategy_sha256": strategy.sha256,
        "query_mode": strategy.query_mode,
        "embedding_pool": embedding_pool,
        "embedding_provider": settings.embedding_provider,
        "embedding_version": embedding_version,
        "embedding_model": embedding_model,
        "with_reranker": with_reranker,
        "reranker_configured_enabled": settings.reranker_enabled,
        "reranker_provider": settings.reranker_provider if with_reranker else "disabled",
        "reranker_model": settings.reranker_model if with_reranker else "",
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "corpus_sha256": corpus_sha256,
        "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    }
    ablation_id = hashlib.sha256(
        json.dumps(ablation_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]

    parent = str(temp_parent.resolve()) if temp_parent else None
    results: list[CaseResult] = []
    index_started = time.perf_counter()
    embeddings = create_embedding_service(embedding_pool)
    # A private local Qdrant directory prevents evaluation from reading or
    # mutating the live Stable/Canary collections. Closing the client before the
    # context exits is required for reliable cleanup on Windows.
    with tempfile.TemporaryDirectory(prefix="aegis-rag-eval-", dir=parent) as temporary:
        store = VectorStore(
            Path(temporary),
            embeddings,
            collection_name=f"rag_eval_{uuid.uuid4().hex}",
            dimensions=dimensions,
        )
        try:
            await _index_corpus(store, corpus)
            index_latency_ms = round((time.perf_counter() - index_started) * 1000, 3)
            for case in cases:
                results.append(
                    await _evaluate_case(
                        case,
                        store=store,
                        strategy=strategy,
                        with_reranker=with_reranker,
                    )
                )
        finally:
            store.client.close()
            await embeddings.close()

    metrics = _aggregate_metrics(results)
    metrics["index_latency_ms"] = index_latency_ms
    return {
        "ablation_id": ablation_id,
        "ablation": ablation_payload,
        "pipeline": [
            "build_retrieval_plan",
            "VectorStore.query_many[dense+sparse+weighted_rrf]",
            "rank_evidence",
            "maybe_rerank" if with_reranker else "reranker_ablation_disabled",
            "build_context",
        ],
        "generation_invoked": False,
        "retrieval_config": strategy.public_dict(),
        "metrics": metrics,
        "results": [asdict(item) for item in results],
    }


async def run(args: argparse.Namespace) -> int:
    report = await evaluate(
        args.cases.resolve(),
        args.corpus.resolve(),
        rag_version=args.rag_version,
        embedding_pool=args.embedding_pool,
        with_reranker=args.with_reranker,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = args.output.with_name(f".{args.output.name}.tmp")
        temporary_output.write_text(rendered + "\n", encoding="utf-8")
        temporary_output.replace(args.output)
    return 0 if report["metrics"]["quality_gate_passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Aegis 与线上同链路、无答案生成的 RAG 分层评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--rag-version",
        choices=RagStrategyRegistry.supported_versions(),
        default=None,
        help="RAG 策略版本；默认使用 RAG_STABLE_VERSION",
    )
    parser.add_argument(
        "--embedding-pool",
        choices=("stable", "canary"),
        default="stable",
        help="使用隔离的 Stable/Canary embedding 配置构建临时索引",
    )
    parser.add_argument(
        "--with-reranker",
        action="store_true",
        help="执行线上 maybe_rerank；未指定时形成显式无 reranker 消融组",
    )
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()

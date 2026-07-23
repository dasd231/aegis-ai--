from __future__ import annotations

from types import SimpleNamespace

from app.services.claim_verifier import verify_claim_segment


def evidence(source_id: str, text: str):
    return SimpleNamespace(
        source_id=source_id,
        hit=SimpleNamespace(content=text, parent_content=text),
    )


def test_claim_verifier_rejects_wrong_quantity_with_valid_citation_id():
    result = verify_claim_segment(
        "符合条件的订单可以在签收后 30 日内申请退货。[S1]",
        [evidence("S1", "符合条件的订单可在签收后 7 日内申请无理由退货。")],
    )
    assert result.supported is False
    assert result.reason == "quantity_mismatch"


def test_claim_verifier_rejects_reversed_safety_instruction():
    result = verify_claim_segment(
        "电池鼓包后可以继续充电。[S1]",
        [evidence("S1", "电池鼓包后必须立即停止使用，不得继续充电。")],
    )
    assert result.supported is False
    assert result.reason == "polarity_contradiction"


def test_claim_verifier_returns_exact_supporting_span_for_supported_paraphrase():
    result = verify_claim_segment(
        "退款到账速度由银行和支付平台处理进度决定。[S2]",
        [evidence("S2", "到账时间取决于支付平台和银行处理，不能承诺固定小时数。")],
    )
    assert result.supported is True
    assert result.spans
    assert result.spans[0].source_id == "S2"
    assert result.spans[0].quote in "到账时间取决于支付平台和银行处理，不能承诺固定小时数。"


def test_claim_verifier_rejects_unknown_source_even_when_text_looks_related():
    result = verify_claim_segment(
        "订单可在七日内退货。[S9]",
        [evidence("S1", "订单可在七日内申请退货。")],
    )
    assert result.supported is False
    assert result.reason == "missing_or_unknown_citation"

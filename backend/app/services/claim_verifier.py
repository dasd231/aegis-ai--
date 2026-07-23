from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol, Sequence


class EvidenceHit(Protocol):
    content: str
    parent_content: str


class EvidenceItem(Protocol):
    source_id: str
    hit: EvidenceHit


@dataclass(frozen=True)
class SupportingSpan:
    source_id: str
    quote: str


@dataclass(frozen=True)
class ClaimSupportVerdict:
    supported: bool
    reason: str
    spans: tuple[SupportingSpan, ...] = ()


_CJK_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_integer(value: str) -> int | None:
    if not value or any(char not in {*_CJK_DIGITS, "十", "百"} for char in value):
        return None
    total = 0
    current = 0
    for char in value:
        if char in _CJK_DIGITS:
            current = _CJK_DIGITS[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
    return total + current


def _normalized_quantities(text: str) -> set[str]:
    compact = text.lower().replace("％", "%")
    quantities = {
        f"{match.group(1)}{match.group(2) or ''}"
        for match in re.finditer(r"(?<![a-z0-9])([0-9]+(?:\.[0-9]+)?)(\s*(?:%|日|天|年|小时|分钟|次|个|元))?", compact)
    }
    for match in re.finditer(r"([零一二两三四五六七八九十百]+)(日|天|年|小时|分钟|次|个|元)", compact):
        number = _chinese_integer(match.group(1))
        if number is not None:
            quantities.add(f"{number}{match.group(2)}")
    return {re.sub(r"\s+", "", item) for item in quantities}


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9_+-]{2,}", lowered))
    runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    bigrams = {run[index : index + 2] for run in runs for index in range(len(run) - 1)}
    return words | bigrams


def _sentences(text: str) -> list[str]:
    return [
        sentence.strip(" \t\r\n#*-_")
        for sentence in re.findall(r"[^。！？\n]+[。！？]?", text)
        if len(sentence.strip(" \t\r\n#*-_")) >= 4
    ]


def _product_entities(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "", entity.lower())
        for entity in re.findall(
            r"(?:小米|redmi|红米)\s*(?:note\s*)?(?:\d{1,3}[a-z]*|mix\s*[a-z0-9]+)(?:\s*(?:pro|max|ultra|\+))?",
            text,
            flags=re.IGNORECASE,
        )
    }


def _polarity_contradiction(claim: str, evidence: str) -> bool:
    # High-risk action predicates are checked deterministically. This catches
    # the most damaging form of citation laundering: a positive instruction
    # citing a source that explicitly prohibits the same action.
    actions = ("继续充电", "自行拆机", "在线取消", "修改地址", "提供密码", "承诺", "挤压", "刺穿")
    negative = r"(?:不得|不能|不应|不可|禁止|严禁|无法|不要)"
    positive = r"(?:可以|可|能够|应该|建议|允许|能)"
    for action in actions:
        if action not in claim or action not in evidence:
            continue
        claim_negative = bool(re.search(rf"{negative}.{{0,10}}{re.escape(action)}", claim))
        evidence_negative = bool(re.search(rf"{negative}.{{0,10}}{re.escape(action)}", evidence))
        claim_positive = bool(re.search(rf"{positive}.{{0,10}}{re.escape(action)}", claim))
        if claim_positive and evidence_negative and not claim_negative:
            return True
        if claim_negative and not evidence_negative and re.search(
            rf"{positive}.{{0,10}}{re.escape(action)}", evidence
        ):
            return True
    return False


def verify_claim_segment(
    segment: str,
    evidence: Sequence[EvidenceItem],
) -> ClaimSupportVerdict:
    cited_ids = tuple(dict.fromkeys(re.findall(r"\[(S\d+)\]", segment)))
    allowed = {item.source_id: item for item in evidence}
    if not cited_ids or any(source_id not in allowed for source_id in cited_ids):
        return ClaimSupportVerdict(False, "missing_or_unknown_citation")
    claim = re.sub(r"\s*\[S\d+\]", "", segment).strip(" \t\r\n#*-_>")
    if not claim:
        return ClaimSupportVerdict(False, "empty_claim")
    sources = [allowed[source_id] for source_id in cited_ids]
    source_text = "\n".join(item.hit.parent_content or item.hit.content for item in sources)

    claim_quantities = _normalized_quantities(claim)
    if not claim_quantities.issubset(_normalized_quantities(source_text)):
        return ClaimSupportVerdict(False, "quantity_mismatch")
    claim_entities = _product_entities(claim)
    source_entities = _product_entities(source_text)
    if claim_entities and source_entities and not claim_entities.issubset(source_entities):
        return ClaimSupportVerdict(False, "entity_mismatch")
    if _polarity_contradiction(claim, source_text):
        return ClaimSupportVerdict(False, "polarity_contradiction")

    claim_terms = _terms(claim)
    ranked_spans: list[tuple[float, SupportingSpan]] = []
    for item in sources:
        for sentence in _sentences(item.hit.parent_content or item.hit.content):
            sentence_terms = _terms(sentence)
            overlap = len(claim_terms & sentence_terms) / max(1, len(claim_terms))
            if overlap > 0:
                ranked_spans.append((overlap, SupportingSpan(item.source_id, sentence[:500])))
    ranked_spans.sort(key=lambda item: item[0], reverse=True)
    if not ranked_spans or ranked_spans[0][0] < 0.18:
        return ClaimSupportVerdict(False, "insufficient_lexical_support")
    spans: list[SupportingSpan] = []
    seen: set[tuple[str, str]] = set()
    for _, span in ranked_spans:
        key = (span.source_id, span.quote)
        if key not in seen:
            spans.append(span)
            seen.add(key)
        if len(spans) >= min(2, len(cited_ids)):
            break
    return ClaimSupportVerdict(True, "supported", tuple(spans))

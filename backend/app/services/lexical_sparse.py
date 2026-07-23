from __future__ import annotations

from collections import Counter
import hashlib
import math
import re

from qdrant_client import models


STOP_TERMS = frozenset({"小米", "客服", "问题", "怎么", "如何", "可以", "什么", "一下"})


def lexical_tokens(text: str) -> list[str]:
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]{2,}", lowered)
    runs = re.findall(r"[\u3400-\u9fff]+", lowered)
    bigrams = [run[index : index + 2] for run in runs for index in range(len(run) - 1)]
    return [token for token in [*words, *bigrams] if token not in STOP_TERMS]


def sparse_lexical_vector(text: str) -> models.SparseVector:
    """Build a deterministic BM25-style sparse vector without a sidecar model.

    Term-frequency saturation and length normalization provide the useful BM25
    behavior for exact product names, order IDs and policy phrases. Dense
    embeddings remain responsible for paraphrases; Qdrant searches both spaces
    and the application performs a bounded union before the grounding gate.
    """
    tokens = lexical_tokens(text)
    if not tokens:
        return models.SparseVector(indices=[], values=[])
    counts = Counter(tokens)
    length_ratio = max(0.25, len(tokens) / 160)
    k1, b = 1.2, 0.75
    weighted: list[tuple[int, float]] = []
    for token, frequency in counts.items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % 2_147_483_647
        saturated = frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length_ratio))
        # Longer phrases are more discriminative than generic two-character terms.
        weighted.append((index, saturated * (1 + min(len(token), 8) * 0.08)))
    merged: dict[int, float] = {}
    for index, value in weighted:
        merged[index] = merged.get(index, 0.0) + value
    norm = math.sqrt(sum(value * value for value in merged.values())) or 1.0
    ordered = sorted(merged.items())
    return models.SparseVector(
        indices=[index for index, _ in ordered],
        values=[value / norm for _, value in ordered],
    )

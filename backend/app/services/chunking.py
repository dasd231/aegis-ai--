from __future__ import annotations

import re
from dataclasses import dataclass


RULE_MARKERS = ("必须", "不得", "严禁", "仅限", "上限", "至少", "不超过", "需要", "should", "must", "禁止")


@dataclass(frozen=True)
class TextChunk:
    index: int
    content: str
    priority: int
    summary: str
    parent_id: str = ""
    parent_content: str = ""
    section_path: str = ""
    child_start: int = 0
    child_end: int = 0


def _priority(text: str) -> int:
    marker_count = sum(1 for marker in RULE_MARKERS if marker.lower() in text.lower())
    return min(3, marker_count)


def _summary(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _split_units(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= 900:
            units.append(paragraph)
            continue
        sentences = [part.strip() for part in re.split(r"(?<=[。！？!?；;\.])\s*", paragraph) if part.strip()]
        units.extend(sentences or [paragraph])
    return units


def _is_section_boundary(paragraph: str) -> bool:
    first_line = paragraph.splitlines()[0].strip()
    return bool(
        re.match(r"^#{1,6}\s+\S", first_line)
        or re.match(r"^---\s*第\s*\d+\s*页(?:文本|视觉转写)?\s*---$", first_line)
        or re.match(r"^[一二三四五六七八九十百]+[、.．]\s*\S", first_line)
        or re.match(r"^第[一二三四五六七八九十百0-9]+[章节篇部分]\s*\S*", first_line)
        or re.match(r"^\d+(?:\.\d+){0,3}[、.．\s]+\S", first_line)
        or re.match(r"^(?:Q\d*|问题\d*)[:：]\s*\S", first_line, flags=re.IGNORECASE)
    )


def _section_name(paragraph: str, fallback: str) -> str:
    first_line = paragraph.splitlines()[0].strip()
    heading = re.match(r"^#{1,6}\s+(.+)$", first_line)
    if heading:
        return heading.group(1).strip()[:160]
    page = re.match(r"^---\s*(第\s*\d+\s*页(?:文本|视觉转写)?)\s*---$", first_line)
    if page:
        return page.group(1).strip()
    chinese = re.match(
        r"^((?:[一二三四五六七八九十百]+[、.．]|第[一二三四五六七八九十百0-9]+[章节篇部分]|"
        r"\d+(?:\.\d+){0,3}[、.．]?|(?:Q\d*|问题\d*)[:：])\s*.+)$",
        first_line,
        flags=re.IGNORECASE,
    )
    return chinese.group(1).strip()[:160] if chinese else fallback


def _parent_sections(text: str, parent_max_chars: int) -> list[tuple[str, str]]:
    """Build bounded page/heading-aware parents without relying on a specific parser."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]
    parents: list[tuple[str, str]] = []
    current: list[str] = []
    current_section = "文档正文"

    def flush() -> None:
        nonlocal current
        if current:
            parents.append((current_section, "\n\n".join(current).strip()))
            current = []

    for paragraph in paragraphs:
        if _is_section_boundary(paragraph):
            flush()
            current_section = _section_name(paragraph, current_section)

        # A very long paragraph is split into semantic units before parent packing,
        # otherwise a single malformed paragraph could defeat the context budget.
        units = _split_units(paragraph) if len(paragraph) > parent_max_chars else [paragraph]
        for unit in units:
            while len(unit) > parent_max_chars:
                flush()
                parents.append((current_section, unit[:parent_max_chars].strip()))
                unit = unit[parent_max_chars:].strip()
            if not unit:
                continue
            candidate = "\n\n".join([*current, unit])
            if current and len(candidate) > parent_max_chars:
                flush()
            current.append(unit)
    flush()
    return parents


def _split_parent(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    raw_chunks: list[str] = []
    current = ""
    for unit in _split_units(text):
        while len(unit) > max_chars:
            if current:
                raw_chunks.append(current.strip())
                current = ""
            raw_chunks.append(unit[:max_chars].strip())
            unit = unit[max_chars - overlap_chars :].strip()
        candidate = f"{current}\n\n{unit}".strip() if current else unit
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            raw_chunks.append(current.strip())
            tail = current[-overlap_chars:].strip() if overlap_chars else ""
            current = f"{tail}\n\n{unit}".strip() if tail else unit
        else:
            current = unit
    if current:
        raw_chunks.append(current.strip())
    return raw_chunks


def chunk_text(
    text: str,
    max_chars: int = 900,
    overlap_chars: int = 120,
    parent_max_chars: int = 2800,
) -> list[TextChunk]:
    """Index small children while retaining bounded page/section parents for answering."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    if (
        max_chars < 100
        or overlap_chars < 0
        or overlap_chars >= max_chars
        or parent_max_chars < max_chars
        or parent_max_chars > 12000
    ):
        raise ValueError("invalid chunk size configuration")

    # Exact duplicates are harmful to ranking and waste context budget.
    seen: set[str] = set()
    chunks: list[TextChunk] = []
    for parent_index, (section_path, parent_content) in enumerate(
        _parent_sections(normalized, parent_max_chars)
    ):
        for content in _split_parent(parent_content, max_chars, overlap_chars):
            fingerprint = re.sub(r"\s+", "", content)
            if not fingerprint or fingerprint in seen:
                continue
            seen.add(fingerprint)
            child_start = parent_content.find(content)
            if child_start < 0:
                child_start = 0
            chunks.append(
                TextChunk(
                    index=len(chunks),
                    content=content,
                    priority=_priority(content),
                    summary=_summary(content),
                    parent_id=f"P{parent_index}",
                    parent_content=parent_content,
                    section_path=section_path,
                    child_start=child_start,
                    child_end=min(len(parent_content), child_start + len(content)),
                )
            )
    return chunks

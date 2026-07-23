from __future__ import annotations

import re
from pathlib import Path

import fitz


MAX_EXTRACTED_CHARS = 2_000_000


class DocumentParseError(ValueError):
    pass


def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t ]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    if len(text) > MAX_EXTRACTED_CHARS:
        raise DocumentParseError("文档解析后的文本超过 200 万字符限制")
    if not text:
        raise DocumentParseError("文档中没有可提取的文本")
    return text


def parse_document(path: Path, extension: str) -> str:
    extension = extension.lower()
    try:
        if extension in {".txt", ".md"}:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
            return clean_text(text)
        if extension == ".pdf":
            with fitz.open(path) as document:
                if document.needs_pass:
                    raise DocumentParseError("不支持加密 PDF")
                if document.page_count > 1000:
                    raise DocumentParseError("PDF 页数超过 1000 页限制")
                return clean_text("\n\n".join(page.get_text("text") for page in document))
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError("文档损坏或格式无法解析") from exc
    raise DocumentParseError(f"不支持的文件格式：{extension}")

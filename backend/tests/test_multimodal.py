from __future__ import annotations

import asyncio

import fitz
import pytest
from PIL import Image

from app.services.document_parser import DocumentParseError
from app.services.multimodal import MultimodalService, validate_image


def test_validated_image_flows_through_visual_transcription(tmp_path, monkeypatch):
    path = tmp_path / "policy.png"
    Image.new("RGB", (64, 32), color="white").save(path, format="PNG")
    validate_image(path)
    service = MultimodalService()
    captured: dict[str, object] = {}

    async def fake_describe(content: bytes, mime_type: str, label: str) -> str:
        captured.update(size=len(content), mime_type=mime_type, label=label)
        return "图片可见文字：退款期限为七日。"

    monkeypatch.setattr(service, "describe_image", fake_describe)
    parsed = asyncio.run(service.parse(path, ".png"))
    assert "七日" in parsed
    assert captured["mime_type"] == "image/png"
    assert captured["label"] == "policy.png"
    assert int(captured["size"]) > 0


def test_corrupt_image_is_rejected_before_model_call(tmp_path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"not-a-real-image")
    with pytest.raises(DocumentParseError, match="损坏|无法识别"):
        validate_image(path)


def test_text_pdf_does_not_require_visual_provider(tmp_path):
    path = tmp_path / "text.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Refund policy: request within seven days after delivery.")
    document.save(path)
    document.close()

    parsed = asyncio.run(MultimodalService().parse(path, ".pdf"))
    assert "seven days" in parsed
    assert "第 1 页文本" in parsed

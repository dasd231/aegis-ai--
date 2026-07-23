from __future__ import annotations

# ruff: noqa: E402 -- direct script execution establishes the backend import root first.

import argparse
import asyncio
from pathlib import Path
import sys

from sqlalchemy import select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.database import SessionLocal, engine
from app.models import DocumentStatus, KnowledgeDocument
from app.services.knowledge import reindex_ready_document
from app.services.multimodal import close_multimodal_service
from app.services.vector_store import INDEX_SCHEMA_VERSION, close_vector_store


async def run(document_id: str | None) -> int:
    with SessionLocal() as db:
        query = select(KnowledgeDocument.id, KnowledgeDocument.original_name).where(
            KnowledgeDocument.status == DocumentStatus.READY
        )
        if document_id:
            query = query.where(KnowledgeDocument.id == document_id)
        documents = list(db.execute(query.order_by(KnowledgeDocument.created_at)).all())
    if not documents:
        print("没有找到可重建索引的就绪文档。")
        return 1
    failures = 0
    try:
        for current_id, name in documents:
            succeeded = await reindex_ready_document(current_id)
            print(f"{'成功' if succeeded else '失败'}：{name}（{current_id}）")
            failures += int(not succeeded)
    finally:
        await close_multimodal_service()
        await close_vector_store()
        engine.dispose()
    print(f"索引 Schema v{INDEX_SCHEMA_VERSION}：总计 {len(documents)}，失败 {failures}。")
    return int(bool(failures))


def main() -> None:
    parser = argparse.ArgumentParser(description="无停机语义地重建 Aegis 就绪文档向量")
    parser.add_argument("--document-id", help="仅重建一个文档；省略时重建所有就绪文档")
    raise SystemExit(asyncio.run(run(parser.parse_args().document_id)))


if __name__ == "__main__":
    main()

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import BinaryIO

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.core.config import get_settings
from app.database import get_db
from app.models import DocumentStatus, KnowledgeBase, KnowledgeDocument, User
from app.schemas import DocumentOut, DocumentPage, KnowledgeBaseCreate, KnowledgeBaseOut
from app.services.knowledge import delete_document as delete_document_service
from app.services.knowledge import process_document
from app.services.job_queue import enqueue_ingestion
from app.services.multimodal import IMAGE_EXTENSIONS, validate_image
from app.services.document_parser import DocumentParseError


router = APIRouter(prefix="/knowledge", tags=["知识库"])
logger = logging.getLogger(__name__)


def _write_upload(source: BinaryIO, destination: Path, maximum: int) -> tuple[int, str, bytes]:
    digest = hashlib.sha256()
    size = 0
    head = b""
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ValueError("文件大小超过限制")
            if len(head) < 8:
                head += chunk[: 8 - len(head)]
            digest.update(chunk)
            output.write(chunk)
    return size, digest.hexdigest(), head


@router.post("/bases", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
def create_base(
    payload: KnowledgeBaseCreate,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = payload.name.strip()
    if db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == name)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="知识库名称已存在")
    knowledge_base = KnowledgeBase(name=name, description=payload.description.strip())
    db.add(knowledge_base)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="知识库名称已存在") from None
    db.refresh(knowledge_base)
    return knowledge_base


@router.get("/bases", response_model=list[KnowledgeBaseOut])
def list_bases(_: User = Depends(require_admin), db: Session = Depends(get_db)):
    return list(db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())).all())


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    knowledge_base_id: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    original_name = Path((file.filename or "").replace("\\", "/")).name
    original_name = re.sub(r"[\x00-\x1f\x7f]", "", original_name)[:255]
    extension = Path(original_name).suffix.lower()
    if not original_name or extension not in settings.extension_set:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="仅支持 .txt、.md、.pdf、.png、.jpg、.jpeg 和 .webp 文件",
        )
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    if knowledge_base is None or not knowledge_base.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    destination = (settings.upload_dir / f"{uuid.uuid4()}{extension}").resolve()
    if destination.parent != settings.upload_dir.resolve():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法文件名")
    try:
        size, sha256, head = await asyncio.to_thread(
            _write_upload, file.file, destination, settings.max_upload_mb * 1024 * 1024
        )
    except ValueError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from None
    except Exception:
        destination.unlink(missing_ok=True)
        logger.exception("failed to store upload")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="文件保存失败") from None
    finally:
        await file.close()

    if size == 0 or (extension == ".pdf" and not head.startswith(b"%PDF")):
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="文件为空或内容与扩展名不匹配")
    if extension in IMAGE_EXTENSIONS:
        try:
            await asyncio.to_thread(validate_image, destination)
        except DocumentParseError as exc:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    duplicate = db.scalar(
        select(KnowledgeDocument.id).where(
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.sha256 == sha256,
            KnowledgeDocument.status.in_([DocumentStatus.PROCESSING, DocumentStatus.READY]),
        )
    )
    if duplicate:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该知识库中已存在相同内容的文档")

    document = KnowledgeDocument(
        knowledge_base_id=knowledge_base_id,
        original_name=original_name,
        storage_path=str(destination),
        content_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        sha256=sha256,
        status=DocumentStatus.PROCESSING,
        uploaded_by=user.id,
    )
    db.add(document)
    try:
        db.flush()
        if settings.ingestion_mode == "queue":
            enqueue_ingestion(db, document.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该知识库中已存在相同内容的文档") from None
    except Exception:
        db.rollback()
        destination.unlink(missing_ok=True)
        logger.exception("failed to persist uploaded document")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="文档元数据保存失败") from None
    db.refresh(document)
    if settings.ingestion_mode == "inline":
        background_tasks.add_task(process_document, document.id)
    return document


@router.get("/documents", response_model=DocumentPage)
def list_documents(
    knowledge_base_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    conditions = []
    if knowledge_base_id:
        conditions.append(KnowledgeDocument.knowledge_base_id == knowledge_base_id)
    query = select(KnowledgeDocument).where(*conditions)
    total = db.scalar(select(func.count()).select_from(KnowledgeDocument).where(*conditions)) or 0
    items = db.scalars(
        query.order_by(KnowledgeDocument.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return DocumentPage(items=list(items), total=total, page=page, page_size=page_size)


@router.post("/documents/{document_id}/retry", response_model=DocumentOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    if document.status != DocumentStatus.FAILED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="只有入库失败的文档可以重试")
    path = Path(document.storage_path)
    try:
        path.resolve().relative_to(settings.upload_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="文档存储路径无效，不能重试") from None
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="原文件已不存在，不能重试")
    changed = db.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id, KnowledgeDocument.status == DocumentStatus.FAILED)
        .values(status=DocumentStatus.PROCESSING, chunk_count=0, error_message=None)
    )
    if changed.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="文档状态已变化，请刷新后重试")
    if settings.ingestion_mode == "queue":
        enqueue_ingestion(db, document_id, reset_attempts=True)
    db.commit()
    document = db.get(KnowledgeDocument, document_id)
    if settings.ingestion_mode == "inline":
        background_tasks.add_task(process_document, document_id)
    return document


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_document(
    document_id: str,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.get(KnowledgeDocument, document_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    try:
        path = await delete_document_service(document_id)
    except Exception:
        logger.exception("document deletion failed")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="删除失败，已保留元数据供重试") from None
    if path:
        try:
            path.resolve().relative_to(get_settings().upload_dir.resolve())
            path.unlink(missing_ok=True)
        except (ValueError, OSError):
            logger.warning("orphan upload could not be removed: %s", path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

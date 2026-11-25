from io import BytesIO

from fastapi import HTTPException, Request
from pypdf import PdfReader
from sqlalchemy import delete

from ..models import ResumeDocument, utc_now

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


async def bounded_document_body(request: Request) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "简历文件不能超过 10MB")
        content.extend(chunk)
    return bytes(content)


def validate_resume_pdf(content: bytes, filename: str) -> None:
    if not filename.lower().endswith(".pdf") or not content.startswith(b"%PDF-"):
        raise HTTPException(422, "请上传有效 PDF 简历原件")
    try:
        pdf = PdfReader(BytesIO(content))
        if pdf.is_encrypted or not pdf.pages:
            raise ValueError("encrypted or empty")
    except Exception:
        raise HTTPException(422, "PDF 无法读取，请先解除密码保护或重新导出") from None


def prune_expired_documents(db):
    # Called on upload/read and by the existing candidate-retention cleanup.
    db.execute(delete(ResumeDocument).where(ResumeDocument.expires_at <= utc_now()))

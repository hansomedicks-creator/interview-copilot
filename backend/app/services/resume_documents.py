from io import BytesIO
from pathlib import PurePath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipCompressionError

from fastapi import HTTPException, Request
from pypdf import PdfReader
from sqlalchemy import delete

from ..models import ResumeDocument, utc_now

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_WORD_BYTES = 20 * 1024 * 1024
MAX_WORD_XML_BYTES = 8 * 1024 * 1024
MAX_WORD_TEXT_CHARS = 400_000
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def document_byte_limit(filename: str) -> int:
    return MAX_WORD_BYTES if PurePath(filename).suffix.lower() == ".docx" else MAX_DOCUMENT_BYTES


async def bounded_document_body(request: Request, filename: str = "") -> bytes:
    limit = document_byte_limit(filename)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > limit:
            raise HTTPException(413, f"文件不能超过 {limit // (1024 * 1024)} MB")
        content.extend(chunk)
    return bytes(content)


def read_word_text(content: bytes) -> str:
    """Extract DOCX body text without executing macros, resolving links or loading embedded assets."""
    if len(content) > MAX_WORD_BYTES:
        raise HTTPException(413, "Word 文件不能超过 20 MB")
    try:
        with ZipFile(BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_WORD_XML_BYTES:
                raise HTTPException(413, "Word 正文结构过大，请删除不相关内容或另存为 PDF")
            with archive.open(info) as stream:
                xml = stream.read(MAX_WORD_XML_BYTES + 1)
        if len(xml) > MAX_WORD_XML_BYTES:
            raise HTTPException(413, "Word 正文结构过大，请另存为 PDF")
        # OOXML needs no DTDs. Reject entity declarations even in UTF-16/32.
        if b"<!DOCTYPE" in xml.replace(b"\0", b"").upper():
            raise ValueError("DTD is not allowed")
        body = ElementTree.fromstring(xml).find(WORD_NAMESPACE + "body")
        if body is None:
            raise ValueError("missing body")
    except (BadZipFile, ZipCompressionError, KeyError, ElementTree.ParseError, RuntimeError, ValueError, NotImplementedError):
        raise HTTPException(422, "Word 无法读取，请另存为未加密的 .docx 后重试") from None

    parts: list[str] = []
    count = 0
    for node in body.iter():
        tag = node.tag.removeprefix(WORD_NAMESPACE)
        part = (
            node.text or "" if tag == "t" else
            "\f" if tag == "br" and node.get(WORD_NAMESPACE + "type") == "page" else
            "\n" if tag in {"p", "br", "cr"} else
            "\t" if tag == "tab" else ""
        )
        if part:
            count += len(part)
            if count > MAX_WORD_TEXT_CHARS:
                raise HTTPException(413, "Word 正文超过 40 万字符，请保留与岗位相关的部分后再上传")
            parts.append(part)
    text = "".join(parts).strip()
    if not text:
        raise HTTPException(422, "Word 未找到可读取的正文；如果只有图片，请上传可复制文字的版本")
    return text


def word_preview_pages(text: str, page_chars: int = 1800) -> list[str]:
    """Readable reflowed pages; prefer paragraph boundaries and honor explicit page breaks."""
    pages = []
    for section in text.split("\f"):
        remaining = section.strip()
        while remaining:
            end = min(len(remaining), page_chars)
            if end < len(remaining):
                boundary = remaining.rfind("\n", page_chars // 2, end)
                if boundary > 0:
                    end = boundary
            pages.append(remaining[:end].strip())
            remaining = remaining[end:].lstrip()
    return pages


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

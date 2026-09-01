from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from ..config import Settings
from .data_governance import record_audit_event
from .knowledge_vault import KnowledgePublishError, inspect_vault


SYSTEM_DOCS_FOLDER = Path("70-系统使用与运维")
REQUIRED_MARKERS = (
    "type: system_documentation",
    "contains_pii: false",
    "rag_scope: excluded",
    "source: repository_managed",
)


def system_docs_source_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "docs" / "obsidian-system-docs"


def build_system_docs_status(settings: Settings) -> dict[str, Any]:
    source_dir = system_docs_source_dir()
    vault = inspect_vault(settings.resolved_knowledge_vault_dir, settings.knowledge_vault_name)
    items: list[dict[str, Any]] = []
    if source_dir.is_dir():
        for source in sorted(source_dir.rglob("*.md")):
            relative_source = source.relative_to(source_dir)
            target_relative = SYSTEM_DOCS_FOLDER / relative_source
            source_content = source.read_text(encoding="utf-8")
            source_hash = _content_hash(source_content)
            target = Path(vault.path) / target_relative if vault.path else None
            target_hash = None
            if target and target.is_file():
                target_hash = _content_hash(target.read_text(encoding="utf-8"))
            item_status = "synced" if target_hash == source_hash else "outdated" if target_hash else "missing"
            items.append(
                {
                    "source_path": relative_source.as_posix(),
                    "target_path": target_relative.as_posix(),
                    "title": source.stem,
                    "status": item_status,
                    "source_hash": source_hash,
                    "target_hash": target_hash,
                }
            )
    synced = sum(item["status"] == "synced" for item in items)
    return {
        "source": {
            "path": str(source_dir),
            "exists": source_dir.is_dir(),
            "document_count": len(items),
        },
        "target": {
            "vault_path": vault.path,
            "relative_folder": SYSTEM_DOCS_FOLDER.as_posix(),
            "writable": vault.writable,
            "message": vault.message,
            "open_uri": (
                f"obsidian://open?vault={quote(settings.knowledge_vault_name, safe='')}"
                f"&file={quote((SYSTEM_DOCS_FOLDER / '首页').as_posix(), safe='')}"
                if vault.writable
                else None
            ),
        },
        "summary": {
            "total": len(items),
            "synced": synced,
            "pending": len(items) - synced,
            "in_sync": bool(items) and synced == len(items),
        },
        "policy": {
            "approval_required": True,
            "rag_scope": "excluded",
            "contains_candidate_data": False,
            "automatic_sync": False,
        },
        "items": items,
    }


def sync_system_docs(
    db: Session,
    settings: Settings,
    user: dict[str, Any],
) -> dict[str, Any]:
    source_dir = system_docs_source_dir()
    if not source_dir.is_dir():
        raise KnowledgePublishError("系统文档源目录不存在")
    vault = inspect_vault(settings.resolved_knowledge_vault_dir, settings.knowledge_vault_name)
    if not vault.writable or not vault.path:
        raise KnowledgePublishError(vault.message)
    root = Path(vault.path).resolve()
    written: list[str] = []
    unchanged: list[str] = []
    for source in sorted(source_dir.rglob("*.md")):
        content = source.read_text(encoding="utf-8")
        _validate_system_document(source, content)
        relative_source = source.relative_to(source_dir)
        relative_target = SYSTEM_DOCS_FOLDER / relative_source
        target = (root / relative_target).resolve()
        if not target.is_relative_to(root):
            raise KnowledgePublishError("系统文档路径超出配置的 Vault")
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            unchanged.append(relative_target.as_posix())
            continue
        _atomic_write(target, content)
        written.append(relative_target.as_posix())
    record_audit_event(
        db,
        user,
        action="system_docs.synced",
        resource_type="obsidian_system_docs",
        resource_id=SYSTEM_DOCS_FOLDER.as_posix(),
        details={
            "written_count": len(written),
            "unchanged_count": len(unchanged),
            "written_paths": written,
            "rag_scope": "excluded",
        },
    )
    db.flush()
    return {
        "status": "completed",
        "written": written,
        "unchanged": unchanged,
        "system_docs": build_system_docs_status(settings),
    }


def _validate_system_document(path: Path, content: str) -> None:
    missing = [marker for marker in REQUIRED_MARKERS if marker not in content]
    if missing:
        raise KnowledgePublishError(
            f"{path.name} 缺少系统文档隔离标记：{', '.join(missing)}"
        )
    if "FEISHU_APP_SECRET=" in content or "INTERVIEW_LLM_API_KEY=" in content:
        raise KnowledgePublishError(f"{path.name} 疑似包含密钥值，已阻止同步")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(target)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()

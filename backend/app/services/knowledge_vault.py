from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import quote


PROPOSAL_LABELS = {
    "competency": "能力模型",
    "question": "面试题目",
    "follow_up_rule": "追问规则",
    "profile": "人才画像",
}

PROPOSAL_FOLDERS = {
    "competency": "能力模型",
    "question": "题目",
    "follow_up_rule": "追问规则",
    "profile": "人才画像",
}

PUBLISHABLE_FIELDS = {
    "question",
    "title",
    "competency_id",
    "competency_name",
    "name",
    "definition",
    "description",
    "round_type",
    "round_types",
    "required",
    "trigger",
    "suggestion",
    "follow_up",
    "follow_ups",
    "scoring_anchors",
    "positive_signals",
    "risk_signals",
    "must_have",
    "trainable",
    "success_outcomes",
    "summary",
    "weight",
    "evidence_requirements",
    "target",
    "reason",
}

SENSITIVE_KEYS = {
    "candidate",
    "candidate_id",
    "candidate_name",
    "display_name",
    "resume",
    "resume_text",
    "phone",
    "mobile",
    "email",
    "transcript",
    "recording",
    "audio",
    "speaker",
    "quote",
}

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


class KnowledgePublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class VaultStatus:
    configured: bool
    exists: bool
    writable: bool
    path: str | None
    vault_name: str
    message: str


@dataclass(frozen=True)
class PublishedKnowledge:
    release_version: str
    relative_path: str
    content_hash: str
    obsidian_uri: str
    release_note_path: str


def inspect_vault(vault_dir: Path | None, vault_name: str) -> VaultStatus:
    if vault_dir is None:
        return VaultStatus(False, False, False, None, vault_name, "知识库路径尚未配置")
    resolved = vault_dir.expanduser().resolve()
    exists = resolved.is_dir()
    writable = exists and os.access(resolved, os.W_OK)
    if not exists:
        message = "知识库目录不存在"
    elif not writable:
        message = "知识库目录不可写"
    elif not ((resolved / ".obsidian").is_dir() or (resolved / "首页.md").is_file()):
        message = "目录存在，但未识别为 Interview Copilot 知识库"
        writable = False
    else:
        message = "Obsidian 知识库已连接"
    return VaultStatus(True, exists, writable, str(resolved), vault_name, message)


def publish_proposal(
    *,
    vault_dir: Path,
    vault_name: str,
    proposal_id: str,
    proposal_type: str,
    payload: dict[str, Any],
    rationale: str,
    source_round_id: str,
    round_type: str,
    job_code: str | None,
    job_title: str,
    reviewed_by: str,
    reviewed_at: datetime,
    release_version: str,
) -> PublishedKnowledge:
    status = inspect_vault(vault_dir, vault_name)
    if not status.writable or not status.path:
        raise KnowledgePublishError(status.message)

    safe_payload = _publishable_payload(payload)
    privacy_text = json.dumps(safe_payload, ensure_ascii=False) + "\n" + rationale
    if EMAIL_RE.search(privacy_text) or MOBILE_RE.search(privacy_text):
        raise KnowledgePublishError("提案中疑似包含邮箱或手机号，已阻止写入企业知识库")

    root = Path(status.path)
    safe_job_code = _slug(job_code or "COMPANY")
    category = PROPOSAL_FOLDERS.get(proposal_type, "其他")
    relative_dir = Path("40-已批准经验") / safe_job_code / category
    filename = f"{reviewed_at:%Y%m%d-%H%M%S}-{_slug(proposal_id)}.md"
    relative_path = relative_dir / filename
    target = _safe_target(root, relative_path)

    title = _proposal_title(proposal_type, safe_payload)
    content = _render_note(
        proposal_id=proposal_id,
        proposal_type=proposal_type,
        title=title,
        payload=safe_payload,
        rationale=rationale,
        source_round_id=source_round_id,
        round_type=round_type,
        job_code=job_code,
        job_title=job_title,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        release_version=release_version,
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    _atomic_write(target, content)

    release_relative = Path("90-知识版本发布记录") / f"{_slug(release_version)}.md"
    release_target = _safe_target(root, release_relative)
    release_content = _render_release_note(
        release_version=release_version,
        proposal_id=proposal_id,
        proposal_type=proposal_type,
        title=title,
        relative_path=relative_path.as_posix(),
        content_hash=content_hash,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
    )
    _atomic_write(release_target, release_content)

    file_without_suffix = relative_path.with_suffix("").as_posix()
    obsidian_uri = (
        f"obsidian://open?vault={quote(vault_name, safe='')}"
        f"&file={quote(file_without_suffix, safe='')}"
    )
    return PublishedKnowledge(
        release_version=release_version,
        relative_path=relative_path.as_posix(),
        content_hash=content_hash,
        obsidian_uri=obsidian_uri,
        release_note_path=release_relative.as_posix(),
    )


def _publishable_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sensitive = sorted(key for key in payload if key.lower() in SENSITIVE_KEYS)
    if sensitive:
        raise KnowledgePublishError(
            f"提案包含禁止写入知识库的字段：{', '.join(sensitive)}"
        )
    filtered = {key: value for key, value in payload.items() if key in PUBLISHABLE_FIELDS}
    if not filtered:
        raise KnowledgePublishError("提案没有可发布的结构化知识字段")
    return filtered


def _proposal_title(proposal_type: str, payload: dict[str, Any]) -> str:
    for key in ("title", "question", "competency_name", "name", "trigger", "summary"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:100]
    return f"{PROPOSAL_LABELS.get(proposal_type, '知识')}更新提案"


def _render_note(**context: Any) -> str:
    payload = context["payload"]
    lines = [
        "---",
        f"kb_id: {_yaml_string('proposal.' + context['proposal_id'])}",
        f"type: {_yaml_string(context['proposal_type'])}",
        "status: approved",
        "version: 1",
        f"release_version: {_yaml_string(context['release_version'])}",
        f"job_code: {_yaml_string(context['job_code'] or 'COMPANY')}",
        f"job_title: {_yaml_string(context['job_title'])}",
        f"round_type: {_yaml_string(context['round_type'])}",
        f"source_round_id: {_yaml_string(context['source_round_id'])}",
        f"reviewed_by: {_yaml_string(context['reviewed_by'])}",
        f"reviewed_at: {_yaml_string(context['reviewed_at'].isoformat())}",
        "contains_pii: false",
        "---",
        "",
        f"# {context['title']}",
        "",
        "## 审批依据",
        "",
        context["rationale"].strip(),
        "",
        "## 结构化知识",
        "",
    ]
    for key, value in payload.items():
        lines.extend(_markdown_field(key, value))
    lines.extend(
        [
            "## 治理信息",
            "",
            f"- 来源面试轮次：`{context['source_round_id']}`",
            f"- 适用轮次：`{context['round_type']}`",
            f"- 审批人：{context['reviewed_by']}",
            f"- 发布版本：`{context['release_version']}`",
            "- 本文不包含候选人简历、录音、逐字稿或身份信息。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_release_note(**context: Any) -> str:
    return "\n".join(
        [
            "---",
            f"kb_id: {_yaml_string('release.' + context['release_version'])}",
            "type: release",
            "status: approved",
            "version: 1",
            f"release_version: {_yaml_string(context['release_version'])}",
            f"released_at: {_yaml_string(context['reviewed_at'].isoformat())}",
            f"owner: {_yaml_string(context['reviewed_by'])}",
            "contains_pii: false",
            "---",
            "",
            f"# {context['release_version']}",
            "",
            f"- 知识类型：{PROPOSAL_LABELS.get(context['proposal_type'], context['proposal_type'])}",
            f"- 知识标题：{context['title']}",
            f"- 来源提案：`{context['proposal_id']}`",
            f"- 发布文件：[[{Path(context['relative_path']).with_suffix('').as_posix()}]]",
            f"- 内容 SHA-256：`{context['content_hash']}`",
            f"- 审批人：{context['reviewed_by']}",
            "",
        ]
    )


def _markdown_field(key: str, value: Any) -> list[str]:
    label = key.replace("_", " ")
    lines = [f"### {label}", ""]
    if isinstance(value, list):
        lines.extend(f"- {_display_value(item)}" for item in value)
    elif isinstance(value, dict):
        lines.append("```json")
        lines.append(json.dumps(value, ensure_ascii=False, indent=2))
        lines.append("```")
    else:
        lines.append(_display_value(value))
    lines.append("")
    return lines


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "未填写"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _yaml_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return slug[:96] or "knowledge"


def _safe_target(root: Path, relative_path: Path) -> Path:
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise KnowledgePublishError("知识文件路径超出配置的 Vault")
    return target


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

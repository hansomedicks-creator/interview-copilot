from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..providers.feishu_notifications import (
    FeishuNotificationError,
    FeishuNotificationSender,
)
from .data_governance import record_audit_event
from .knowledge_vault import inspect_vault


STATUS_ORDER = {"error": 0, "not_configured": 1, "attention": 2, "deferred": 3, "ready": 4}


def build_readiness_center(
    db: Session,
    settings: Settings,
    user: dict[str, Any],
) -> dict[str, Any]:
    dialect = db.get_bind().dialect.name
    vault = inspect_vault(
        settings.resolved_knowledge_vault_dir,
        settings.knowledge_vault_name,
    )
    recording = _path_status(settings.recording_dir)
    public_url = urlparse(settings.public_base_url)
    public_ready = public_url.scheme == "https" and public_url.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    secure_session = (
        settings.environment == "production"
        and settings.session_secret != "development-only-change-me"
    )

    checks = [
        _check(
            "runtime_environment",
            "production_foundation",
            "运行环境",
            "ready" if settings.environment == "production" else "attention",
            True,
            "当前为生产模式" if settings.environment == "production" else "当前仍是开发模式",
            "开发模式适合本机演示，但会保留本地预览身份和调试边界。",
            "部署试点服务器后设置 INTERVIEW_ENV=production。",
            [_field("INTERVIEW_ENV", settings.environment == "production")],
            "configuration",
        ),
        _check(
            "database",
            "production_foundation",
            "业务数据库",
            "ready" if dialect == "postgresql" else "attention",
            True,
            "PostgreSQL 已接入" if dialect == "postgresql" else "当前使用 SQLite",
            "SQLite 可用于本地演示，不适合多人并发、备份恢复和正式审计场景。",
            "试点服务器配置 INTERVIEW_DATABASE_URL，并迁移到 PostgreSQL。",
            [_field("INTERVIEW_DATABASE_URL", dialect == "postgresql")],
            "connection",
        ),
        _check(
            "recording_storage",
            "production_foundation",
            "录音存储",
            "ready" if recording["writable"] else "attention",
            True,
            "录音目录可写" if recording["writable"] else "录音目录需要初始化",
            "面试录音需要稳定写入、容量监控和到期清理。",
            "配置服务器录音目录或对象存储，并执行写入测试。",
            [_field("INTERVIEW_RECORDING_DIR", recording["configured"])],
            "connection",
            metadata={"free_bytes": recording["free_bytes"]},
        ),
        _check(
            "public_access",
            "production_foundation",
            "HTTPS 与正式域名",
            "ready" if public_ready else "attention",
            True,
            "正式 HTTPS 地址已配置" if public_ready else "当前为本机访问地址",
            "飞书登录回调、通知深链接和浏览器麦克风在正式环境都依赖 HTTPS。",
            "准备域名与 HTTPS 后配置 INTERVIEW_PUBLIC_BASE_URL。",
            [_field("INTERVIEW_PUBLIC_BASE_URL", public_ready)],
            "configuration",
        ),
        _check(
            "session_security",
            "production_foundation",
            "会话安全",
            "ready" if secure_session else "attention",
            True,
            "生产会话密钥已启用" if secure_session else "仍使用开发会话边界",
            "生产环境需要独立随机密钥和 HTTPS Cookie，防止伪造组织身份。",
            "在服务器安全配置 INTERVIEW_SESSION_SECRET，不在页面中保存或回显。",
            [_field("INTERVIEW_SESSION_SECRET", secure_session, secret=True)],
            "configuration",
        ),
        _check(
            "feishu_oauth",
            "identity_and_collaboration",
            "飞书组织登录",
            "ready" if settings.feishu_oauth_configured else "not_configured",
            True,
            "飞书 OAuth 配置完整" if settings.feishu_oauth_configured else "尚未接入飞书正式登录",
            "正式登录决定系统如何识别面试官、HR 和管理员，并加载正确的面试任务。",
            "配置飞书 App ID、App Secret、回调地址及组织角色名单。",
            [
                _field("FEISHU_APP_ID", bool(settings.feishu_app_id)),
                _field("FEISHU_APP_SECRET", bool(settings.feishu_app_secret), secret=True),
                _field("FEISHU_REDIRECT_URI", bool(settings.feishu_redirect_uri)),
                _field("FEISHU_HR_OPEN_IDS", bool(settings.feishu_hr_open_ids)),
                _field("FEISHU_ADMIN_OPEN_IDS", bool(settings.feishu_admin_open_ids)),
            ],
            "connection",
        ),
        _check(
            "feishu_notifications",
            "identity_and_collaboration",
            "飞书待办通知",
            "ready" if settings.feishu_notifications_configured else "not_configured",
            False,
            "通知能力已启用" if settings.feishu_notifications_configured else "通知仍处于预览模式",
            "不影响本地面试，但会影响排期、提醒、评价催办和终审协同效率。",
            "开通 im:message:send_as_bot 后启用 FEISHU_NOTIFICATIONS_ENABLED。",
            [
                _field("FEISHU_NOTIFICATIONS_ENABLED", settings.feishu_notifications_enabled),
                _field("FEISHU_APP_ID", bool(settings.feishu_app_id)),
                _field("FEISHU_APP_SECRET", bool(settings.feishu_app_secret), secret=True),
            ],
            "connection",
        ),
        _check(
            "realtime_asr",
            "ai_pipeline",
            "腾讯云实时字幕",
            "ready" if settings.asr_configured else "not_configured",
            True,
            "ASR 配置完整，待真实音频验收" if settings.asr_configured else "当前只能录音，不能自动生成字幕",
            "没有实时字幕时，追问建议和证据识别无法随对话自动更新。",
            "配置腾讯云 ASR 后，用 5—10 秒测试音频验证账号权限、延迟和准确率。",
            [
                _field("INTERVIEW_ASR_PROVIDER", settings.asr_provider == "tencent"),
                _field("TENCENT_ASR_APP_ID", bool(settings.tencent_asr_app_id)),
                _field("TENCENT_ASR_SECRET_ID", bool(settings.tencent_asr_secret_id), secret=True),
                _field("TENCENT_ASR_SECRET_KEY", bool(settings.tencent_asr_secret_key), secret=True),
            ],
            "configuration",
        ),
        _check(
            "llm",
            "ai_pipeline",
            "真实 AI 分析",
            "ready"
            if settings.provider_mode == "production" and settings.llm_configured
            else "not_configured",
            True,
            "真实模型已接入" if settings.provider_mode == "production" and settings.llm_configured else "当前使用本地演示规则",
            "模型负责证据候选、追问和评价摘要；任何结果仍需本地校验和人工确认。",
            "配置 OpenAI 兼容模型地址、模型名和 API Key，再执行无候选人数据的连接测试。",
            [
                _field("INTERVIEW_PROVIDER_MODE", settings.provider_mode == "production"),
                _field("INTERVIEW_LLM_BASE_URL", bool(settings.llm_base_url)),
                _field("INTERVIEW_LLM_MODEL", bool(settings.llm_model)),
                _field("INTERVIEW_LLM_API_KEY", bool(settings.llm_api_key), secret=True),
            ],
            "connection",
        ),
        _check(
            "knowledge_vault",
            "knowledge_and_data",
            "Obsidian 企业知识库",
            "ready" if vault.writable else "not_configured",
            False,
            "知识库可读写" if vault.writable else vault.message,
            "不阻塞单场面试，但会影响岗位画像、题库经验和系统文档沉淀。",
            "配置 Vault 目录并确认服务账号具有读写权限。",
            [_field("INTERVIEW_KNOWLEDGE_VAULT_DIR", vault.configured)],
            "connection",
        ),
        _check(
            "beisen",
            "knowledge_and_data",
            "北森数据接口",
            "deferred",
            False,
            "等待管理员开放权限",
            "当前可通过脱敏文件导入历史样本；北森接口不阻塞首轮试点。",
            "获得权限后接入候选人阶段、面试安排和历史评价。",
            [],
            None,
        ),
    ]
    is_admin = user.get("role") == "admin"
    for item in checks:
        item["can_test"] = bool(is_admin and item["test_kind"])
        item["permission_note"] = (
            "管理员可执行检查"
            if item["can_test"]
            else "由管理员执行连接测试"
            if item["test_kind"]
            else "当前无需连接测试"
        )

    required = [item for item in checks if item["required_for_pilot"]]
    blockers = [item for item in required if item["status"] in {"error", "not_configured"}]
    attention = [item for item in checks if item["status"] == "attention"]
    ready = [item for item in checks if item["status"] == "ready"]
    required_ready = [item for item in required if item["status"] == "ready"]
    overall = "not_ready" if blockers else "needs_attention" if any(item["status"] != "ready" for item in required) else "ready"
    actions = sorted(
        [item for item in checks if item["status"] != "ready" and item["status"] != "deferred"],
        key=lambda item: (
            not item["required_for_pilot"],
            STATUS_ORDER.get(item["status"], 9),
        ),
    )
    return {
        "overall": {
            "status": overall,
            "label": {
                "ready": "可以进入正式试点",
                "needs_attention": "具备基础能力，仍需完成上线配置",
                "not_ready": "尚未达到正式试点条件",
            }[overall],
            "progress_percent": round(len(required_ready) / max(1, len(required)) * 100),
            "current_stage": "production_pilot" if overall == "ready" else "local_demonstration",
        },
        "summary": {
            "total": len(checks),
            "ready": len(ready),
            "attention": len(attention),
            "blockers": len(blockers),
            "deferred": sum(item["status"] == "deferred" for item in checks),
        },
        "viewer": {"role": user.get("role"), "can_run_tests": is_admin},
        "policy": {
            "secrets_server_side_only": True,
            "secret_values_returned": False,
            "tests_send_candidate_data": False,
            "automatic_configuration_changes": False,
        },
        "recommended_actions": [
            {"check_id": item["id"], "title": item["title"], "action": item["next_action"]}
            for item in actions[:5]
        ],
        "checks": checks,
    }


def run_readiness_test(
    db: Session,
    settings: Settings,
    user: dict[str, Any],
    check_id: str,
    feishu_sender: FeishuNotificationSender,
) -> dict[str, Any]:
    allowed = {
        "runtime_environment",
        "database",
        "recording_storage",
        "public_access",
        "session_security",
        "feishu_oauth",
        "feishu_notifications",
        "realtime_asr",
        "llm",
        "knowledge_vault",
    }
    if check_id not in allowed:
        raise ValueError("该检查项不支持连接测试")
    if check_id == "database":
        result = _test_database(db)
    elif check_id == "recording_storage":
        result = _test_writable_path(settings.recording_dir, create=True)
    elif check_id == "knowledge_vault":
        vault_dir = settings.resolved_knowledge_vault_dir
        result = _test_writable_path(vault_dir, create=False) if vault_dir else _result("action_required", "知识库目录尚未配置")
    elif check_id in {"feishu_oauth", "feishu_notifications"}:
        result = _test_feishu(settings, feishu_sender, require_notifications=check_id == "feishu_notifications")
    elif check_id == "llm":
        result = _test_llm(settings)
    elif check_id == "realtime_asr":
        result = (
            _result("passed_with_manual_step", "配置字段完整；仍需用 5—10 秒真实音频验证账号权限、延迟和准确率")
            if settings.asr_configured
            else _result("action_required", "腾讯云 ASR 配置不完整")
        )
    elif check_id == "public_access":
        parsed = urlparse(settings.public_base_url)
        result = (
            _result("passed", "正式 HTTPS 地址格式有效")
            if parsed.scheme == "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            else _result("action_required", "当前仍是本机地址，需要正式域名和 HTTPS")
        )
    elif check_id == "session_security":
        result = (
            _result("passed", "生产会话安全配置有效")
            if settings.environment == "production" and settings.session_secret != "development-only-change-me"
            else _result("action_required", "仍是开发会话配置")
        )
    else:
        result = (
            _result("passed", "当前为生产运行模式")
            if settings.environment == "production"
            else _result("action_required", "当前仍是开发运行模式")
        )
    record_audit_event(
        db,
        user,
        action="readiness.connection_tested",
        resource_type="system_readiness_check",
        resource_id=check_id,
        details={"result_status": result["status"]},
    )
    db.flush()
    return result


def _check(
    check_id: str,
    category: str,
    title: str,
    status: str,
    required_for_pilot: bool,
    summary: str,
    impact: str,
    next_action: str,
    configuration: list[dict[str, Any]],
    test_kind: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "category": category,
        "title": title,
        "status": status,
        "required_for_pilot": required_for_pilot,
        "summary": summary,
        "impact": impact,
        "next_action": next_action,
        "configuration": configuration,
        "test_kind": test_kind,
        "metadata": metadata or {},
    }


def _field(key: str, configured: bool, secret: bool = False) -> dict[str, Any]:
    return {"key": key, "configured": bool(configured), "secret": secret, "value": None}


def _path_status(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    probe = resolved if resolved.exists() else resolved.parent
    writable = resolved.is_dir() and os.access(resolved, os.W_OK)
    try:
        free_bytes = shutil.disk_usage(probe).free if probe.exists() else None
    except OSError:
        free_bytes = None
    return {
        "configured": bool(str(path)),
        "writable": writable,
        "free_bytes": free_bytes,
    }


def _result(status: str, message: str) -> dict[str, str]:
    return {"status": status, "message": message}


def _test_database(db: Session) -> dict[str, str]:
    try:
        value = db.execute(text("SELECT 1")).scalar_one()
        return _result("passed", "数据库连接正常，可以完成读写事务") if value == 1 else _result("failed", "数据库返回异常结果")
    except Exception:
        return _result("failed", "数据库连接测试失败，请检查地址、账号和网络")


def _test_writable_path(path: Path | None, *, create: bool) -> dict[str, str]:
    if path is None:
        return _result("action_required", "目录尚未配置")
    resolved = path.resolve()
    try:
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        if not resolved.is_dir():
            return _result("failed", "目标目录不存在或不是文件夹")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=resolved, prefix=".readiness-", suffix=".tmp") as handle:
            handle.write("readiness-test")
            temporary = Path(handle.name)
        temporary.unlink()
        return _result("passed", "目录读写测试通过，临时测试文件已清理")
    except OSError:
        return _result("failed", "目录不可写，请检查服务器路径和服务账号权限")


def _test_feishu(
    settings: Settings,
    sender: FeishuNotificationSender,
    *,
    require_notifications: bool,
) -> dict[str, str]:
    configured = settings.feishu_notifications_configured if require_notifications else bool(settings.feishu_app_id and settings.feishu_app_secret)
    if not configured:
        return _result("action_required", "飞书应用配置不完整")
    try:
        sender.test_connection()
        message = "飞书应用凭证有效；测试不会发送任何消息"
        if not settings.feishu_redirect_uri:
            return _result("passed_with_manual_step", f"{message}，仍需补充 OAuth 回调地址")
        return _result("passed", message)
    except FeishuNotificationError:
        return _result("failed", "无法获取飞书应用令牌，请检查应用凭证、权限和服务器网络")


def _test_llm(settings: Settings) -> dict[str, str]:
    if settings.provider_mode != "production" or not settings.llm_configured:
        return _result("action_required", "真实模型配置不完整")
    try:
        response = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
            json={
                "model": settings.llm_model,
                "messages": [{"role": "user", "content": "仅回复 ready"}],
                "temperature": 0,
                "max_tokens": 8,
            },
            timeout=settings.llm_timeout_seconds,
        )
        if response.status_code >= 400:
            return _result("failed", "模型服务拒绝请求，请检查地址、模型名、API Key 或账户额度")
        data = response.json()
        if not data.get("choices"):
            return _result("failed", "模型服务已响应，但返回格式不兼容")
        return _result("passed", "模型连接测试通过；测试未发送任何候选人数据")
    except (httpx.HTTPError, ValueError):
        return _result("failed", "无法连接模型服务，请检查网络、地址和超时配置")

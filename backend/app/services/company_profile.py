from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CompanyProfileVersion, new_id
from ..schemas import CompanyProfileDraftSave
from .catalog import competencies_for


ROUND_LABELS = {"business": "业务面", "hr": "HR 面", "ceo": "CEO 面"}
ANTI_BIAS_BOUNDARY = [
    "不使用姓名、性别、年龄、婚育、籍贯等非岗位因素判断候选人",
    "不把名校、知名公司或相似背景直接当成能力证据",
    "公司画像只描述可观察行为和成功证据，不描述理想候选人的个人背景",
]


DEFAULT_COMPETENCIES: list[dict[str, Any]] = [
    {
        "competency_id": "ownership",
        "name": "责任担当",
        "definition": "面对目标、问题和不确定性时，能够明确个人责任并主动推动结果闭环。",
        "positive_evidence": ["能说明本人责任和关键动作", "遇到偏差时主动纠正并复盘"],
        "risk_signals": ["持续把问题归因于他人", "只描述团队成果，无法说明本人贡献"],
        "required_question": "请讲一次结果没有达到预期、但你需要承担主要责任的经历。",
        "follow_up": "你怎么判断先补救哪一部分？其中哪项决定由你本人作出？",
        "primary_round": "business",
        "keywords": ["负责", "责任", "主动", "推动", "结果", "复盘"],
        "score_anchors": {
            "1": "回避责任，无法说明本人行动或结果。",
            "3": "能够完成分内工作，并对结果作出基本复盘。",
            "5": "在复杂约束下主动承担关键责任，推动闭环并形成可复用改进。",
        },
    },
    {
        "competency_id": "learning_agility",
        "name": "学习敏捷",
        "definition": "能够从新信息、失败和反馈中快速调整认知与行动。",
        "positive_evidence": ["能说明判断如何被新证据改变", "学习结果转化为后续行动"],
        "risk_signals": ["复盘停留在口号", "面对反馈时持续为原判断辩解"],
        "required_question": "请讲一次你原来的判断被证明不完整，后来快速调整做法的经历。",
        "follow_up": "什么证据让你改变判断，调整后结果有什么不同？",
        "primary_round": "business",
        "keywords": ["学习", "反馈", "调整", "改变", "复盘", "验证"],
        "score_anchors": {
            "1": "无法举出基于反馈改变做法的案例。",
            "3": "能接受反馈并对具体做法进行调整。",
            "5": "主动寻找反证，快速迭代并将经验沉淀为方法。",
        },
    },
    {
        "competency_id": "collaboration",
        "name": "协作共赢",
        "definition": "能够理解不同角色的目标，在分歧中建立共识并推动共同结果。",
        "positive_evidence": ["能还原真实分歧及对方诉求", "通过具体行动建立共同目标"],
        "risk_signals": ["把协作理解为一味妥协", "将冲突简单归因为对方难沟通"],
        "required_question": "请讲一次你需要推动不同立场的人共同完成目标的经历。",
        "follow_up": "对方最初的核心诉求是什么，你具体改变了什么沟通或方案？",
        "primary_round": "hr",
        "keywords": ["协作", "沟通", "共识", "冲突", "团队", "协调"],
        "score_anchors": {
            "1": "不能理解他方诉求，主要依靠权力或回避处理分歧。",
            "3": "能沟通分歧并完成基本协作。",
            "5": "能重构共同目标，在复杂利益关系中形成持续合作。",
        },
    },
    {
        "competency_id": "integrity",
        "name": "诚信与原则",
        "definition": "面对压力和利益冲突时，仍能诚实呈现事实并守住组织基本原则。",
        "positive_evidence": ["主动暴露风险和不利信息", "能说明原则、代价与处理过程"],
        "risk_signals": ["刻意隐藏关键信息", "为了短期结果突破明确合规或诚信底线"],
        "required_question": "请讲一次坚持正确做法会增加短期压力，但你仍然选择坚持的经历。",
        "follow_up": "你承担了什么代价，又如何向相关人员说明事实？",
        "primary_round": "hr",
        "keywords": ["诚信", "原则", "事实", "风险", "透明", "合规"],
        "score_anchors": {
            "1": "对事实和责任表述反复，或认可用不当方式换取结果。",
            "3": "能够遵守明确规则并如实沟通问题。",
            "5": "在高压和利益冲突中仍主动保护长期信任与组织原则。",
        },
    },
    {
        "competency_id": "long_term_value",
        "name": "长期价值意识",
        "definition": "能够把短期行动与客户价值、组织能力和长期结果连接起来。",
        "positive_evidence": ["能说明短期与长期的取舍标准", "关注客户或经营结果而非表面完成"],
        "risk_signals": ["只追求短期数字而忽略长期损害", "无法说明工作如何创造实际价值"],
        "required_question": "请讲一次你需要在短期结果和长期价值之间作出取舍的经历。",
        "follow_up": "你的判断标准、验证指标和止损条件分别是什么？",
        "primary_round": "ceo",
        "keywords": ["长期", "价值", "客户", "经营", "取舍", "指标"],
        "score_anchors": {
            "1": "只关注表面交付或短期数字，无法识别长期影响。",
            "3": "能够兼顾短期目标和基本长期影响。",
            "5": "能建立清晰取舍机制，让短期行动持续积累客户与组织价值。",
        },
    },
]


DEFAULT_RED_LINES = [
    "伪造或故意隐瞒影响任职判断的关键信息",
    "为了短期结果突破明确的诚信、合规或客户利益底线",
    "对歧视、骚扰或严重不尊重他人的行为缺乏基本边界",
]


def default_company_profile_template() -> dict[str, Any]:
    return {
        "company_name": "本公司",
        "profile_purpose": "定义所有岗位共同遵循的可观察行为标准，为岗位画像、统一面试问题和证据评价提供公司级底座。",
        "competencies": DEFAULT_COMPETENCIES,
        "red_lines": DEFAULT_RED_LINES,
        "anti_bias_boundary": ANTI_BIAS_BOUNDARY,
        "generator_version": "company-profile-v0.1",
    }


def active_company_profile(db: Session) -> CompanyProfileVersion | None:
    return db.scalar(
        select(CompanyProfileVersion)
        .where(CompanyProfileVersion.status == "active")
        .order_by(CompanyProfileVersion.version_number.desc())
    )


def save_company_profile_draft(
    db: Session,
    *,
    payload: CompanyProfileDraftSave,
    created_by: str,
) -> CompanyProfileVersion:
    active = active_company_profile(db)
    current = db.scalar(
        select(CompanyProfileVersion)
        .where(CompanyProfileVersion.status == "draft")
        .order_by(CompanyProfileVersion.version_number.desc())
    )
    profile_payload = {
        "company_name": payload.company_name.strip(),
        "profile_purpose": payload.profile_purpose.strip(),
        "competencies": [
            _normalized_competency(item.model_dump(), index)
            for index, item in enumerate(payload.competencies)
        ],
        "red_lines": payload.red_lines,
        "anti_bias_boundary": ANTI_BIAS_BOUNDARY,
        "generator_version": "company-profile-v0.1",
        "version_parent": active.version_label if active else None,
    }
    if current:
        current.company_name = payload.company_name.strip()
        current.profile_payload = profile_payload
        current.change_summary = payload.change_summary.strip()
        current.created_by = created_by
        current.source_mode = "hr_revision" if active else "hr_manual"
        return current

    highest = db.scalar(select(func.max(CompanyProfileVersion.version_number))) or 0
    version_number = highest + 1
    version = CompanyProfileVersion(
        id=new_id("cpv"),
        company_name=payload.company_name.strip(),
        version_number=version_number,
        version_label=f"company-profile-v{version_number}",
        status="draft",
        source_mode="hr_revision" if active else "hr_manual",
        profile_payload=profile_payload,
        change_summary=payload.change_summary.strip(),
        created_by=created_by,
    )
    db.add(version)
    db.flush()
    return version


def company_profile_version_payload(version: CompanyProfileVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "company_name": version.company_name,
        "version_number": version.version_number,
        "version_label": version.version_label,
        "status": version.status,
        "source_mode": version.source_mode,
        "profile_payload": version.profile_payload,
        "change_summary": version.change_summary,
        "created_by": version.created_by,
        "approved_by": version.approved_by,
        "approved_at": version.approved_at,
        "publication": {
            "status": version.publication_status,
            "release_version": version.release_version,
            "relative_path": version.relative_path,
            "content_hash": version.content_hash,
            "obsidian_uri": version.obsidian_uri,
            "error_message": version.publication_error,
        },
        "created_at": version.created_at,
    }


def build_company_profile_center(db: Session) -> dict[str, Any]:
    versions = list(
        db.scalars(
            select(CompanyProfileVersion).order_by(CompanyProfileVersion.version_number.desc())
        ).all()
    )
    active = next((item for item in versions if item.status == "active"), None)
    draft = next((item for item in versions if item.status == "draft"), None)
    editor = (
        draft.profile_payload
        if draft
        else active.profile_payload
        if active
        else default_company_profile_template()
    )
    return {
        "active_version": company_profile_version_payload(active),
        "draft_version": company_profile_version_payload(draft),
        "versions": [company_profile_version_payload(item) for item in versions],
        "editor_payload": editor,
        "governance": {
            "recommended_competency_count": "建议设置 5–7 项公司通用能力，系统允许 3–8 项",
            "inheritance": "公司基础画像 → 岗位人才画像 → 当前面试轮次",
            "activation": "只有 HR 人工确认后的版本才会进入新面试题与岗位画像",
            "history": "新版本生效时保留旧版本，已完成面试不会被改写",
            "boundary": "只能使用可观察行为，不使用个人背景或受保护特征",
        },
    }


def effective_competencies(
    db: Session,
    round_type: str,
    job_competencies: list[dict],
) -> list[dict[str, Any]]:
    base = [dict(item) for item in competencies_for(round_type, job_competencies)]
    active = active_company_profile(db)
    if not active:
        return base
    company_items = []
    for item in (active.profile_payload or {}).get("competencies", []):
        if item.get("primary_round") != round_type:
            continue
        company_items.append(
            {
                "id": f"company.{item['competency_id']}",
                "name": f"公司通用 · {item['name']}",
                "description": item.get("definition", ""),
                "positive_evidence": list(item.get("positive_evidence", [])),
                "risk_signals": list(item.get("risk_signals", [])),
                "score_anchors": dict(item.get("score_anchors", {})),
                "evidence_requirements": ["具体情境", "本人行动", "可验证结果", "反思与复盘"],
                "keywords": item.get("keywords", []),
                "question": item.get("required_question", ""),
                "follow_up": item.get("follow_up", ""),
                "source": "company_profile",
                "company_profile_version": active.version_label,
            }
        )
    known = {item.get("id") for item in company_items}
    return [*company_items, *(item for item in base if item.get("id") not in known)]


def company_required_questions(db: Session, round_type: str) -> tuple[list[dict[str, Any]], str | None]:
    active = active_company_profile(db)
    if not active:
        return [], None
    questions = []
    for item in (active.profile_payload or {}).get("competencies", []):
        if item.get("primary_round") != round_type:
            continue
        competency_id = f"company.{item['competency_id']}"
        questions.append(
            {
                "id": f"q-{round_type}-{competency_id}",
                "competency_id": competency_id,
                "competency_name": f"公司通用 · {item['name']}",
                "question": item["required_question"],
                "follow_up": item["follow_up"],
                "keywords": item.get("keywords", []),
                "required": True,
                "source": "company_standard",
                "rationale": "来自当前生效的公司基础人才画像",
                "source_evidence": active.version_label,
            }
        )
    return questions, active.version_label


def company_foundation_snapshot(db: Session) -> dict[str, Any] | None:
    active = active_company_profile(db)
    if not active:
        return None
    profile = active.profile_payload or {}
    return {
        "version_label": active.version_label,
        "company_name": active.company_name,
        "profile_purpose": profile.get("profile_purpose"),
        "competencies": [
            {
                "competency_id": item.get("competency_id"),
                "name": item.get("name"),
                "definition": item.get("definition"),
                "primary_round": item.get("primary_round"),
            }
            for item in profile.get("competencies", [])
        ],
        "red_lines": profile.get("red_lines", []),
    }


def company_profile_payload_for_publication(version: CompanyProfileVersion) -> dict[str, Any]:
    profile = version.profile_payload or {}
    return {
        "title": f"{version.company_name}基础人才画像 · {version.version_label}",
        "summary": profile.get("profile_purpose"),
        "round_types": ["business", "hr", "ceo"],
        "must_have": [
            {
                "competency_id": f"company.{item.get('competency_id')}",
                "competency_name": item.get("name"),
                "definition": item.get("definition"),
                "round_type": item.get("primary_round"),
                "positive_evidence": item.get("positive_evidence", []),
                "risk_signals": item.get("risk_signals", []),
                "required_question": item.get("required_question"),
                "follow_up": item.get("follow_up"),
                "score_anchors": item.get("score_anchors", {}),
            }
            for item in profile.get("competencies", [])
        ],
        "risk_signals": profile.get("red_lines", []),
        "evidence_requirements": ["具体情境", "本人行动", "可验证结果", "反思与复盘"],
        "anti_bias_boundary": profile.get("anti_bias_boundary", ANTI_BIAS_BOUNDARY),
        "reason": version.change_summary,
    }


def _normalized_competency(item: dict[str, Any], index: int) -> dict[str, Any]:
    competency_id = (item.get("competency_id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", competency_id):
        digest = hashlib.sha256(item["name"].strip().encode("utf-8")).hexdigest()[:8]
        competency_id = f"core-{index + 1}-{digest}"
    item["competency_id"] = competency_id
    item["name"] = item["name"].strip()
    item["definition"] = item["definition"].strip()
    item["required_question"] = item["required_question"].strip()
    item["follow_up"] = item["follow_up"].strip()
    item["score_anchors"] = {
        key: value.strip() for key, value in item["score_anchors"].items()
    }
    return item

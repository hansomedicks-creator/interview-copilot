from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    HistoricalHiringSample,
    InterviewRound,
    Job,
    Scorecard,
    TalentProfileVersion,
    new_id,
)
from .catalog import ROUND_CATALOG
from .company_profile import company_foundation_snapshot
from .job_semantics import build_local_job_semantic_profile


MIN_OUTCOME_SAMPLES = 3
PROFILE_GENERATOR_VERSION = "talent-profile-v0.1"
ROUND_LABELS = {"business": "业务面", "hr": "HR 面", "ceo": "CEO 面"}
POSITIVE_HISTORICAL_OUTCOMES = {"offer_approval", "hired", "probation_passed"}


def build_or_refresh_profile_draft(
    db: Session,
    *,
    job: Job,
    created_by: str,
) -> tuple[TalentProfileVersion, bool]:
    active = db.scalar(
        select(TalentProfileVersion).where(
            TalentProfileVersion.job_id == job.id,
            TalentProfileVersion.status == "active",
        )
    )
    profile_payload, evidence_summary, change_summary, source_mode = _build_profile(
        db, job, active
    )
    current_draft = db.scalar(
        select(TalentProfileVersion)
        .where(
            TalentProfileVersion.job_id == job.id,
            TalentProfileVersion.status == "draft",
        )
        .order_by(TalentProfileVersion.version_number.desc())
    )
    if current_draft:
        changed = (
            current_draft.evidence_summary.get("sample_signature")
            != evidence_summary.get("sample_signature")
            or current_draft.profile_payload != profile_payload
        )
        if changed:
            current_draft.profile_payload = profile_payload
            current_draft.evidence_summary = evidence_summary
            current_draft.change_summary = change_summary
            current_draft.source_mode = source_mode
            current_draft.created_by = created_by
        return current_draft, changed

    highest = db.scalar(
        select(func.max(TalentProfileVersion.version_number)).where(
            TalentProfileVersion.job_id == job.id
        )
    ) or 0
    version_number = highest + 1
    version = TalentProfileVersion(
        id=new_id("tpv"),
        job_id=job.id,
        version_number=version_number,
        version_label=f"profile-v{version_number}",
        status="draft",
        source_mode=source_mode,
        profile_payload=profile_payload,
        evidence_summary=evidence_summary,
        change_summary=change_summary,
        created_by=created_by,
    )
    db.add(version)
    db.flush()
    return version, True


def maybe_refresh_outcome_profile_draft(
    db: Session,
    *,
    job: Job,
    created_by: str,
) -> TalentProfileVersion | None:
    active = db.scalar(
        select(TalentProfileVersion).where(
            TalentProfileVersion.job_id == job.id,
            TalentProfileVersion.status == "active",
        )
    )
    if not active:
        return None
    summary = collect_outcome_summary(db, job)
    if not summary["threshold_met"]:
        return None
    draft, _ = build_or_refresh_profile_draft(db, job=job, created_by=created_by)
    return draft


def collect_outcome_summary(db: Session, job: Job) -> dict[str, Any]:
    applications = list(
        db.scalars(
            select(Application).where(
                Application.job_id == job.id,
                Application.human_final_decision == "offer_approval",
            )
        ).all()
    )
    application_ids = [item.id for item in applications]
    rounds = list(
        db.scalars(
            select(InterviewRound).where(
                InterviewRound.application_id.in_(application_ids)
            )
        ).all()
    ) if application_ids else []
    round_by_id = {item.id: item for item in rounds}
    round_ids = list(round_by_id)
    scorecards = list(
        db.scalars(
            select(Scorecard).where(
                Scorecard.interview_round_id.in_(round_ids),
                Scorecard.status == "submitted",
            )
        ).all()
    ) if round_ids else []

    scores_by_competency: dict[str, list[int]] = defaultdict(list)
    names: dict[str, str] = {}
    for scorecard in scorecards:
        for item in scorecard.final_scores or []:
            if item.get("assessment") != "human_confirmed" or item.get("score") is None:
                continue
            competency_id = item.get("competency_id")
            if not competency_id:
                continue
            scores_by_competency[competency_id].append(int(item["score"]))
            names[competency_id] = item.get("competency_name") or competency_id

    historical_samples = list(
        db.scalars(
            select(HistoricalHiringSample).where(HistoricalHiringSample.job_id == job.id)
        ).all()
    )
    positive_historical = [
        item for item in historical_samples if item.outcome in POSITIVE_HISTORICAL_OUTCOMES
    ]
    historical_signals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "positive": 0, "negative": 0}
    )
    for sample in positive_historical:
        for signal in sample.competency_signals or []:
            competency_id = signal.get("competency_id")
            if not competency_id:
                continue
            names[competency_id] = signal.get("competency_name") or competency_id
            historical_signals[competency_id]["total"] += 1
            direction = signal.get("direction")
            if direction in {"positive", "negative"}:
                historical_signals[competency_id][direction] += 1

    observed = [
        {
            "competency_id": competency_id,
            "competency_name": names[competency_id],
            "observation_count": len(scores_by_competency.get(competency_id, []))
            + historical_signals[competency_id]["total"],
            "average_human_score": (
                round(sum(scores_by_competency[competency_id]) / len(scores_by_competency[competency_id]), 2)
                if scores_by_competency.get(competency_id)
                else None
            ),
            "historical_signal_count": historical_signals[competency_id]["total"],
            "positive_historical_signals": historical_signals[competency_id]["positive"],
            "negative_historical_signals": historical_signals[competency_id]["negative"],
        }
        for competency_id in set(scores_by_competency) | set(historical_signals)
    ]
    observed.sort(
        key=lambda item: (item["observation_count"], item["average_human_score"] or 0),
        reverse=True,
    )
    signature_source = {
        "application_ids": sorted(application_ids),
        "scorecards": sorted(
            (item.id, item.submitted_at.isoformat() if item.submitted_at else "")
            for item in scorecards
        ),
        "historical_samples": sorted(
            (item.id, item.record_hash, item.outcome) for item in historical_samples
        ),
    }
    sample_signature = hashlib.sha256(
        json.dumps(signature_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    eligible_samples = len(applications) + len(positive_historical)
    performance_validated = sum(
        item.outcome == "probation_passed" for item in historical_samples
    )
    return {
        "eligible_offer_samples": eligible_samples,
        "live_offer_samples": len(applications),
        "historical_positive_samples": len(positive_historical),
        "performance_validated_samples": performance_validated,
        "historical_total_samples": len(historical_samples),
        "submitted_scorecards": len(scorecards),
        "confirmed_score_observations": sum(len(values) for values in scores_by_competency.values()),
        "minimum_outcome_samples": MIN_OUTCOME_SAMPLES,
        "threshold_met": eligible_samples >= MIN_OUTCOME_SAMPLES,
        "evidence_grade": (
            "validated"
            if performance_validated
            else "candidate"
            if eligible_samples >= MIN_OUTCOME_SAMPLES
            else "exploratory"
        ),
        "observed_competencies": observed[:8],
        "sample_signature": sample_signature,
        "boundary": (
            "试用期通过样本可作为岗位成功的初步验证，其余录用与入职结果仍只是招聘过程信号。"
            if performance_validated
            else "录用审批和入职只是招聘决策信号，不等同于入职后的绩效验证。"
        ),
    }


def profile_payload_for_publication(version: TalentProfileVersion, job: Job) -> dict[str, Any]:
    profile = version.profile_payload or {}
    return {
        "title": f"{job.title}人才画像 · {version.version_label}",
        "summary": profile.get("summary"),
        "round_types": ["business", "hr", "ceo"],
        "must_have": profile.get("must_have", []),
        "success_outcomes": profile.get("success_outcomes", []),
        "positive_signals": profile.get("positive_signals", []),
        "risk_signals": profile.get("risk_signals", []),
        "evidence_requirements": profile.get("evidence_requirements", []),
        "company_foundation": profile.get("company_foundation"),
        "reason": version.change_summary,
    }


def version_payload(version: TalentProfileVersion) -> dict[str, Any]:
    public_evidence = {
        key: value
        for key, value in (version.evidence_summary or {}).items()
        if key not in {"sample_signature", "job_definition_signature"}
    }
    return {
        "id": version.id,
        "job_id": version.job_id,
        "version_number": version.version_number,
        "version_label": version.version_label,
        "status": version.status,
        "source_mode": version.source_mode,
        "profile_payload": version.profile_payload,
        "evidence_summary": public_evidence,
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


def _build_profile(
    db: Session,
    job: Job,
    active: TalentProfileVersion | None,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    outcome = collect_outcome_summary(db, job)
    job_definition_signature = _job_definition_signature(job)
    active_job_definition_signature = (
        (active.evidence_summary or {}).get("job_definition_signature") if active else None
    )
    job_definition_changed = bool(
        active and active_job_definition_signature != job_definition_signature
    )
    baseline = (
        _baseline_profile(job)
        if not active or job_definition_changed
        else dict(active.profile_payload)
    )
    outcome["job_definition_signature"] = job_definition_signature
    company_foundation = company_foundation_snapshot(db)
    active_company_version = (
        ((active.profile_payload or {}).get("company_foundation") or {}).get("version_label")
        if active
        else None
    )
    company_version = (company_foundation or {}).get("version_label")
    company_inheritance_changed = bool(
        active and company_version and company_version != active_company_version
    )
    baseline["company_foundation"] = company_foundation
    source_mode = (
        "jd_revision"
        if job_definition_changed
        else "company_inheritance"
        if company_inheritance_changed and not outcome["threshold_met"]
        else "outcome_aggregation"
        if active or outcome["threshold_met"]
        else "jd_baseline"
    )
    baseline["generator_version"] = PROFILE_GENERATOR_VERSION
    baseline["observed_signals"] = outcome["observed_competencies"] if outcome["threshold_met"] else []
    if active:
        baseline["version_parent"] = active.version_label
        baseline["sample_warning"] = outcome["boundary"]
        if job_definition_changed:
            change_summary = (
                "岗位 JD 已由 HR 更新；本草稿重新提取岗位重点并组合当前公司标准，"
                "只影响尚未开始的面试，等待 HR 审核后生效。"
            )
        elif company_inheritance_changed and not outcome["threshold_met"]:
            change_summary = (
                f"继承新的公司基础画像 {company_version}；岗位自身能力标准保持不变，"
                "等待 HR 核对公司通用标准与岗位要求的组合。"
            )
        elif outcome["threshold_met"]:
            top_names = [item["competency_name"] for item in outcome["observed_competencies"][:3]]
            change_summary = (
                f"基于 {outcome['eligible_offer_samples']} 份有效录用/历史样本形成候选更新；"
                f"重点复核 {'、'.join(top_names) or '现有能力项'}，不自动修改录用标准。"
            )
        else:
            change_summary = (
                f"当前只有 {outcome['eligible_offer_samples']} 份有效录用/历史样本，"
                f"未达到 {MIN_OUTCOME_SAMPLES} 份门槛；草稿仅供观察，暂不能生效。"
            )
    else:
        if outcome["threshold_met"]:
            top_names = [item["competency_name"] for item in outcome["observed_competencies"][:3]]
            change_summary = (
                f"根据岗位 JD 与 {outcome['historical_positive_samples']} 份脱敏历史成功样本建立首版；"
                f"重点复核 {'、'.join(top_names) or '现有能力项'}，由 HR 审核后生效。"
            )
        else:
            change_summary = "根据岗位 JD 与业务面、HR 面、CEO 面的标准能力项建立首次人才画像基线。"
    if company_foundation and not company_inheritance_changed:
        change_summary += f" 已继承公司基础画像 {company_foundation['version_label']}。"
    return baseline, outcome, change_summary, source_mode


def _job_definition_signature(job: Job) -> str:
    source = {
        "title": job.title.strip(),
        "jd_text": job.jd_text.strip(),
        "competencies": job.competencies or [],
    }
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _baseline_profile(job: Job) -> dict[str, Any]:
    must_have: list[dict[str, Any]] = []
    if job.competencies:
        for item in job.competencies:
            must_have.append({
                "competency_id": item.get("id"),
                "competency_name": item.get("name"),
                "definition": item.get("description"),
                "round_type": "custom",
                "evidence_requirements": ["具体情境", "本人行动", "可量化结果"],
            })
    semantic_profile = job.semantic_profile or build_local_job_semantic_profile(
        job.title, job.jd_text
    )
    semantic_dimensions = list(semantic_profile.get("interview_dimensions", []))
    if not must_have and semantic_dimensions:
        for item in semantic_dimensions:
            must_have.append(
                {
                    "competency_id": f"jd_semantic.{item.get('id')}",
                    "competency_name": item.get("name") or "岗位职责核实",
                    "definition": item.get("definition")
                    or "结合岗位实际职责核实可观察行为证据",
                    "round_type": item.get("round_type") or "custom",
                    "evidence_requirements": [
                        item.get("evidence_target")
                        or "具体情境、本人行动与可验证结果"
                    ],
                    "source_excerpt": item.get("source_excerpt"),
                }
            )
    if not must_have:
        for round_type in ("business", "hr", "ceo"):
            for item in ROUND_CATALOG[round_type][:2]:
                must_have.append({
                    "competency_id": item["id"],
                    "competency_name": item["name"],
                    "definition": item["description"],
                    "round_type": round_type,
                    "evidence_requirements": ["具体情境", "本人行动", "可量化结果"],
                })

    outcomes = list(semantic_profile.get("business_outcomes", []))
    if not outcomes:
        outcomes = ["形成与岗位使命一致、可衡量且可复盘的业务结果"]
    return {
        "summary": semantic_profile.get("role_mission")
        or f"用于{job.title}三轮面试的一致性评价基线，强调可复核行为证据。",
        "must_have": must_have,
        "success_outcomes": outcomes,
        "interview_focus_by_round": {
            round_type: [item["competency_name"] for item in must_have if item["round_type"] == round_type]
            for round_type in ("business", "hr", "ceo")
        },
        "positive_signals": ["能区分团队成果与本人责任边界", "能够解释关键机制和判断依据", "面对反证会修正判断"],
        "risk_signals": ["只给结论而无行为证据", "将团队成绩全部归因于个人", "未验证不等于不符合"],
        "evidence_requirements": ["每项评分必须引用已确认面试证据", "结论与证据不足时进入下一轮验证", "AI 不自动作出录用或淘汰决定"],
        "anti_bias_boundary": ["不使用姓名、性别、年龄、籍贯等受保护特征", "不把公司知名度或学历作为能力替代指标", "人才画像只定义岗位成功证据，不定义理想候选人的个人背景"],
        "jd_semantic_model": {
            "analysis_mode": semantic_profile.get("analysis_mode"),
            "provider": semantic_profile.get("provider"),
            "excluded_non_job_factors": semantic_profile.get(
                "excluded_non_job_factors", []
            ),
        },
    }

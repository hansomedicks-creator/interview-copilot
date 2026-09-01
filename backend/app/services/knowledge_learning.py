from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import InterviewRound, Job, KnowledgeProposal, Scorecard, new_id


GENERATOR_VERSION = "knowledge-learning-v0.1"
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


@dataclass(frozen=True)
class KnowledgeLearningResult:
    created: int
    refreshed: int
    proposal_ids: list[str]
    pending_review: int

    def as_payload(self) -> dict[str, Any]:
        if self.pending_review:
            status = "pending_hr_review"
        elif self.proposal_ids:
            status = "already_governed"
        else:
            status = "no_reusable_gap"
        return {
            "status": status,
            "created_this_submission": self.created,
            "refreshed_this_submission": self.refreshed,
            "proposal_count": len(self.proposal_ids),
            "pending_hr_review": self.pending_review,
            "proposal_ids": self.proposal_ids,
            "generator_version": GENERATOR_VERSION,
            "policy": "仅从已提交的人工评价中形成脱敏提案；仍需 HR 审批后才能发布。",
        }


@dataclass(frozen=True)
class _ProposalSpec:
    fingerprint: str
    proposal_type: str
    payload: dict[str, Any]
    rationale_template: str


def generate_interview_knowledge_proposals(
    db: Session,
    *,
    interview: InterviewRound,
    job: Job,
    scorecard: Scorecard,
    candidate_name: str,
) -> KnowledgeLearningResult:
    """Create reviewable, de-identified improvements after human submission.

    The source material is intentionally limited to JD-derived question gaps. Resume
    questions, transcript excerpts, evidence quotes and human free-text notes never
    enter the proposal payload.
    """

    if scorecard.status != "submitted":
        return KnowledgeLearningResult(0, 0, [], 0)

    specs = _build_specs(interview, job, scorecard, candidate_name)
    if not specs:
        return KnowledgeLearningResult(0, 0, [], 0)

    fingerprints = {item.fingerprint for item in specs}
    existing_by_fingerprint: dict[str, KnowledgeProposal] = {}
    for proposal in db.scalars(select(KnowledgeProposal)).all():
        fingerprint = (proposal.payload or {}).get("_fingerprint")
        if fingerprint in fingerprints:
            existing_by_fingerprint[fingerprint] = proposal

    created = 0
    refreshed = 0
    selected: list[KnowledgeProposal] = []
    for spec in specs:
        proposal = existing_by_fingerprint.get(spec.fingerprint)
        if proposal is None:
            payload = _with_occurrence_metadata(spec.payload, spec.fingerprint, [interview.id])
            proposal = KnowledgeProposal(
                id=new_id("kp"),
                source_round_id=interview.id,
                proposal_type=spec.proposal_type,
                payload=payload,
                rationale=_rationale(spec.rationale_template, 1),
                status="pending",
            )
            db.add(proposal)
            db.flush()
            existing_by_fingerprint[spec.fingerprint] = proposal
            created += 1
        elif proposal.status == "pending":
            prior_payload = proposal.payload or {}
            round_ids = list(dict.fromkeys([
                *prior_payload.get("_source_round_ids", [proposal.source_round_id]),
                interview.id,
            ]))
            next_payload = _with_occurrence_metadata(spec.payload, spec.fingerprint, round_ids)
            next_rationale = _rationale(spec.rationale_template, len(round_ids))
            if next_payload != prior_payload or next_rationale != proposal.rationale:
                proposal.payload = next_payload
                proposal.rationale = next_rationale
                refreshed += 1
        selected.append(proposal)

    pending_review = sum(item.status == "pending" for item in selected)
    return KnowledgeLearningResult(
        created=created,
        refreshed=refreshed,
        proposal_ids=[item.id for item in selected],
        pending_review=pending_review,
    )


def _build_specs(
    interview: InterviewRound,
    job: Job,
    scorecard: Scorecard,
    candidate_name: str,
) -> list[_ProposalSpec]:
    questions = {
        item.get("id"): item
        for item in (interview.plan_payload or {}).get("questions", [])
        if item.get("id")
    }
    next_round = {
        item.get("source_question_id"): item
        for item in (scorecard.next_round_questions or [])
        if item.get("source_question_id")
    }
    assessments = [
        item
        for item in (scorecard.recommendation or {}).get("jd_assessments", [])
        if item.get("status") in {"shallow", "unanswered"}
    ]

    specs: list[_ProposalSpec] = []
    for assessment in assessments[:2]:
        question_id = assessment.get("question_id")
        source = questions.get(question_id, {})
        if source.get("source") != "resume_jd_match":
            continue
        suggestion = next_round.get(question_id, {})
        question_text = _clean_text(
            suggestion.get("question") or source.get("question") or "",
            candidate_name,
        )
        if not question_text:
            continue
        competency_id = source.get("competency_id") or "jd_verification"
        competency_name = _clean_text(
            source.get("competency_name") or assessment.get("competency_name") or "岗位重点核实",
            candidate_name,
        )
        common_key = f"{job.id}|{interview.round_type}|{question_id}"
        specs.append(
            _ProposalSpec(
                fingerprint=_fingerprint(f"question|{common_key}"),
                proposal_type="question",
                payload={
                    "question": question_text,
                    "competency_id": competency_id,
                    "competency_name": competency_name,
                    "round_type": interview.round_type,
                    "required": False,
                    "follow_up": "请继续核实具体情境、本人行动、量化结果和事后复盘。",
                    "evidence_requirements": ["具体情境", "本人行动", "量化结果", "复盘与反思"],
                    "reason": "该岗位重点在已完成人工评价的面试中仍缺少可复核证据。",
                },
                rationale_template="该 JD 重点仍未形成充分证据，建议作为岗位题库候选问题。",
            )
        )

        if assessment.get("status") == "shallow" and not any(
            item.proposal_type == "follow_up_rule" for item in specs
        ):
            missing = [
                _clean_text(item, candidate_name)
                for item in assessment.get("missing_dimensions", [])
                if _clean_text(item, candidate_name)
            ]
            specs.append(
                _ProposalSpec(
                    fingerprint=_fingerprint(f"follow_up_rule|{common_key}"),
                    proposal_type="follow_up_rule",
                    payload={
                        "title": f"{competency_name}：回答较浅时的结构化追问",
                        "competency_id": competency_id,
                        "competency_name": competency_name,
                        "round_type": interview.round_type,
                        "trigger": "回答仅停留在经历或结论，尚未暴露足以影响岗位判断的关键事实。",
                        "suggestion": "先识别原问题意图，再从实现机制、判断依据、责任边界、约束或修正中选择一个最关键缺口追问；结果和指标仅在成果主张或 KPI 场景使用。",
                        "follow_ups": [
                            "其中最关键的一步为什么这样设计？",
                            "当时哪条信息真正改变了你的判断？",
                            "如果没有团队协助，哪一部分你仍能独立完成？",
                        ],
                        "evidence_requirements": missing or ["实现机制", "判断依据", "责任边界"],
                        "reason": "已提交的人工评价显示该问题获得了回答，但证据深度不足。",
                    },
                    rationale_template="该 JD 问题出现回答较浅的情况，建议沉淀统一追问规则。",
                )
            )

    return specs[:3]


def _with_occurrence_metadata(
    payload: dict[str, Any], fingerprint: str, source_round_ids: list[str]
) -> dict[str, Any]:
    return {
        **payload,
        "_auto_generated": True,
        "_fingerprint": fingerprint,
        "_generator_version": GENERATOR_VERSION,
        "_occurrence_count": len(source_round_ids),
        "_source_round_ids": source_round_ids,
    }


def _rationale(template: str, occurrence_count: int) -> str:
    if occurrence_count <= 1:
        return f"{template} 当前来自 1 场已提交人工评价的面试，需 HR 审批。"
    return f"{template} 已在 {occurrence_count} 场已提交人工评价的面试中出现，需 HR 审批。"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_text(value: Any, candidate_name: str) -> str:
    text = str(value or "").strip()
    if candidate_name and candidate_name.strip():
        text = text.replace(candidate_name.strip(), "候选人")
    text = EMAIL_RE.sub("[已脱敏邮箱]", text)
    text = MOBILE_RE.sub("[已脱敏手机号]", text)
    return text[:800]

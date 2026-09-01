from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import InterviewRound, Job
from .company_profile import effective_competencies
from .job_semantics import build_local_job_semantic_profile


ROUND_LABELS = {
    "business": "业务面",
    "hr": "HR 面",
    "ceo": "CEO 面",
    "custom": "补充面试",
}


def round_evaluation_dimensions(
    db: Session,
    interview: InterviewRound,
    job: Job,
) -> list[dict[str, Any]]:
    """Combine role-specific competencies with this round's JD evidence dimensions."""
    dimensions = [
        {**item, "evaluation_source": item.get("source", "round_competency")}
        for item in effective_competencies(db, interview.round_type, job.competencies)
    ]
    known = {str(item.get("id")) for item in dimensions}
    semantic_profile = job.semantic_profile or build_local_job_semantic_profile(
        job.title, job.jd_text
    )
    for item in semantic_profile.get("interview_dimensions", []):
        if item.get("round_type") != interview.round_type:
            continue
        dimension_id = f"jd_semantic.{item.get('id')}"
        if dimension_id in known:
            continue
        name = str(item.get("name") or "岗位职责证据")
        evidence_target = str(
            item.get("evidence_target") or "具体情境、本人行动与可验证结果"
        )
        dimensions.append(
            {
                "id": dimension_id,
                "name": f"岗位证据 · {name}",
                "description": str(
                    item.get("definition") or f"核实候选人在{name}方面的岗位相关行为"
                ),
                "positive_evidence": [evidence_target],
                "risk_signals": ["只能给出抽象结论，无法说明本人行动或可核验结果"],
                "score_anchors": {
                    "1": "没有岗位相关事实，或已有事实与岗位要求明显冲突。",
                    "3": f"能够提供基本可核实的{evidence_target}。",
                    "5": f"在复杂约束下稳定呈现{evidence_target}，并能说明复盘与迁移。",
                },
                "evidence_requirements": ["候选人原话", "具体事实", "本人行动", "可核验结果"],
                "keywords": [],
                "question": str(item.get("question") or ""),
                "follow_up": str(item.get("follow_up") or ""),
                "source_excerpt": str(item.get("source_excerpt") or ""),
                "evaluation_source": "job_semantic_profile",
            }
        )
        known.add(dimension_id)
    return dimensions


def evaluation_scope_payload(
    interview: InterviewRound,
    dimensions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "round_type": interview.round_type,
        "round_label": ROUND_LABELS.get(interview.round_type, interview.round_type),
        "interviewer_names": list(interview.interviewer_names or []),
        "dimensions": [
            {
                "competency_id": str(item.get("id")),
                "competency_name": str(item.get("name")),
                "source": str(item.get("evaluation_source", "round_competency")),
            }
            for item in dimensions
        ],
        "transcript_scope": "本轮全部已确认角色的最终逐字稿",
        "planned_question_dependency": False,
        "policy": "评价维度按面试轮次区分；证据可来自预设题、临场问题或自然追问，不要求面试官照题库提问。",
    }

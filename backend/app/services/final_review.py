from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    AudioRecording,
    Candidate,
    EvidenceItem,
    InterviewerQualityReview,
    InterviewRound,
    Job,
    KnowledgeProposal,
    Scorecard,
    TranscriptSegment,
)
from .interviewer_quality import build_interviewer_metrics


ROUND_LABELS = {"business": "业务面", "hr": "HR 面", "ceo": "CEO 面", "custom": "补充面试"}
PRIMARY_ROUND_TYPES = {"business", "hr", "ceo"}


def build_final_review(db: Session, application: Application) -> dict[str, Any]:
    candidate = db.get(Candidate, application.candidate_id)
    job = db.get(Job, application.job_id)
    rounds = list(
        db.scalars(
            select(InterviewRound).where(InterviewRound.application_id == application.id)
        ).all()
    )
    rounds.sort(key=lambda item: (item.scheduled_at or item.created_at, item.created_at))

    round_payloads = []
    required_round_ids = {
        item.id
        for item in rounds
        if item.round_type in PRIMARY_ROUND_TYPES and item.status != "cancelled"
    }
    competency_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ai_round_summaries: list[dict[str, Any]] = []
    outstanding_questions = []
    pending_knowledge = 0
    completed_count = 0
    submitted_scorecards = 0

    for interview in rounds:
        scorecard = db.scalar(select(Scorecard).where(Scorecard.interview_round_id == interview.id))
        evidence = list(
            db.scalars(select(EvidenceItem).where(EvidenceItem.interview_round_id == interview.id)).all()
        )
        transcript_count = len(
            list(
                db.scalars(
                    select(TranscriptSegment).where(
                        TranscriptSegment.interview_round_id == interview.id,
                        TranscriptSegment.is_final.is_(True),
                    )
                ).all()
            )
        )
        recordings = list(
            db.scalars(
                select(AudioRecording)
                .where(AudioRecording.interview_round_id == interview.id)
                .order_by(AudioRecording.created_at.desc())
            ).all()
        )
        knowledge = list(
            db.scalars(
                select(KnowledgeProposal).where(KnowledgeProposal.source_round_id == interview.id)
            ).all()
        )
        pending_knowledge += sum(
            item.status in {"pending", "approved_for_publish"} for item in knowledge
        )
        if interview.id in required_round_ids and interview.status == "completed":
            completed_count += 1
        if interview.id in required_round_ids and scorecard and scorecard.status == "submitted":
            submitted_scorecards += 1

        human_decision = (scorecard.recommendation or {}).get("human_decision") if scorecard else None
        final_scores = scorecard.final_scores if scorecard else []
        ai_scores = scorecard.ai_scores if scorecard else []
        assessed_ai_scores = [
            item for item in ai_scores if item.get("score") is not None
        ]
        if assessed_ai_scores:
            recommendation = scorecard.recommendation or {}
            scope = recommendation.get("evaluation_scope", {})
            ai_round_summaries.append(
                {
                    "round_id": interview.id,
                    "round_type": interview.round_type,
                    "round_label": ROUND_LABELS.get(
                        interview.round_type, interview.round_type
                    ),
                    "interviewer_names": list(interview.interviewer_names or []),
                    "score": round(
                        sum(float(item["score"]) for item in assessed_ai_scores)
                        / len(assessed_ai_scores),
                        1,
                    ),
                    "assessed_dimensions": len(assessed_ai_scores),
                    "total_dimensions": len(ai_scores),
                    "evidence_count": len(
                        {
                            evidence_id
                            for item in assessed_ai_scores
                            for evidence_id in item.get("evidence_ids", [])
                        }
                    ),
                    "transcript_count": transcript_count,
                    "evaluation_scope": scope,
                    "assessment_basis": (
                        recommendation.get("ai_recommendation", {})
                    ).get("assessment_basis", "available_round_evidence"),
                }
            )
        for item in final_scores:
            if item.get("score") is None:
                continue
            competency_entries[item.get("competency_id", "unknown")].append({
                **item,
                "round_id": interview.id,
                "round_type": interview.round_type,
                "round_label": ROUND_LABELS.get(interview.round_type, interview.round_type),
            })

        if scorecard:
            outstanding_questions.extend([
                {
                    **item,
                    "source_round_id": interview.id,
                    "source_round_type": interview.round_type,
                    "source_round_label": ROUND_LABELS.get(interview.round_type, interview.round_type),
                }
                for item in scorecard.next_round_questions
            ])

        quality_review = db.scalar(
            select(InterviewerQualityReview).where(
                InterviewerQualityReview.interview_round_id == interview.id
            )
        )
        metrics = quality_review.automated_metrics if quality_review and quality_review.automated_metrics else build_interviewer_metrics(db, interview)
        round_payloads.append({
            "id": interview.id,
            "round_type": interview.round_type,
            "round_label": ROUND_LABELS.get(interview.round_type, interview.round_type),
            "status": interview.status,
            "scheduled_at": interview.scheduled_at,
            "interviewer_names": interview.interviewer_names,
            "meeting_source": interview.meeting_source,
            "transcript_count": transcript_count,
            "transcript_url": f"/api/v1/interviews/{interview.id}/transcript.txt",
            "recordings": [
                {
                    "id": item.id,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "download_url": f"/api/v1/recordings/{item.id}/download",
                }
                for item in recordings
            ],
            "evidence_summary": {
                "total": len(evidence),
                "confirmed": sum(item.human_status in {"confirmed", "modified"} for item in evidence),
                "pending": sum(item.human_status == "pending" for item in evidence),
                "rejected": sum(item.human_status == "rejected" for item in evidence),
            },
            "scorecard": None if not scorecard else {
                "status": scorecard.status,
                "rubric_version": scorecard.rubric_version,
                "ai_decision": (scorecard.recommendation or {}).get("decision"),
                "human_decision": human_decision,
                "question_evidence_summary": (scorecard.recommendation or {}).get("question_evidence_summary", {}),
                "jd_assessments": (scorecard.recommendation or {}).get("jd_assessments", []),
                "evaluation_scope": (scorecard.recommendation or {}).get("evaluation_scope", {}),
                "ai_recommendation": (scorecard.recommendation or {}).get("ai_recommendation", {}),
                "answer_logic_review": (scorecard.recommendation or {}).get("answer_logic_review", {}),
                "final_scores": final_scores,
                "next_round_questions": scorecard.next_round_questions,
            },
            "interviewer_quality": {
                "status": quality_review.status if quality_review else "ai_draft",
                "metrics": metrics,
            },
            "knowledge_proposals": {
                "total": len(knowledge),
                "pending": sum(
                    item.status in {"pending", "approved_for_publish"}
                    for item in knowledge
                ),
            },
        })

    missing_steps = []
    if not required_round_ids:
        missing_steps.append("尚未配置该岗位的面试流程")
    for item in round_payloads:
        if item["id"] in required_round_ids and item["status"] != "completed":
            missing_steps.append(f"{item['round_label']}尚未完成")
        elif item["id"] in required_round_ids and not item["scorecard"]:
            missing_steps.append(f"{item['round_label']}尚未生成评价")
        elif item["id"] in required_round_ids and item["scorecard"]["status"] != "submitted":
            missing_steps.append(f"{item['round_label']}尚未提交人工评价")

    competency_summary = []
    for competency_id, entries in competency_entries.items():
        competency_summary.append({
            "competency_id": competency_id,
            "competency_name": entries[0].get("competency_name", competency_id),
            "average_human_score": round(sum(item["score"] for item in entries) / len(entries), 1),
            "round_count": len(entries),
            "evidence_count": sum(len(item.get("evidence_ids", [])) for item in entries),
            "round_scores": entries,
        })

    ready = not missing_steps
    final_details = (application.screening_payload or {}).get("final_review")
    cross_round_ai_assessment = {
        "overall_score": (
            round(
                sum(item["score"] for item in ai_round_summaries)
                / len(ai_round_summaries),
                1,
            )
            if ai_round_summaries
            else None
        ),
        "rounds_assessed": len(ai_round_summaries),
        "total_transcript_segments": sum(
            item["transcript_count"] for item in ai_round_summaries
        ),
        "rounds": ai_round_summaries,
        "method": "先按岗位实际配置的各轮职责独立评分，再对有证据的轮次等权汇总。",
        "policy": "综合分使用所有轮次的真实问答，不要求面试官按预设题提问；仅作 HR 决策参考，不自动改变候选人阶段。",
    }
    return {
        "application_id": application.id,
        "candidate": {
            "id": candidate.id if candidate else None,
            "display_name": candidate.display_name if candidate else "候选人",
        },
        "job": {
            "id": job.id if job else None,
            "title": job.title if job else "目标岗位",
            "source_job_code": job.source_job_code if job else None,
        },
        "current_stage": application.current_stage,
        "human_final_decision": application.human_final_decision,
        "final_decision_details": final_details,
        "readiness": {
            "status": "ready_for_hr_decision" if ready else "not_ready",
            "rounds_total": len(required_round_ids),
            "configured_round_order": [
                item.round_type for item in rounds if item.id in required_round_ids
            ],
            "rounds_completed": completed_count,
            "scorecards_submitted": submitted_scorecards,
            "missing_steps": missing_steps,
            "open_question_count": len(outstanding_questions),
            "pending_knowledge_approvals": pending_knowledge,
            "policy": "完整度仅用于提示 HR 检查材料，不自动生成录用或淘汰结论。",
        },
        "rounds": round_payloads,
        "competency_summary": competency_summary,
        "cross_round_ai_assessment": cross_round_ai_assessment,
        "outstanding_questions": outstanding_questions,
    }

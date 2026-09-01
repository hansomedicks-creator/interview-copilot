from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    EvidenceItem,
    InterviewAssignment,
    InterviewerQualityReview,
    InterviewQuestionProgress,
    InterviewRound,
    Job,
    TranscriptSegment,
)
from .job_semantics import contains_non_job_factor


def build_interviewer_metrics(db: Session, interview: InterviewRound) -> dict[str, Any]:
    plan = interview.plan_payload or {}
    required = plan.get("required_questions") or [
        item for item in plan.get("questions", []) if item.get("required")
    ]
    progress = db.scalars(
        select(InterviewQuestionProgress).where(
            InterviewQuestionProgress.interview_round_id == interview.id,
            InterviewQuestionProgress.asked.is_(True),
        )
    ).all()
    asked_ids = {item.question_id for item in progress}
    segments = db.scalars(
        select(TranscriptSegment).where(
            TranscriptSegment.interview_round_id == interview.id,
            TranscriptSegment.is_final.is_(True),
        )
    ).all()
    evidence_count = len(
        db.scalars(
            select(EvidenceItem).where(EvidenceItem.interview_round_id == interview.id)
        ).all()
    )
    candidate_ms = sum(
        max(0, item.end_ms - item.start_ms)
        for item in segments
        if item.speaker_role == "candidate"
    )
    interviewer_ms = sum(
        max(0, item.end_ms - item.start_ms)
        for item in segments
        if item.speaker_role == "interviewer"
    )
    known_speech_ms = candidate_ms + interviewer_ms
    required_total = len(required)
    required_asked = sum(item.get("id") in asked_ids for item in required)
    candidate_share = round(candidate_ms / known_speech_ms, 2) if known_speech_ms else None
    candidate_segments = sum(item.speaker_role == "candidate" for item in segments)
    evidence_density = round(evidence_count / candidate_segments, 2) if candidate_segments else 0
    flags: list[dict[str, str]] = []
    if required_total and required_asked < required_total:
        flags.append({"code": "required_questions_missing", "message": "本轮存在未覆盖的统一必问题。"})
    if candidate_share is not None and candidate_share < 0.55:
        flags.append({"code": "low_candidate_talk_share", "message": "候选人有效表达占比较低，需检查面试官是否讲述过多。"})
    if candidate_segments and evidence_count == 0:
        flags.append({"code": "low_evidence_yield", "message": "已有候选人回答，但没有形成可复核证据。"})
    interviewer_text = " ".join(
        item.effective_text for item in segments if item.speaker_role == "interviewer"
    )
    fairness_risk = contains_non_job_factor(interviewer_text)
    if fairness_risk:
        flags.append({"code": "non_job_factor_question", "message": "面试官问题中出现了可能与岗位无关的个人背景因素，需要 HR 复核。"})
    required_coverage = round(required_asked / required_total, 2) if required_total else 1
    preparation_score = _bounded_score(1 + required_coverage * 4)
    if candidate_share is None:
        listening_score = 3
    elif 0.58 <= candidate_share <= 0.85:
        listening_score = 5
    elif candidate_share >= 0.5:
        listening_score = 4
    else:
        listening_score = 2
    question_score = _bounded_score(
        2.2 + min(evidence_density, 1.5) * 1.4 + required_coverage
    )
    fairness_score = 2 if fairness_risk else _bounded_score(3 + required_coverage * 2)
    ai_ratings = {
        "preparation": preparation_score,
        "question_quality": question_score,
        "listening": listening_score,
        "fairness": fairness_score,
    }
    return {
        "required_questions_total": required_total,
        "required_questions_asked": required_asked,
        "required_question_coverage": required_coverage,
        "candidate_talk_share": candidate_share,
        "candidate_segment_count": candidate_segments,
        "evidence_count": evidence_count,
        "evidence_density": evidence_density,
        "flags": flags,
        "ai_ratings": ai_ratings,
        "ai_overall_score": round(sum(ai_ratings.values()) / len(ai_ratings), 1),
        "ai_rating_basis": {
            "preparation": "统一问题覆盖与面试计划执行",
            "question_quality": "候选人回答转化为可复核证据的效率",
            "listening": "候选人与面试官的有效表达占比",
            "fairness": "问题一致性及是否涉及非岗位个人因素",
        },
        "interpretation": "质量信号用于复盘面试过程，不直接作为绩效结论。",
    }


def build_quality_overview(db: Session, job_id: str | None = None) -> dict[str, Any]:
    applications = list(
        db.scalars(
            select(Application).where(Application.job_id == job_id)
            if job_id
            else select(Application)
        ).all()
    )
    application_by_id = {item.id: item for item in applications}
    application_ids = list(application_by_id)
    rounds = list(
        db.scalars(
            select(InterviewRound).where(
                InterviewRound.application_id.in_(application_ids),
                InterviewRound.status == "completed",
            )
        ).all()
    ) if application_ids else []
    round_ids = [item.id for item in rounds]
    reviews = list(
        db.scalars(
            select(InterviewerQualityReview).where(
                InterviewerQualityReview.interview_round_id.in_(round_ids)
            )
        ).all()
    ) if round_ids else []
    review_by_round = {item.interview_round_id: item for item in reviews}
    assignments = list(
        db.scalars(
            select(InterviewAssignment).where(
                InterviewAssignment.interview_round_id.in_(round_ids)
            )
        ).all()
    ) if round_ids else []
    assignments_by_round: dict[str, list[InterviewAssignment]] = defaultdict(list)
    for assignment in assignments:
        assignments_by_round[assignment.interview_round_id].append(assignment)

    records = []
    for interview in rounds:
        application = application_by_id[interview.application_id]
        metrics = build_interviewer_metrics(db, interview)
        review = review_by_round.get(interview.id)
        assignees: list[Any] = assignments_by_round.get(interview.id) or [
            {"user_open_id": f"name:{name}", "display_name": name}
            for name in interview.interviewer_names
        ]
        records.append({
            "interview": interview,
            "application": application,
            "metrics": metrics,
            "review": review,
            "assignees": assignees,
        })

    by_interviewer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    interviewer_names: dict[str, str] = {}
    for record in records:
        for assignee in record["assignees"]:
            open_id = assignee.user_open_id if isinstance(assignee, InterviewAssignment) else assignee["user_open_id"]
            display_name = assignee.display_name if isinstance(assignee, InterviewAssignment) else assignee["display_name"]
            by_interviewer[open_id].append(record)
            interviewer_names[open_id] = display_name

    interviewer_rows = [
        _aggregate_interviewer(open_id, interviewer_names[open_id], items)
        for open_id, items in by_interviewer.items()
    ]
    interviewer_rows.sort(
        key=lambda item: (item["risk_level"] == "needs_attention", item["interview_count"]),
        reverse=True,
    )

    jobs = {item.job_id: db.get(Job, item.job_id) for item in applications}
    job_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for current_job_id, job in jobs.items():
        if not job or job.status == "demo":
            continue
        group_key = (
            job.title.strip().casefold(),
            (job.source_job_code or "").strip().casefold(),
        )
        job_groups[group_key].append(current_job_id)
    job_rows = []
    for grouped_job_ids in job_groups.values():
        grouped_jobs = [jobs[current_job_id] for current_job_id in grouped_job_ids]
        job = max(grouped_jobs, key=lambda item: item.created_at)
        grouped_job_id_set = set(grouped_job_ids)
        job_applications = [item for item in applications if item.job_id in grouped_job_id_set]
        job_records = [
            item for item in records
            if item["application"].job_id in grouped_job_id_set
        ]
        flagged = sum(bool(item["metrics"]["flags"]) for item in job_records)
        completed = len(job_records)
        offer_count = sum(item.human_final_decision == "offer_approval" for item in job_applications)
        if completed < 3:
            diagnosis = "样本不足，先继续积累至少 3 场已完成面试。"
            diagnosis_code = "insufficient_sample"
        elif flagged / completed >= 0.5:
            diagnosis = "流程异常信号偏多，建议先复盘统一问题覆盖、倾听和证据产出。"
            diagnosis_code = "interview_process"
        elif len(job_applications) >= 3 and offer_count == 0:
            diagnosis = "面试过程暂未出现集中异常，建议同步检查岗位标准与候选人供给。"
            diagnosis_code = "supply_or_standard"
        else:
            diagnosis = "现有面试过程指标较稳定，继续结合招聘结果观察。"
            diagnosis_code = "stable_observation"
        job_rows.append({
            "job_id": job.id,
            "job_title": job.title,
            "application_count": len(job_applications),
            "completed_interviews": completed,
            "reviewed_interviews": sum(item["review"] is not None for item in job_records),
            "offer_approval_count": offer_count,
            "flagged_interviews": flagged,
            "average_required_question_coverage": _average(
                [item["metrics"]["required_question_coverage"] for item in job_records]
            ),
            "diagnosis": diagnosis,
            "diagnosis_code": diagnosis_code,
        })
    job_rows.sort(key=lambda item: item["completed_interviews"], reverse=True)

    metrics = [item["metrics"] for item in records]
    talk_shares = [item["candidate_talk_share"] for item in metrics if item["candidate_talk_share"] is not None]
    return {
        "filters": {
            "job_id": job_id,
            "jobs": [{"id": item["job_id"], "title": item["job_title"]} for item in job_rows],
        },
        "summary": {
            "completed_interviews": len(records),
            "reviewed_interviews": len(reviews),
            "review_completion_rate": round(len(reviews) / len(records), 2) if records else None,
            "flagged_interviews": sum(bool(item["flags"]) for item in metrics),
            "average_required_question_coverage": _average(
                [item["required_question_coverage"] for item in metrics]
            ),
            "average_candidate_talk_share": _average(talk_shares),
            "interviewer_count": len(interviewer_rows),
        },
        "interviewers": interviewer_rows,
        "jobs": job_rows,
        "governance": {
            "minimum_sample": 3,
            "boundary": "面试质量指标用于定位流程问题和辅导机会，不单独作为绩效、晋升或淘汰依据。",
            "interpretation_order": ["先看样本量", "再看流程异常", "最后结合岗位招聘结果"],
        },
    }


def _aggregate_interviewer(
    open_id: str,
    display_name: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = [item["metrics"] for item in records]
    reviews = [item["review"] for item in records if item["review"] is not None]
    talk_shares = [item["candidate_talk_share"] for item in metrics if item["candidate_talk_share"] is not None]
    rating_values: dict[str, list[int]] = defaultdict(list)
    for metric in metrics:
        for key, value in (metric.get("ai_ratings") or {}).items():
            if isinstance(value, int):
                rating_values[key].append(value)
    rating_averages = {key: _average(values) for key, values in rating_values.items()}
    flag_counts: dict[str, int] = defaultdict(int)
    for metric in metrics:
        for flag in metric["flags"]:
            flag_counts[flag["code"]] += 1
    coaching = []
    coverage = _average([item["required_question_coverage"] for item in metrics])
    talk_share = _average(talk_shares)
    density = _average([item["evidence_density"] for item in metrics])
    if coverage is not None and coverage < 0.8:
        coaching.append("统一必问题覆盖不足，建议使用开场检查清单。")
    if talk_share is not None and talk_share < 0.55:
        coaching.append("候选人表达占比较低，建议减少面试官连续讲述并增加开放式追问。")
    if density is not None and density < 0.5:
        coaching.append("回答转化为证据的效率偏低，建议围绕情境、行动和结果继续追问。")
    if rating_averages.get("question_quality", 5) < 3.5:
        coaching.append("AI 复盘显示提问质量需要提升。")
    if rating_averages.get("fairness", 5) < 3.5:
        coaching.append("AI 复盘提示公平性风险，应复核问题一致性和证据标准。")
    if len(records) < 3:
        risk_level = "insufficient_sample"
    elif coaching:
        risk_level = "needs_attention"
    else:
        risk_level = "stable"
    round_distribution: dict[str, int] = defaultdict(int)
    for item in records:
        round_distribution[item["interview"].round_type] += 1
    return {
        "open_id": open_id,
        "display_name": display_name,
        "interview_count": len(records),
        "reviewed_count": len(reviews),
        "average_required_question_coverage": coverage,
        "average_candidate_talk_share": talk_share,
        "average_evidence_density": density,
        "ai_rating_averages": rating_averages,
        "human_rating_averages": {},
        "flag_counts": dict(flag_counts),
        "round_distribution": dict(round_distribution),
        "coaching_signals": coaching,
        "risk_level": risk_level,
        "sample_warning": "至少积累 3 场后再判断趋势。" if len(records) < 3 else None,
    }


def _average(values: list[float | int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _bounded_score(value: float) -> int:
    return max(1, min(5, round(value)))

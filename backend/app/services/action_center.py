from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    Candidate,
    InterviewAssignment,
    InterviewRound,
    Job,
    KnowledgeProposal,
    Scorecard,
    utc_now,
)


ROUND_LABELS = {
    "business": "业务面",
    "hr": "HR 面",
    "ceo": "CEO 面",
    "custom": "补充面试",
}
PRIMARY_ROUNDS = {"business", "hr", "ceo"}


def build_personal_action_center(db: Session, user: dict[str, Any]) -> dict[str, Any]:
    assigned_ids = set(
        db.scalars(
            select(InterviewAssignment.interview_round_id).where(
                InterviewAssignment.user_open_id == user["open_id"]
            )
        ).all()
    )
    if not assigned_ids:
        return _personal_payload([])

    today = utc_now().date()
    seven_days_later = today + timedelta(days=7)
    rounds = list(
        db.scalars(
            select(InterviewRound)
            .where(InterviewRound.id.in_(assigned_ids))
            .order_by(InterviewRound.scheduled_at, InterviewRound.created_at)
        ).all()
    )
    items: list[dict[str, Any]] = []
    for interview in rounds:
        if interview.status == "cancelled":
            continue
        application = db.get(Application, interview.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        job = db.get(Job, application.job_id) if application else None
        if not application or not candidate or not job or candidate.source == "demo":
            continue
        if user["role"] == "hr" and interview.round_type != "hr":
            continue
        scorecard = db.scalar(
            select(Scorecard).where(Scorecard.interview_round_id == interview.id)
        )
        base = _round_item(application, candidate, job, interview)
        if interview.status == "completed" and (
            not scorecard or scorecard.status not in {"submitted", "dismissed"}
        ):
            score_summary = _scorecard_summary(scorecard)
            items.append({
                **base,
                "type": "feedback_due",
                "priority": "high",
                "title": f"提交{base['round_label']}评价",
                "detail": (
                    f"AI 草稿：{score_summary['score']} · {score_summary['label']}。点击查看证据并复核。"
                    if score_summary
                    else "面试已经结束，AI 正在整理标准化评价草稿。"
                ),
                "ai_summary": score_summary,
                "action": "open_interview",
                "action_label": "完成评价",
            })
            continue
        scheduled_date = interview.scheduled_at.date() if interview.scheduled_at else None
        if interview.status in {"planned", "ready", "in_progress"} and scheduled_date == today:
            items.append({
                **base,
                "type": "today_interview",
                "priority": "high" if interview.status == "in_progress" else "normal",
                "title": f"今日{base['round_label']}",
                "detail": "进入工作台后会自动加载候选人、岗位、本轮题库和前轮待验证问题。",
                "action": "open_interview",
                "action_label": "进入面试",
            })
        elif (
            interview.status in {"planned", "ready"}
            and scheduled_date
            and today < scheduled_date <= seven_days_later
        ):
            items.append({
                **base,
                "type": "upcoming_interview",
                "priority": "normal",
                "title": f"即将进行{base['round_label']}",
                "detail": "建议提前查看简历、岗位重点和统一必问题。",
                "action": "open_interview",
                "action_label": "提前准备",
            })
    return _personal_payload(items)


def build_hr_action_center(db: Session) -> dict[str, Any]:
    applications = list(
        db.scalars(select(Application).order_by(Application.created_at.desc())).all()
    )
    all_rounds = list(db.scalars(select(InterviewRound)).all())
    rounds_by_application: dict[str, list[InterviewRound]] = defaultdict(list)
    for interview in all_rounds:
        rounds_by_application[interview.application_id].append(interview)

    scorecards = {
        item.interview_round_id: item
        for item in db.scalars(select(Scorecard)).all()
    }
    today = utc_now().date()
    items: list[dict[str, Any]] = []
    included_application_ids: set[str] = set()

    for application in applications:
        candidate = db.get(Candidate, application.candidate_id)
        job = db.get(Job, application.job_id)
        if not candidate or not job or candidate.source == "demo":
            continue
        included_application_ids.add(application.id)
        rounds = [item for item in rounds_by_application[application.id] if item.status != "cancelled"]
        expected = [item for item in rounds if item.round_type in PRIMARY_ROUNDS]
        base = {
            "application_id": application.id,
            "candidate": {"id": candidate.id, "display_name": candidate.display_name},
            "job": {"id": job.id, "title": job.title, "source_job_code": job.source_job_code},
        }
        if not expected and application.current_stage == "interview_to_schedule":
            items.append({
                **base,
                "id": f"schedule:{application.id}",
                "type": "unscheduled",
                "priority": "high",
                "title": "配置并安排面试流程",
                "detail": "候选人资料已经导入，请按岗位需要选择轮次、调整顺序并分配面试官。",
                "interview_id": None,
                "round_type": None,
                "round_label": None,
                "scheduled_at": None,
                "action": "schedule_application",
                "action_label": "立即排期",
            })

        for interview in expected:
            scorecard = scorecards.get(interview.id)
            round_base = _round_item(application, candidate, job, interview)
            if interview.status == "completed" and (
                not scorecard or scorecard.status not in {"submitted", "dismissed"}
            ):
                score_summary = _scorecard_summary(scorecard)
                items.append({
                    **round_base,
                    "type": "missing_scorecard",
                    "priority": "high",
                    "title": f"{round_base['round_label']}待补人工评价",
                    "detail": (
                        f"AI 草稿：{score_summary['score']} · {score_summary['label']}；尚待人工复核。"
                        if score_summary
                        else "本轮已结束，AI 评价草稿正在生成，尚待人工复核。"
                    ),
                    "ai_summary": score_summary,
                    "action": "open_interview",
                    "action_label": "查看本轮",
                })
            if (
                interview.status in {"planned", "ready", "in_progress"}
                and interview.scheduled_at
                and interview.scheduled_at.date() == today
            ):
                items.append({
                    **round_base,
                    "type": "today_interview",
                    "priority": "normal",
                    "title": f"今日{round_base['round_label']}",
                    "detail": f"面试官：{'、'.join(interview.interviewer_names) or '待分配'}。",
                    "action": "open_final_review",
                    "action_label": "查看安排",
                })

        if (
            bool(expected)
            and all(item.status == "completed" for item in expected)
            and all(
                scorecards.get(item.id) and scorecards[item.id].status == "submitted"
                for item in expected
            )
            and application.human_final_decision is None
        ):
            items.append({
                **base,
                "id": f"final:{application.id}",
                "type": "ready_for_decision",
                "priority": "urgent",
                "title": "岗位面试材料已齐，等待 HR 终审",
                "detail": f"该岗位配置的 {len(expected)} 轮面试和人工评价均已完成，请结合证据、分歧与风险作出流程决定。",
                "interview_id": None,
                "round_type": None,
                "round_label": None,
                "scheduled_at": None,
                "action": "open_final_review",
                "action_label": "进入终审",
            })

    pending_knowledge = 0
    for proposal in db.scalars(
        select(KnowledgeProposal).where(
            KnowledgeProposal.status.in_({"pending", "approved_for_publish"})
        )
    ).all():
        source_round = db.get(InterviewRound, proposal.source_round_id)
        if source_round and source_round.application_id in included_application_ids:
            pending_knowledge += 1
    if pending_knowledge:
        items.append({
            "id": "knowledge:pending",
            "type": "knowledge_approval",
            "priority": "normal",
            "title": f"{pending_knowledge} 条知识更新待处理",
            "detail": "AI 只生成脱敏提案，必须由 HR 审批后才能发布到正式知识库。",
            "application_id": None,
            "interview_id": None,
            "candidate": None,
            "job": None,
            "round_type": None,
            "round_label": None,
            "scheduled_at": None,
            "action": "open_knowledge",
            "action_label": "去审批",
        })

    order = {
        "ready_for_decision": 0,
        "missing_scorecard": 1,
        "unscheduled": 2,
        "today_interview": 3,
        "knowledge_approval": 4,
    }
    items.sort(key=lambda item: (order.get(item["type"], 99), item.get("scheduled_at") or ""))
    return {
        "summary": {
            "unscheduled": sum(item["type"] == "unscheduled" for item in items),
            "today_interviews": sum(item["type"] == "today_interview" for item in items),
            "missing_scorecards": sum(item["type"] == "missing_scorecard" for item in items),
            "ready_for_decision": sum(item["type"] == "ready_for_decision" for item in items),
            "knowledge_approvals": pending_knowledge,
            "total_actions": len(items),
        },
        "items": items,
        "boundary": "行动中心只负责聚合待办和打开正确页面，不自动提交评价、变更候选人阶段或发布知识。",
    }


def _personal_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    order = {"feedback_due": 0, "today_interview": 1, "upcoming_interview": 2}
    items.sort(key=lambda item: (order.get(item["type"], 99), item.get("scheduled_at") or ""))
    return {
        "summary": {
            "today_interviews": sum(item["type"] == "today_interview" for item in items),
            "feedback_due": sum(item["type"] == "feedback_due" for item in items),
            "upcoming_7_days": sum(item["type"] == "upcoming_interview" for item in items),
        },
        "items": items,
        "boundary": "只展示分配给当前账号的面试；评价和候选人阶段仍需人工确认。",
    }


def _round_item(
    application: Application,
    candidate: Candidate,
    job: Job,
    interview: InterviewRound,
) -> dict[str, Any]:
    return {
        "id": f"round:{interview.id}",
        "application_id": application.id,
        "interview_id": interview.id,
        "candidate": {"id": candidate.id, "display_name": candidate.display_name},
        "job": {"id": job.id, "title": job.title, "source_job_code": job.source_job_code},
        "round_type": interview.round_type,
        "round_label": ROUND_LABELS.get(interview.round_type, interview.round_type),
        "scheduled_at": interview.scheduled_at,
    }


def _scorecard_summary(scorecard: Scorecard | None) -> dict[str, Any] | None:
    if not scorecard:
        return None
    ai = (scorecard.recommendation or {}).get("ai_recommendation") or {}
    score = ai.get("overall_score")
    return {
        "score": f"{score} / 5" if score is not None else "暂不可评",
        "score_value": score,
        "decision": ai.get("decision"),
        "label": ai.get("label") or "等待人工复核",
        "status": scorecard.status,
    }

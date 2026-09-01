from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    Application,
    Candidate,
    InterviewAssignment,
    InterviewRound,
    Job,
    KnowledgeProposal,
    NotificationDispatch,
    Scorecard,
    UserIdentity,
    new_id,
    utc_now,
)
from ..providers.feishu_notifications import FeishuNotificationError, FeishuNotificationSender
from .data_governance import record_audit_event


ROUND_LABELS = {"business": "业务面", "hr": "HR 面", "ceo": "CEO 面", "custom": "补充面试"}


def sync_notification_queue(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _aware(now or utc_now())
    existing_dispatches = {
        item.event_key: item for item in db.scalars(select(NotificationDispatch)).all()
    }
    for item in existing_dispatches.values():
        if item.status == "queued":
            item.status = "cancelled"
    created: list[NotificationDispatch] = []
    identities = {
        item.open_id: item
        for item in db.scalars(select(UserIdentity).where(UserIdentity.active.is_(True))).all()
    }
    hr_recipients = [item for item in identities.values() if item.role in {"hr", "admin"}]

    rounds = list(
        db.scalars(
            select(InterviewRound).where(
                InterviewRound.status != "cancelled",
                InterviewRound.scheduled_at.is_not(None),
            )
        ).all()
    )
    application_ids = {item.application_id for item in rounds}
    applications = {
        item.id: item
        for item in db.scalars(select(Application).where(Application.id.in_(application_ids))).all()
    } if application_ids else {}
    candidates = {
        item.id: item
        for item in db.scalars(select(Candidate).where(Candidate.id.in_({app.candidate_id for app in applications.values()}))).all()
    } if applications else {}
    jobs = {
        item.id: item
        for item in db.scalars(select(Job).where(Job.id.in_({app.job_id for app in applications.values()}))).all()
    } if applications else {}
    assignments = list(
        db.scalars(
            select(InterviewAssignment).where(
                InterviewAssignment.interview_round_id.in_([item.id for item in rounds])
            )
        ).all()
    ) if rounds else []
    assignments_by_round: dict[str, list[InterviewAssignment]] = defaultdict(list)
    for item in assignments:
        assignments_by_round[item.interview_round_id].append(item)

    for interview in rounds:
        application = applications.get(interview.application_id)
        candidate = candidates.get(application.candidate_id) if application else None
        job = jobs.get(application.job_id) if application else None
        if not candidate or not job:
            continue
        scheduled_at = _aware(interview.scheduled_at)
        if scheduled_at >= now:
            for assignment in assignments_by_round[interview.id]:
                identity = identities.get(assignment.user_open_id)
                created_item = _queue(
                    db, existing_dispatches,
                    event_key=f"interview_assigned:{interview.id}:{assignment.user_open_id}",
                    notification_type="interview_assigned",
                    recipient_open_id=assignment.user_open_id,
                    recipient_display_name=assignment.display_name,
                    recipient_role=identity.role if identity else "interviewer",
                    title=f"你有一场新的{ROUND_LABELS.get(interview.round_type, interview.round_type)}安排",
                    message=f"候选人：{candidate.display_name}\n岗位：{job.title}\n时间：{_display_time(scheduled_at)}\n请在面试前查看简历、统一必问题和前轮已确认材料。",
                    action_path=f"/?interview={interview.id}",
                    resource_type="interview_round",
                    resource_id=interview.id,
                    scheduled_for=now,
                )
                if created_item:
                    created.append(created_item)
                reminder_at = max(now, scheduled_at - timedelta(hours=24))
                reminder = _queue(
                    db, existing_dispatches,
                    event_key=f"interview_reminder_24h:{interview.id}:{assignment.user_open_id}",
                    notification_type="interview_reminder",
                    recipient_open_id=assignment.user_open_id,
                    recipient_display_name=assignment.display_name,
                    recipient_role=identity.role if identity else "interviewer",
                    title=f"面试提醒：{ROUND_LABELS.get(interview.round_type, interview.round_type)}将在 24 小时内开始",
                    message=f"候选人：{candidate.display_name}\n岗位：{job.title}\n时间：{_display_time(scheduled_at)}\n请确认设备、会议方式和本轮重点问题。",
                    action_path=f"/?interview={interview.id}",
                    resource_type="interview_round",
                    resource_id=interview.id,
                    scheduled_for=reminder_at,
                )
                if reminder:
                    created.append(reminder)

        if interview.status == "completed":
            scorecard = db.scalar(select(Scorecard).where(Scorecard.interview_round_id == interview.id))
            if not scorecard or scorecard.status != "submitted":
                for assignment in assignments_by_round[interview.id]:
                    identity = identities.get(assignment.user_open_id)
                    feedback = _queue(
                        db, existing_dispatches,
                        event_key=f"feedback_due:{interview.id}:{assignment.user_open_id}",
                        notification_type="feedback_due",
                        recipient_open_id=assignment.user_open_id,
                        recipient_display_name=assignment.display_name,
                        recipient_role=identity.role if identity else "interviewer",
                        title="面试已结束，请补充人工评价",
                        message=f"候选人：{candidate.display_name}\n岗位：{job.title}\n请确认引用证据、完成能力评分并提交本轮人工结论。",
                        action_path=f"/?interview={interview.id}",
                        resource_type="interview_round",
                        resource_id=interview.id,
                        scheduled_for=now,
                    )
                    if feedback:
                        created.append(feedback)

    rounds_by_application: dict[str, list[InterviewRound]] = defaultdict(list)
    for item in rounds:
        rounds_by_application[item.application_id].append(item)
    for application_id, application_rounds in rounds_by_application.items():
        application = applications.get(application_id)
        if not application or application.human_final_decision:
            continue
        required = [
            item for item in application_rounds
            if item.round_type in {"business", "hr", "ceo"}
        ]
        if not required or any(item.status != "completed" for item in required):
            continue
        scorecards = {
            item.interview_round_id: item
            for item in db.scalars(
                select(Scorecard).where(Scorecard.interview_round_id.in_([item.id for item in required]))
            ).all()
        }
        if any(scorecards.get(item.id) is None or scorecards[item.id].status != "submitted" for item in required):
            continue
        candidate = candidates.get(application.candidate_id)
        job = jobs.get(application.job_id)
        if not candidate or not job:
            continue
        for recipient in hr_recipients:
            final_notice = _queue(
                db, existing_dispatches,
                event_key=f"final_review_ready:{application.id}:{recipient.open_id}",
                notification_type="final_review_ready",
                recipient_open_id=recipient.open_id,
                recipient_display_name=recipient.display_name,
                recipient_role=recipient.role,
                title="岗位面试材料已齐，请进行 HR 终审",
                message=f"候选人：{candidate.display_name}\n岗位：{job.title}\n该岗位配置的 {len(required)} 轮人工评价均已提交，请查看证据汇总后作出人工流程决定。",
                action_path=f"/?final_review={application.id}",
                resource_type="application",
                resource_id=application.id,
                scheduled_for=now,
            )
            if final_notice:
                created.append(final_notice)

    proposals = db.scalars(
        select(KnowledgeProposal).where(KnowledgeProposal.status.in_({"pending", "approved_for_publish"}))
    ).all()
    for proposal in proposals:
        for recipient in hr_recipients:
            knowledge_notice = _queue(
                db, existing_dispatches,
                event_key=f"knowledge_approval:{proposal.id}:{recipient.open_id}",
                notification_type="knowledge_approval",
                recipient_open_id=recipient.open_id,
                recipient_display_name=recipient.display_name,
                recipient_role=recipient.role,
                title="人才知识库有新的待审批提案",
                message="AI 已根据人工确认后的面试材料形成脱敏知识提案。请审核内容后决定批准发布或驳回，系统不会自动写入正式知识库。",
                action_path="/?knowledge=1",
                resource_type="knowledge_proposal",
                resource_id=proposal.id,
                scheduled_for=now,
            )
            if knowledge_notice:
                created.append(knowledge_notice)

    db.flush()
    return {"created": len(created), "queue": build_notification_center(db, settings, now=now)}


def build_notification_center(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _aware(now or utc_now())
    items = list(
        db.scalars(
            select(NotificationDispatch)
            .where(NotificationDispatch.status != "cancelled")
            .order_by(NotificationDispatch.scheduled_for, NotificationDispatch.created_at.desc())
            .limit(200)
        ).all()
    )
    payloads = [notification_payload(item, now) for item in items]
    return {
        "generated_at": now,
        "integration": {
            "provider": "feishu_im",
            "status": "ready" if settings.feishu_notifications_configured else "not_configured",
            "sending_enabled": settings.feishu_notifications_configured,
            "server_side_secret": True,
            "required_permission": "im:message:send_as_bot",
            "automatic_sending": False,
        },
        "summary": {
            "total": len(payloads),
            "due": sum(item["status"] in {"queued", "failed"} and item["is_due"] for item in payloads),
            "scheduled": sum(item["status"] == "queued" and not item["is_due"] for item in payloads),
            "sent": sum(item["status"] == "sent" for item in payloads),
            "failed": sum(item["status"] == "failed" for item in payloads),
        },
        "items": payloads,
    }


def dispatch_notifications(
    db: Session,
    settings: Settings,
    sender: FeishuNotificationSender,
    user: dict[str, Any],
    notification_ids: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _aware(now or utc_now())
    if not settings.feishu_notifications_configured or not sender.ready:
        raise ValueError("Feishu notification sending is not configured on the server")
    items = list(
        db.scalars(select(NotificationDispatch).where(NotificationDispatch.id.in_(notification_ids))).all()
    )
    found_ids = {item.id for item in items}
    if missing := [item for item in notification_ids if item not in found_ids]:
        raise ValueError(f"notification not found: {missing[0]}")
    sent = 0
    failed = 0
    skipped = 0
    for item in items:
        if item.status == "sent" or _aware(item.scheduled_for) > now:
            skipped += 1
            continue
        item.attempts += 1
        item.last_attempted_at = now
        action_url = settings.public_base_url.rstrip("/") + "/" + item.action_path.lstrip("/")
        try:
            item.provider_message_id = sender.send_text(
                recipient_open_id=item.recipient_open_id,
                title=item.title,
                message=item.message,
                action_url=action_url,
            )
            item.status = "sent"
            item.sent_at = now
            item.error_message = None
            sent += 1
        except FeishuNotificationError as error:
            item.status = "failed"
            item.error_message = str(error)[:1000]
            failed += 1
    record_audit_event(
        db,
        user,
        action="notification.dispatch_executed",
        resource_type="notification_batch",
        resource_id=new_id("notify"),
        details={"requested": len(notification_ids), "sent": sent, "failed": failed, "skipped": skipped},
    )
    db.flush()
    return {"status": "completed", "sent": sent, "failed": failed, "skipped": skipped}


def notification_payload(item: NotificationDispatch, now: datetime) -> dict[str, Any]:
    return {
        "id": item.id,
        "notification_type": item.notification_type,
        "recipient": {
            "open_id": item.recipient_open_id,
            "display_name": item.recipient_display_name,
            "role": item.recipient_role,
        },
        "title": item.title,
        "message": item.message,
        "action_path": item.action_path,
        "resource_type": item.resource_type,
        "resource_id": item.resource_id,
        "status": item.status,
        "scheduled_for": item.scheduled_for,
        "is_due": _aware(item.scheduled_for) <= now,
        "attempts": item.attempts,
        "error_message": item.error_message,
        "sent_at": item.sent_at,
        "created_at": item.created_at,
    }


def _queue(
    db: Session,
    existing_dispatches: dict[str, NotificationDispatch],
    *,
    event_key: str,
    notification_type: str,
    recipient_open_id: str,
    recipient_display_name: str,
    recipient_role: str,
    title: str,
    message: str,
    action_path: str,
    resource_type: str,
    resource_id: str,
    scheduled_for: datetime,
) -> NotificationDispatch | None:
    if existing := existing_dispatches.get(event_key):
        if existing.status in {"queued", "failed", "cancelled"}:
            was_failed = existing.status == "failed"
            existing.notification_type = notification_type
            existing.recipient_open_id = recipient_open_id
            existing.recipient_display_name = recipient_display_name
            existing.recipient_role = recipient_role
            existing.title = title
            existing.message = message
            existing.action_path = action_path
            existing.resource_type = resource_type
            existing.resource_id = resource_id
            existing.scheduled_for = scheduled_for
            existing.status = "failed" if was_failed else "queued"
            if not was_failed:
                existing.error_message = None
        return None
    item = NotificationDispatch(
        id=new_id("notice"),
        event_key=event_key,
        notification_type=notification_type,
        recipient_open_id=recipient_open_id,
        recipient_display_name=recipient_display_name,
        recipient_role=recipient_role,
        title=title,
        message=message,
        action_path=action_path,
        resource_type=resource_type,
        resource_id=resource_id,
        scheduled_for=scheduled_for,
    )
    db.add(item)
    existing_dispatches[event_key] = item
    return item


def _display_time(value: datetime) -> str:
    return value.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

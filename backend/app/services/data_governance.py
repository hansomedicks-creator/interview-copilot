from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    Application,
    AudioRecording,
    AuditEvent,
    Candidate,
    EvidenceItem,
    InterviewQuestionProgress,
    InterviewReportVersion,
    InterviewRound,
    Job,
    Scorecard,
    TranscriptSegment,
    new_id,
    utc_now,
)


def record_audit_event(
    db: Session,
    user: dict[str, Any],
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        id=new_id("audit"),
        actor_open_id=str(user.get("open_id") or "unknown"),
        actor_display_name=str(user.get("display_name") or "未知用户"),
        actor_role=str(user.get("role") or "unknown"),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
    )
    db.add(event)
    return event


def audit_event_payload(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "actor": {
            "open_id": event.actor_open_id,
            "display_name": event.actor_display_name,
            "role": event.actor_role,
        },
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "details": event.details or {},
        "created_at": event.created_at,
    }


def build_governance_center(
    db: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _aware(now or utc_now())
    candidates = list(
        db.scalars(
            select(Candidate)
            .where(Candidate.source != "demo")
            .order_by(Candidate.retention_until, Candidate.created_at)
        ).all()
    )
    cleaned_candidate_ids = set(
        db.scalars(
            select(AuditEvent.resource_id).where(
                AuditEvent.action == "retention.sensitive_artifacts_cleaned",
                AuditEvent.resource_type == "candidate",
            )
        ).all()
    )
    items = [_candidate_governance_item(db, candidate, now, cleaned_candidate_ids) for candidate in candidates]
    due_items = [item for item in items if item["status"] in {"expired", "recording_due"}]
    expiring_items = [item for item in items if item["status"] == "expiring_soon"]
    audit_events = db.scalars(
        select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(50)
    ).all()
    return {
        "generated_at": now,
        "policy": {
            "default_retention_days": settings.retention_days,
            "maximum_retention_days": settings.max_retention_days,
            "expiring_soon_days": 30,
            "automatic_cleanup_enabled": False,
            "cleanup_requires_hr_confirmation": True,
            "deleted_scope": ["录音文件", "逐字稿", "逐字稿证据引用", "报告中的原话和下载入口"],
            "preserved_scope": ["能力评分", "各轮人工评价", "最终流程决定", "脱敏知识成果", "审计记录"],
        },
        "summary": {
            "candidate_count": len(items),
            "cleanup_due": len(due_items),
            "expired": sum(item["status"] == "expired" for item in items),
            "recording_due": sum(item["status"] == "recording_due" for item in items),
            "expiring_soon": len(expiring_items),
            "cleaned": sum(item["status"] == "cleaned" for item in items),
            "sensitive_bytes_due": sum(item["expired_recording_bytes"] for item in due_items),
        },
        "items": due_items + expiring_items + [
            item for item in items if item["status"] not in {"expired", "recording_due", "expiring_soon"}
        ],
        "audit_events": [audit_event_payload(item) for item in audit_events],
    }


def execute_retention_cleanup(
    db: Session,
    settings: Settings,
    user: dict[str, Any],
    candidate_ids: list[str],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = _aware(now or utc_now())
    candidates = list(
        db.scalars(select(Candidate).where(Candidate.id.in_(candidate_ids))).all()
    )
    found_ids = {item.id for item in candidates}
    missing_ids = [item for item in candidate_ids if item not in found_ids]
    if missing_ids:
        raise ValueError(f"candidate not found: {missing_ids[0]}")

    totals = {
        "candidates_processed": 0,
        "recordings_deleted": 0,
        "recording_bytes_deleted": 0,
        "transcript_segments_deleted": 0,
        "evidence_items_deleted": 0,
        "scorecards_redacted": 0,
        "reports_redacted": 0,
        "file_delete_failures": 0,
    }
    candidate_results: list[dict[str, Any]] = []
    for candidate in candidates:
        result = _cleanup_candidate(db, settings, candidate, user, now)
        candidate_results.append(result)
        if result["status"] == "cleaned":
            totals["candidates_processed"] += 1
        for key in totals:
            if key != "candidates_processed" and key in result:
                totals[key] += result[key]

    if not totals["candidates_processed"]:
        raise ValueError("selected candidates do not have expired sensitive materials")
    record_audit_event(
        db,
        user,
        action="retention.cleanup_executed",
        resource_type="retention_batch",
        resource_id=new_id("cleanup"),
        details={
            **totals,
            "candidate_ids": [item["candidate_id"] for item in candidate_results if item["status"] == "cleaned"],
            "policy_time": now.isoformat(),
        },
    )
    return {"status": "completed", "summary": totals, "candidates": candidate_results}


def _candidate_governance_item(
    db: Session,
    candidate: Candidate,
    now: datetime,
    cleaned_candidate_ids: set[str],
) -> dict[str, Any]:
    applications = list(db.scalars(select(Application).where(Application.candidate_id == candidate.id)).all())
    application_ids = [item.id for item in applications]
    rounds = list(
        db.scalars(select(InterviewRound).where(InterviewRound.application_id.in_(application_ids))).all()
    ) if application_ids else []
    round_ids = [item.id for item in rounds]
    recordings = list(
        db.scalars(select(AudioRecording).where(AudioRecording.interview_round_id.in_(round_ids))).all()
    ) if round_ids else []
    transcript_count = len(list(
        db.scalars(select(TranscriptSegment.id).where(TranscriptSegment.interview_round_id.in_(round_ids))).all()
    )) if round_ids else 0
    evidence_count = len(list(
        db.scalars(select(EvidenceItem.id).where(EvidenceItem.interview_round_id.in_(round_ids))).all()
    )) if round_ids else 0
    candidate_expired = _aware(candidate.retention_until) <= now
    expired_recordings = [item for item in recordings if _aware(item.retention_until) <= now]
    has_sensitive_artifacts = bool(recordings or transcript_count or evidence_count)
    if candidate_expired and (has_sensitive_artifacts or candidate.id not in cleaned_candidate_ids):
        status = "expired"
    elif expired_recordings:
        status = "recording_due"
    elif candidate_expired:
        status = "cleaned"
    elif _aware(candidate.retention_until) <= now + timedelta(days=30):
        status = "expiring_soon"
    else:
        status = "active"
    jobs = {item.id: item for item in db.scalars(select(Job).where(Job.id.in_([app.job_id for app in applications]))).all()} if applications else {}
    return {
        "candidate_id": candidate.id,
        "candidate_name": candidate.display_name,
        "source": candidate.source,
        "job_titles": sorted({jobs[app.job_id].title for app in applications if app.job_id in jobs}),
        "retention_until": candidate.retention_until,
        "status": status,
        "candidate_package_expired": candidate_expired,
        "application_count": len(applications),
        "round_count": len(rounds),
        "recording_count": len(recordings),
        "expired_recording_count": len(expired_recordings),
        "expired_recording_bytes": sum(item.byte_count for item in expired_recordings),
        "transcript_segment_count": transcript_count,
        "evidence_count": evidence_count,
        "can_cleanup": status in {"expired", "recording_due"},
    }


def _cleanup_candidate(
    db: Session,
    settings: Settings,
    candidate: Candidate,
    user: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    applications = list(db.scalars(select(Application).where(Application.candidate_id == candidate.id)).all())
    application_ids = [item.id for item in applications]
    rounds = list(
        db.scalars(select(InterviewRound).where(InterviewRound.application_id.in_(application_ids))).all()
    ) if application_ids else []
    round_ids = [item.id for item in rounds]
    candidate_expired = _aware(candidate.retention_until) <= now
    recordings = list(
        db.scalars(select(AudioRecording).where(AudioRecording.interview_round_id.in_(round_ids))).all()
    ) if round_ids else []
    recordings_to_delete = [
        item for item in recordings if candidate_expired or _aware(item.retention_until) <= now
    ]
    if not candidate_expired and not recordings_to_delete:
        return {"candidate_id": candidate.id, "status": "not_due"}

    result = {
        "candidate_id": candidate.id,
        "status": "cleaned",
        "scope": "candidate_package" if candidate_expired else "expired_recordings_only",
        "recordings_deleted": 0,
        "recording_bytes_deleted": 0,
        "transcript_segments_deleted": 0,
        "evidence_items_deleted": 0,
        "scorecards_redacted": 0,
        "reports_redacted": 0,
        "file_delete_failures": 0,
    }
    recording_root = settings.recording_dir.resolve()
    for recording in recordings_to_delete:
        recording_path = (recording_root / PurePath(recording.storage_key)).resolve()
        if not recording_path.is_relative_to(recording_root):
            result["file_delete_failures"] += 1
            continue
        try:
            if recording_path.exists():
                recording_path.unlink()
        except OSError:
            result["file_delete_failures"] += 1
            continue
        result["recordings_deleted"] += 1
        result["recording_bytes_deleted"] += recording.byte_count
        db.delete(recording)

    if candidate_expired:
        segments = list(
            db.scalars(select(TranscriptSegment).where(TranscriptSegment.interview_round_id.in_(round_ids))).all()
        ) if round_ids else []
        evidence = list(
            db.scalars(select(EvidenceItem).where(EvidenceItem.interview_round_id.in_(round_ids))).all()
        ) if round_ids else []
        for item in segments:
            db.delete(item)
        for item in evidence:
            db.delete(item)
        result["transcript_segments_deleted"] = len(segments)
        result["evidence_items_deleted"] = len(evidence)

        progresses = list(
            db.scalars(select(InterviewQuestionProgress).where(InterviewQuestionProgress.interview_round_id.in_(round_ids))).all()
        ) if round_ids else []
        for item in progresses:
            item.evidence_segment_ids = []

        scorecards = list(
            db.scalars(select(Scorecard).where(Scorecard.interview_round_id.in_(round_ids))).all()
        ) if round_ids else []
        for scorecard in scorecards:
            scorecard.ai_scores = _redact_evidence_references(scorecard.ai_scores)
            scorecard.human_scores = _redact_evidence_references(scorecard.human_scores)
            scorecard.final_scores = _redact_evidence_references(scorecard.final_scores)
            recommendation = _redact_evidence_references(scorecard.recommendation)
            recommendation["sensitive_artifact_retention"] = {
                "status": "expired",
                "redacted_at": now.isoformat(),
            }
            scorecard.recommendation = recommendation
            scorecard.next_round_questions = _redact_evidence_references(scorecard.next_round_questions)
            result["scorecards_redacted"] += 1

        reports = list(
            db.scalars(select(InterviewReportVersion).where(InterviewReportVersion.application_id.in_(application_ids))).all()
        ) if application_ids else []
        for report in reports:
            snapshot = deepcopy(report.snapshot_payload or {})
            management = snapshot.setdefault("management", {})
            management["key_evidence"] = []
            appendix = snapshot.setdefault("hr_appendix", {})
            for artifact in appendix.get("artifacts") or []:
                artifact["transcript_url"] = None
                artifact["recordings"] = []
                artifact["retention_status"] = "expired"
            governance = snapshot.setdefault("governance", {})
            governance["sensitive_artifact_retention"] = {
                "status": "expired_and_redacted",
                "redacted_at": now.isoformat(),
                "original_content_hash": report.content_hash,
                "preserved": ["评分", "人工评价", "最终流程决定"],
            }
            report.snapshot_payload = snapshot
            report.content_hash = _content_hash(snapshot)
            result["reports_redacted"] += 1

        record_audit_event(
            db,
            user,
            action="retention.sensitive_artifacts_cleaned",
            resource_type="candidate",
            resource_id=candidate.id,
            details={key: value for key, value in result.items() if key not in {"candidate_id", "status"}},
        )
    else:
        record_audit_event(
            db,
            user,
            action="retention.recordings_cleaned",
            resource_type="candidate",
            resource_id=candidate.id,
            details={
                "recordings_deleted": result["recordings_deleted"],
                "recording_bytes_deleted": result["recording_bytes_deleted"],
                "file_delete_failures": result["file_delete_failures"],
            },
        )
    return result


def _redact_evidence_references(value: Any) -> Any:
    if isinstance(value, list):
        return [_redact_evidence_references(item) for item in value]
    if not isinstance(value, dict):
        return value
    output: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"evidence_ids", "confirmed_evidence_ids", "evidence_segment_ids", "segment_ids"}:
            output[key] = []
        elif key in {"quote", "text_raw", "text_corrected"}:
            output[key] = None
        else:
            output[key] = _redact_evidence_references(item)
    return output


def _content_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

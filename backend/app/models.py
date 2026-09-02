from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_candidate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str] = mapped_column(String(128))
    resume_text: Mapped[str] = mapped_column(Text, default="")
    resume_asset_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), unique=True, index=True)
    structured_data: Mapped[dict] = mapped_column(JSON, default=dict)
    recognition_version: Mapped[str] = mapped_column(String(64), default="resume-rules-v0.1")
    source_import_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verified_by_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_job_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(128))
    jd_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="draft")
    competency_model_version: Mapped[str] = mapped_column(String(32), default="generic-v0.1")
    competencies: Mapped[list[dict]] = mapped_column(JSON, default=list)
    semantic_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    current_stage: Mapped[str] = mapped_column(String(64), default="interview")
    screening_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    human_final_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    archived_by_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    archived_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ResumeImportBatch(Base):
    __tablename__ = "resume_import_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="uploading")
    created_by_open_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResumeImportItem(Base):
    __tablename__ = "resume_import_items"
    __table_args__ = (UniqueConstraint("batch_id", "content_hash", name="uq_batch_resume_hash"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("resume_import_batches.id"), index=True)
    filename: Mapped[str] = mapped_column(String(256))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    recognized_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="recognized")
    duplicate_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InterviewRound(Base):
    __tablename__ = "interview_rounds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    round_type: Mapped[str] = mapped_column(String(32))
    interview_mode: Mapped[str] = mapped_column(String(32), default="structured")
    interviewer_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meeting_source: Mapped[str] = mapped_column(String(32), default="offline")
    external_meeting_ids: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="planned")
    notice_status: Mapped[str] = mapped_column(String(32), default="pending")
    notice_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notice_acknowledged_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plan_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    suggestion_history: Mapped[list[dict]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class UserIdentity(Base):
    __tablename__ = "user_identities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_source: Mapped[str] = mapped_column(String(32), default="feishu")
    open_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="interviewer")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InterviewAssignment(Base):
    __tablename__ = "interview_assignments"
    __table_args__ = (
        UniqueConstraint("interview_round_id", "user_open_id", name="uq_round_assignee"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    user_open_id: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    assigned_by_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InterviewQuestionProgress(Base):
    __tablename__ = "interview_question_progress"
    __table_args__ = (
        UniqueConstraint("interview_round_id", "question_id", name="uq_round_question"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    question_id: Mapped[str] = mapped_column(String(128))
    asked: Mapped[bool] = mapped_column(Boolean, default=True)
    asked_by: Mapped[str] = mapped_column(String(128))
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    evidence_segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class InterviewerQualityReview(Base):
    __tablename__ = "interviewer_quality_reviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), unique=True, index=True
    )
    interviewer_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    automated_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    human_ratings: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ai_draft")
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    speaker_role: Mapped[str] = mapped_column(String(32))
    speaker_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    provider_speaker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text_raw: Mapped[str] = mapped_column(Text)
    text_corrected: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)
    corrected_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def effective_text(self) -> str:
        return self.text_corrected or self.text_raw


class SpeakerRoleMapping(Base):
    __tablename__ = "speaker_role_mappings"
    __table_args__ = (
        UniqueConstraint("interview_round_id", "provider_speaker_id", name="uq_round_provider_speaker"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    provider_speaker_id: Mapped[int] = mapped_column(Integer)
    speaker_role: Mapped[str] = mapped_column(String(32), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="semantic_auto")
    interviewer_score: Mapped[float] = mapped_column(Float, default=0.0)
    candidate_score: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AudioRecording(Base):
    __tablename__ = "audio_recordings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    storage_key: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(64), default="audio/wav")
    sample_rate: Mapped[int] = mapped_column(Integer, default=16000)
    channels: Mapped[int] = mapped_column(Integer, default=1)
    sample_width_bytes: Mapped[int] = mapped_column(Integer, default=2)
    byte_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    peak_level: Mapped[float] = mapped_column(Float, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="recording")
    pipeline_backend: Mapped[str] = mapped_column(String(64), default="native-frame-bridge")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), index=True
    )
    competency_id: Mapped[str] = mapped_column(String(64), index=True)
    segment_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    quote: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(16))
    strength: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(String(64), default="mock-rules-v0.1")
    human_status: Mapped[str] = mapped_column(String(16), default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Scorecard(Base):
    __tablename__ = "scorecards"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    interview_round_id: Mapped[str] = mapped_column(
        ForeignKey("interview_rounds.id"), unique=True, index=True
    )
    rubric_version: Mapped[str] = mapped_column(String(32), default="five-level-v0.1")
    ai_scores: Mapped[list[dict]] = mapped_column(JSON, default=list)
    human_scores: Mapped[list[dict]] = mapped_column(JSON, default=list)
    final_scores: Mapped[list[dict]] = mapped_column(JSON, default=list)
    recommendation: Mapped[dict] = mapped_column(JSON, default=dict)
    next_round_questions: Mapped[list[dict]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="ai_draft")
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InterviewReportVersion(Base):
    __tablename__ = "interview_report_versions"
    __table_args__ = (
        UniqueConstraint("application_id", "version_number", name="uq_application_report_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    version_label: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    snapshot_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_open_id: Mapped[str] = mapped_column(String(128), index=True)
    actor_display_name: Mapped[str] = mapped_column(String(128))
    actor_role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(96), index=True)
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str] = mapped_column(String(128), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class NotificationDispatch(Base):
    __tablename__ = "notification_dispatches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    notification_type: Mapped[str] = mapped_column(String(64), index=True)
    recipient_open_id: Mapped[str] = mapped_column(String(128), index=True)
    recipient_display_name: Mapped[str] = mapped_column(String(128))
    recipient_role: Mapped[str] = mapped_column(String(32), default="interviewer")
    title: Mapped[str] = mapped_column(String(256))
    message: Mapped[str] = mapped_column(Text)
    action_path: Mapped[str] = mapped_column(Text, default="/")
    resource_type: Mapped[str] = mapped_column(String(64), index=True)
    resource_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class KnowledgeProposal(Base):
    __tablename__ = "knowledge_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_round_id: Mapped[str] = mapped_column(ForeignKey("interview_rounds.id"), index=True)
    proposal_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    rationale: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KnowledgePublication(Base):
    __tablename__ = "knowledge_publications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_proposals.id"), unique=True, index=True
    )
    release_version: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    obsidian_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str] = mapped_column(String(128))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TalentProfileVersion(Base):
    __tablename__ = "talent_profile_versions"
    __table_args__ = (
        UniqueConstraint("job_id", "version_number", name="uq_job_profile_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    version_label: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    source_mode: Mapped[str] = mapped_column(String(32), default="jd_baseline")
    profile_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    change_summary: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_status: Mapped[str] = mapped_column(String(32), default="not_published")
    release_version: Mapped[str | None] = mapped_column(String(96), nullable=True)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    obsidian_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CompanyProfileVersion(Base):
    __tablename__ = "company_profile_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(128))
    version_number: Mapped[int] = mapped_column(Integer, unique=True)
    version_label: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    source_mode: Mapped[str] = mapped_column(String(32), default="hr_manual")
    profile_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    change_summary: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publication_status: Mapped[str] = mapped_column(String(32), default="not_published")
    release_version: Mapped[str | None] = mapped_column(String(96), nullable=True)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    obsidian_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    publication_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HistoricalSampleImportBatch(Base):
    __tablename__ = "historical_sample_import_batches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    source: Mapped[str] = mapped_column(String(32), default="beisen_export")
    filename: Mapped[str] = mapped_column(String(256))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    imported_rows: Mapped[int] = mapped_column(Integer, default=0)
    skipped_duplicates: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HistoricalHiringSample(Base):
    __tablename__ = "historical_hiring_samples"
    __table_args__ = (
        UniqueConstraint("job_id", "record_hash", name="uq_job_historical_sample_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("historical_sample_import_batches.id"), index=True
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    record_hash: Mapped[str] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    competency_signals: Mapped[list[dict]] = mapped_column(JSON, default=list)
    quality_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_quality: Mapped[str] = mapped_column(String(32), default="historical_reviewed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

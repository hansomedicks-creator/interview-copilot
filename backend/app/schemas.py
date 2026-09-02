from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CandidateCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    resume_text: str = ""
    source: str = "manual"
    source_candidate_id: str | None = None
    resume_asset_url: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=180)


class CandidateRead(ORMModel):
    id: str
    display_name: str
    source: str
    source_candidate_id: str | None
    retention_until: datetime
    created_at: datetime


class CompetencyDefinition(BaseModel):
    id: str
    name: str
    description: str
    keywords: list[str] = Field(default_factory=list)


class JobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    jd_text: str = ""
    source_job_code: str | None = None
    competencies: list[CompetencyDefinition] = Field(default_factory=list)


class JobRead(ORMModel):
    id: str
    title: str
    jd_text: str
    status: str
    competency_model_version: str
    competencies: list[dict[str, Any]]
    semantic_profile: dict[str, Any] = Field(default_factory=dict)


class AdminJobSave(BaseModel):
    title: str = Field(min_length=2, max_length=128)
    jd_text: str = Field(min_length=20, max_length=100_000)
    source_job_code: str | None = Field(default=None, max_length=128)
    status: Literal["active", "paused", "closed"] = "active"

    @model_validator(mode="after")
    def clean_job_definition(self) -> "AdminJobSave":
        self.title = self.title.strip()
        self.jd_text = self.jd_text.strip()
        self.source_job_code = self.source_job_code.strip() if self.source_job_code else None
        if len(self.jd_text) < 20:
            raise ValueError("job description must contain at least 20 characters")
        return self


class ApplicationCreate(BaseModel):
    candidate_id: str
    job_id: str
    current_stage: str = "interview"
    screening_payload: dict[str, Any] = Field(default_factory=dict)


class ApplicationRead(ORMModel):
    id: str
    candidate_id: str
    job_id: str
    current_stage: str
    screening_payload: dict[str, Any]
    human_final_decision: str | None
    archived_at: datetime | None = None
    archived_by_open_id: str | None = None
    archived_reason: str | None = None


class ApplicationFinalDecision(BaseModel):
    decision: Literal["offer_approval", "supplementary_interview", "hold", "reject"]
    decided_by: str = Field(min_length=1, max_length=128)
    notes: str = Field(min_length=5, max_length=2000)
    confirmed_by_hr: bool


class InterviewRoundCreate(BaseModel):
    application_id: str
    round_type: Literal["hr", "business", "ceo", "custom"]
    interview_mode: Literal["structured", "conversation"] = "structured"
    interviewer_names: list[str] = Field(default_factory=list)
    scheduled_at: datetime | None = None
    meeting_source: Literal["offline", "feishu"] = "offline"


class InterviewRoundRead(ORMModel):
    id: str
    application_id: str
    round_type: str
    interview_mode: str
    interviewer_names: list[str]
    scheduled_at: datetime | None
    meeting_source: str
    status: str
    notice_status: str
    plan_version: str | None
    plan_payload: dict[str, Any]
    suggestion_history: list[dict[str, Any]] = Field(default_factory=list)
    started_at: datetime | None
    ended_at: datetime | None


class InterviewTaskRoundCreate(BaseModel):
    round_type: Literal["business", "hr", "ceo"]
    interview_mode: Literal["structured", "conversation"] = "structured"
    interviewer_names: list[str] = Field(min_length=1, max_length=10)
    interviewer_open_ids: list[str] = Field(default_factory=list, max_length=10)
    scheduled_at: datetime
    meeting_source: Literal["offline", "feishu"] = "offline"

    @model_validator(mode="after")
    def interviewer_names_are_not_blank(self) -> "InterviewTaskRoundCreate":
        self.interviewer_names = [name.strip() for name in self.interviewer_names if name.strip()]
        if not self.interviewer_names:
            raise ValueError("at least one interviewer name is required")
        return self


class InterviewTaskCreate(BaseModel):
    application_id: str | None = None
    job_id: str | None = None
    candidate_name: str = Field(min_length=1, max_length=128)
    resume_text: str = Field(min_length=1)
    job_title: str = Field(min_length=1, max_length=128)
    jd_text: str = Field(min_length=1)
    source_job_code: str | None = Field(default=None, max_length=128)
    screening_payload: dict[str, Any] = Field(default_factory=dict)
    retention_days: int = Field(default=120, ge=1, le=180)
    rounds: list[InterviewTaskRoundCreate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_round_sequence(self) -> "InterviewTaskCreate":
        round_types = [item.round_type for item in self.rounds]
        if len(round_types) != len(set(round_types)):
            raise ValueError("each interview round type can only appear once")
        times = [item.scheduled_at for item in self.rounds]
        if any(later <= earlier for earlier, later in zip(times, times[1:])):
            raise ValueError("round schedules must follow the configured order and be strictly increasing")
        return self


class ResumeImportBatchCreate(BaseModel):
    job_id: str | None = None
    job_title: str | None = Field(default=None, max_length=128)
    jd_text: str = ""
    source_job_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def job_is_identifiable(self) -> "ResumeImportBatchCreate":
        if not self.job_id and not (self.job_title or "").strip():
            raise ValueError("select an existing job or provide a new job title")
        return self


class ResumeImportItemUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=256)
    years_experience: int | None = Field(default=None, ge=0, le=80)
    highest_education: str | None = Field(default=None, max_length=32)
    current_company: str | None = Field(default=None, max_length=128)
    current_title: str | None = Field(default=None, max_length=128)
    location: str | None = Field(default=None, max_length=64)


class ResumeImportCommit(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=200)
    retention_days: int = Field(default=120, ge=1, le=180)


class InterviewRoundManageUpdate(BaseModel):
    scheduled_at: datetime | None = None
    interviewer_open_ids: list[str] | None = Field(default=None, max_length=10)
    interviewer_names: list[str] | None = Field(default=None, max_length=10)
    meeting_source: Literal["offline", "feishu"] | None = None
    interview_mode: Literal["structured", "conversation"] | None = None

    @model_validator(mode="after")
    def assignment_fields_match(self) -> "InterviewRoundManageUpdate":
        if self.interviewer_open_ids is not None:
            if not self.interviewer_open_ids:
                raise ValueError("at least one interviewer is required")
            if self.interviewer_names is None or len(self.interviewer_names) != len(self.interviewer_open_ids):
                raise ValueError("interviewer names and open ids must align")
        return self


class NoticeAcknowledge(BaseModel):
    acknowledged_by: str = Field(min_length=1, max_length=128)
    candidate_was_notified: bool


class QuestionProgressUpdate(BaseModel):
    question_id: str = Field(min_length=1, max_length=128)
    asked: bool = True
    asked_by: str = Field(min_length=1, max_length=128)
    evidence_segment_ids: list[str] = Field(default_factory=list)


class SuggestionStatusUpdate(BaseModel):
    status: Literal["active", "addressed", "skipped"]


class InterviewerReviewSubmit(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=128)
    ratings: dict[Literal["preparation", "question_quality", "listening", "fairness"], int] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def valid_rating_range(self) -> "InterviewerReviewSubmit":
        required = {"preparation", "question_quality", "listening", "fairness"}
        if self.ratings and (set(self.ratings) != required or any(not 1 <= value <= 5 for value in self.ratings.values())):
            raise ValueError("all four interviewer ratings must be integers from 1 to 5")
        return self


class TranscriptSegmentCreate(BaseModel):
    speaker_role: Literal["candidate", "interviewer", "unknown"]
    speaker_confidence: float = Field(default=1.0, ge=0, le=1)
    provider_speaker_id: int | None = Field(default=None, ge=0, le=9)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str = Field(min_length=1)
    is_final: bool = True

    @model_validator(mode="after")
    def end_not_before_start(self) -> "TranscriptSegmentCreate":
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class TranscriptSegmentRead(ORMModel):
    id: str
    interview_round_id: str
    speaker_role: str
    speaker_confidence: float
    provider_speaker_id: int | None
    start_ms: int
    end_ms: int
    text_raw: str
    text_corrected: str | None
    is_final: bool


class SpeakerRoleMappingUpdate(BaseModel):
    speaker_role: Literal["candidate", "interviewer"]


class SpeakerRoleMappingRead(BaseModel):
    provider_speaker_id: int
    speaker_label: str
    speaker_role: Literal["candidate", "interviewer", "unknown"]
    confidence: float
    source: str
    sample_count: int
    sample_text: str | None = None
    confirmed_by: str | None = None


class EvidenceRead(ORMModel):
    id: str
    interview_round_id: str
    competency_id: str
    segment_ids: list[str]
    quote: str
    direction: str
    strength: float
    explanation: str
    human_status: str


class EvidenceReview(BaseModel):
    status: Literal["confirmed", "modified", "rejected"]
    reviewed_by: str = Field(min_length=1, max_length=128)
    explanation: str | None = None


class ScorecardRead(ORMModel):
    id: str
    interview_round_id: str
    rubric_version: str
    ai_scores: list[dict[str, Any]]
    human_scores: list[dict[str, Any]]
    final_scores: list[dict[str, Any]]
    recommendation: dict[str, Any]
    next_round_questions: list[dict[str, Any]]
    status: str


class ScorecardHumanScore(BaseModel):
    competency_id: str = Field(min_length=1, max_length=64)
    score: int = Field(ge=1, le=5)
    evidence_ids: list[str] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class ScorecardSubmit(BaseModel):
    submitted_by: str = Field(min_length=1, max_length=128)
    decision: Literal["advance", "hold", "reject", "supplementary_interview"]
    summary_notes: str | None = Field(default=None, max_length=2000)
    scores: list[ScorecardHumanScore] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_competencies(self) -> "ScorecardSubmit":
        ids = [item.competency_id for item in self.scores]
        if len(ids) != len(set(ids)):
            raise ValueError("each competency can only be scored once")
        return self


class ScorecardDismiss(BaseModel):
    dismissed_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class InterviewReportLock(BaseModel):
    confirmed_by_hr: bool


class RetentionCleanupRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=100)
    confirmed_by_hr: bool

    @model_validator(mode="after")
    def unique_candidates(self) -> "RetentionCleanupRequest":
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate_ids must be unique")
        return self


class NotificationDispatchRequest(BaseModel):
    notification_ids: list[str] = Field(min_length=1, max_length=100)
    confirmed_by_hr: bool

    @model_validator(mode="after")
    def unique_notifications(self) -> "NotificationDispatchRequest":
        if len(self.notification_ids) != len(set(self.notification_ids)):
            raise ValueError("notification_ids must be unique")
        return self


class SystemDocsSyncRequest(BaseModel):
    confirmed_by_hr: bool


class KnowledgeProposalCreate(BaseModel):
    source_round_id: str
    proposal_type: Literal["competency", "question", "follow_up_rule", "profile"]
    payload: dict[str, Any]
    rationale: str = Field(min_length=1)


class KnowledgeProposalReview(BaseModel):
    decision: Literal["approved", "rejected"]
    reviewed_by: str = Field(min_length=1, max_length=128)


class TalentProfileActivate(BaseModel):
    # Kept for backwards compatibility with the original activation endpoint.
    # The new HR flow uses an explicit "保存并生效" action instead of a review step.
    confirmed_by_hr: bool = True


class JobStatusUpdate(BaseModel):
    status: Literal["active", "paused", "closed"]


class TalentProfileDraftSave(BaseModel):
    profile_payload: dict[str, Any] = Field(min_length=1)
    change_summary: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def clean_talent_profile_draft(self) -> "TalentProfileDraftSave":
        self.change_summary = self.change_summary.strip()
        if not self.change_summary:
            raise ValueError("change_summary cannot be blank")
        return self


class CompanyProfileCompetencyInput(BaseModel):
    competency_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=2, max_length=64)
    definition: str = Field(min_length=5, max_length=500)
    positive_evidence: list[str] = Field(min_length=1, max_length=8)
    risk_signals: list[str] = Field(min_length=1, max_length=8)
    required_question: str = Field(min_length=5, max_length=500)
    follow_up: str = Field(min_length=5, max_length=500)
    primary_round: Literal["business", "hr", "ceo"]
    keywords: list[str] = Field(default_factory=list, max_length=20)
    score_anchors: dict[Literal["1", "3", "5"], str]

    @model_validator(mode="after")
    def clean_company_competency(self) -> "CompanyProfileCompetencyInput":
        self.positive_evidence = [item.strip() for item in self.positive_evidence if item.strip()]
        self.risk_signals = [item.strip() for item in self.risk_signals if item.strip()]
        self.keywords = list(dict.fromkeys(item.strip() for item in self.keywords if item.strip()))
        if not self.positive_evidence or not self.risk_signals:
            raise ValueError("positive evidence and risk signals cannot be blank")
        if set(self.score_anchors) != {"1", "3", "5"}:
            raise ValueError("score anchors 1, 3 and 5 are required")
        if any(len(value.strip()) < 3 for value in self.score_anchors.values()):
            raise ValueError("score anchors cannot be blank")
        return self


class CompanyProfileDraftSave(BaseModel):
    company_name: str = Field(min_length=2, max_length=128)
    profile_purpose: str = Field(min_length=10, max_length=1000)
    competencies: list[CompanyProfileCompetencyInput] = Field(min_length=3, max_length=8)
    red_lines: list[str] = Field(default_factory=list, max_length=10)
    change_summary: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def validate_company_profile(self) -> "CompanyProfileDraftSave":
        names = [item.name.strip().lower() for item in self.competencies]
        if len(names) != len(set(names)):
            raise ValueError("company competency names must be unique")
        self.red_lines = list(dict.fromkeys(item.strip() for item in self.red_lines if item.strip()))
        return self


class CompanyProfileActivate(BaseModel):
    confirmed_by_hr: bool


class HistoricalCompetencySignal(BaseModel):
    competency_id: str = Field(min_length=1, max_length=64)
    competency_name: str = Field(min_length=1, max_length=128)
    direction: Literal["positive", "negative", "mentioned"]
    confidence: float = Field(ge=0, le=1)
    source: Literal["structured_column", "evaluation_text"]


class HistoricalSampleCommitItem(BaseModel):
    row_number: int = Field(ge=2)
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal[
        "offer_approval", "hired", "probation_passed", "probation_failed", "rejected", "unknown"
    ]
    competency_signals: list[HistoricalCompetencySignal] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)


class HistoricalSampleCommit(BaseModel):
    job_id: str
    filename: str = Field(min_length=1, max_length=256)
    file_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_rows: int = Field(ge=1, le=500)
    samples: list[HistoricalSampleCommitItem] = Field(min_length=1, max_length=500)

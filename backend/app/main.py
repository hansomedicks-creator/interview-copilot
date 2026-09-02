from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from io import BytesIO
import json
from pathlib import Path, PurePath
import secrets
from typing import Any
from urllib.parse import quote, unquote, urlencode
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .audio import AudioFrameBridge, AudioRecordingSession, InvalidAudioChunk, pipecat_available
from .asr import ASREvent, ASRProviderError, asr_capability, create_asr_session
from .database import Database
from .models import (
    Application,
    AudioRecording,
    Candidate,
    CandidateProfile,
    CompanyProfileVersion,
    EvidenceItem,
    HistoricalHiringSample,
    HistoricalSampleImportBatch,
    InterviewerQualityReview,
    InterviewAssignment,
    InterviewReportVersion,
    InterviewQuestionProgress,
    InterviewRound,
    Job,
    KnowledgePublication,
    KnowledgeProposal,
    ResumeImportBatch,
    ResumeImportItem,
    Scorecard,
    SpeakerRoleMapping,
    TalentProfileVersion,
    TranscriptSegment,
    UserIdentity,
    new_id,
    utc_now,
)
from .providers import create_intelligence_provider
from .providers.feishu_notifications import FeishuNotificationSender
from .schemas import (
    AdminJobSave,
    ApplicationCreate,
    ApplicationFinalDecision,
    ApplicationRead,
    CandidateCreate,
    CandidateRead,
    CompanyProfileActivate,
    CompanyProfileDraftSave,
    EvidenceRead,
    EvidenceReview,
    HistoricalSampleCommit,
    InterviewRoundCreate,
    InterviewRoundRead,
    InterviewTaskCreate,
    InterviewerReviewSubmit,
    InterviewReportLock,
    InterviewRoundManageUpdate,
    JobCreate,
    JobRead,
    KnowledgeProposalCreate,
    KnowledgeProposalReview,
    NoticeAcknowledge,
    NotificationDispatchRequest,
    QuestionProgressUpdate,
    ResumeImportBatchCreate,
    ResumeImportCommit,
    ResumeImportItemUpdate,
    RetentionCleanupRequest,
    ScorecardRead,
    ScorecardDismiss,
    ScorecardSubmit,
    SpeakerRoleMappingRead,
    SpeakerRoleMappingUpdate,
    SuggestionStatusUpdate,
    SystemDocsSyncRequest,
    TalentProfileActivate,
    TranscriptSegmentCreate,
    TranscriptSegmentRead,
)
from .services.planning import build_plan, prior_round_context
from .services.job_semantics import build_local_job_semantic_profile
from .services.action_center import build_hr_action_center, build_personal_action_center
from .services.interviewer_quality import build_interviewer_metrics, build_quality_overview
from .services.knowledge_learning import generate_interview_knowledge_proposals
from .services.knowledge_vault import KnowledgePublishError, inspect_vault, publish_proposal
from .services.resume_recognition import recognize_resume
from .services.final_review import build_final_review
from .services.reporting import (
    build_or_refresh_report_draft,
    list_report_versions,
    lock_report,
    render_report_html,
    report_metadata,
    report_payload,
)
from .services.historical_import import HistoricalImportError, preview_historical_export
from .services.data_governance import (
    build_governance_center,
    execute_retention_cleanup,
    record_audit_event,
)
from .services.notifications import (
    build_notification_center,
    dispatch_notifications,
    sync_notification_queue,
)
from .services.system_docs import build_system_docs_status, sync_system_docs
from .services.readiness import build_readiness_center, run_readiness_test
from .services.suggestion_history import (
    merge_suggestion_history,
    update_suggestion_status,
)
from .services.evaluation_scope import round_evaluation_dimensions
from .services.evidence_presentation import build_evidence_digest, empty_evidence_digest
from .services.utterance_quality import is_usable_evidence_record
from .services.talent_profile import (
    build_or_refresh_profile_draft,
    collect_outcome_summary,
    maybe_refresh_outcome_profile_draft,
    profile_payload_for_publication,
    version_payload,
)
from .services.company_profile import (
    build_company_profile_center,
    company_profile_payload_for_publication,
    company_profile_version_payload,
    save_company_profile_draft,
)
from .services.speaker_roles import (
    confirm_speaker_role,
    observe_speaker,
    speaker_mapping_payloads,
)


STATIC_DIR = Path(__file__).parent / "static"


def _extract_document(content: bytes, filename: str) -> str:
    if not content:
        raise HTTPException(422, "document is empty")
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "document exceeds 10 MB")
    suffix = PurePath(filename).suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            text = content.decode("utf-8-sig")
        elif suffix == ".docx":
            with ZipFile(BytesIO(content)) as archive:
                document_xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(document_xml)
            paragraphs = []
            for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                runs = [node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
                if runs:
                    paragraphs.append("".join(runs))
            text = "\n".join(paragraphs)
        elif suffix == ".pdf":
            reader = PdfReader(BytesIO(content))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            raise HTTPException(415, "supported formats: .pdf, .docx, .txt, .md")
    except (BadZipFile, KeyError, ElementTree.ParseError, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(422, f"document could not be parsed: {error}") from error
    text = text.strip()
    if not text:
        raise HTTPException(422, "no readable text found; scanned PDFs require OCR")
    return text


def create_app(database_url: str | None = None, settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    app_settings.validate()
    database = Database(database_url or app_settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.create_all()
        if app_settings.environment != "production":
            with database.session_factory() as db:
                _seed_development_users(db)
                _backfill_legacy_assignments(db)
        yield

    app = FastAPI(
        title="Interview Copilot API",
        version="0.1.0",
        description="Evidence-first companion for human-led interviews",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.settings = app_settings
    app.state.intelligence = create_intelligence_provider(app_settings)
    app.state.notification_sender = FeishuNotificationSender(app_settings)
    app.state.audio_bridge_factory = lambda: AudioFrameBridge(
        sample_rate=app_settings.audio_sample_rate,
        channels=app_settings.audio_channels,
    )

    public_api_paths = {
        "/api/v1/health",
        "/api/v1/capabilities",
        "/api/v1/auth/status",
        "/api/v1/auth/feishu/login",
        "/api/v1/auth/feishu/callback",
        "/api/v1/auth/dev-login",
    }

    @app.middleware("http")
    async def authenticated_api(request: Request, call_next):
        if app_settings.environment == "test" or not request.url.path.startswith("/api/v1") or request.url.path in public_api_paths:
            if app_settings.environment == "test":
                request.state.user = {
                    "open_id": "dev-hr",
                    "display_name": "开发环境 HR",
                    "role": "hr",
                    "identity_source": "test",
                }
            return await call_next(request)
        session = _decode_signed_payload(request.cookies.get("interview_session"), app_settings.session_secret)
        if not session:
            return _json_error(401, "feishu login required")
        with database.session_factory() as db:
            identity = db.scalar(
                select(UserIdentity).where(
                    UserIdentity.open_id == session.get("open_id"),
                    UserIdentity.active.is_(True),
                )
            )
            if not identity:
                return _json_error(401, "user identity is inactive or unknown")
            request.state.user = _user_payload(identity)
            if identity.role == "interviewer" and request.url.path.startswith("/api/v1/interviews/"):
                path_parts = request.url.path.split("/")
                interview_id = path_parts[4] if len(path_parts) > 4 else ""
                assigned = db.scalar(
                    select(InterviewAssignment).where(
                        InterviewAssignment.interview_round_id == interview_id,
                        InterviewAssignment.user_open_id == identity.open_id,
                    )
                )
                if not assigned:
                    return _json_error(403, "this interview is not assigned to the current user")
        return await call_next(request)
    app.state.asr_session_factory = lambda event_handler: create_asr_session(
        app_settings, event_handler
    )

    def get_db(request: Request):
        yield from request.app.state.database.session()

    @app.get("/api/v1/health")
    def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": app.version,
            "provider_mode": request.app.state.settings.provider_mode,
            "production_providers_configured": request.app.state.settings.llm_configured,
        }

    @app.get("/api/v1/capabilities")
    def capabilities(request: Request) -> dict[str, Any]:
        vault_status = inspect_vault(
            request.app.state.settings.resolved_knowledge_vault_dir,
            request.app.state.settings.knowledge_vault_name,
        )
        return {
            "realtime_transport": "websocket_transcript_and_pcm_audio",
            "audio_input": {
                "status": "ready",
                "format": "pcm_s16le",
                "sample_rate": request.app.state.settings.audio_sample_rate,
                "channels": request.app.state.settings.audio_channels,
                "recording": "wav_local_development",
                "explicit_user_action_required": True,
            },
            "pipecat": {
                "enabled": request.app.state.settings.pipecat_enabled,
                "installed": pipecat_available(),
                "frame_type": "InputAudioRawFrame",
            },
            "asr": asr_capability(request.app.state.settings),
            "llm": {
                "status": (
                    "ready"
                    if request.app.state.settings.provider_mode == "production"
                    and request.app.state.settings.llm_configured
                    else "not_configured"
                    if request.app.state.settings.provider_mode == "production"
                    else "mock_rules"
                ),
                "mode": request.app.state.settings.provider_mode,
                "provider": request.app.state.intelligence.name,
                "model": (
                    request.app.state.settings.llm_model
                    if request.app.state.settings.llm_configured
                    else None
                ),
                "server_side_secret": True,
                "automatic_fallback": True,
                "automatic_hiring_decision": False,
            },
            "knowledge": {
                "status": "ready" if vault_status.writable else "not_configured",
                "approval_required": True,
                "provider": "obsidian_markdown",
            },
            "feishu": {
                "status": "ready" if request.app.state.settings.feishu_oauth_configured else "not_configured",
                "login": "oauth_2.0",
                "identity_key": "open_id",
                "notifications": {
                    "status": "ready" if request.app.state.settings.feishu_notifications_configured else "not_configured",
                    "automatic_sending": False,
                    "server_side_secret": True,
                },
            },
            "beisen": {"status": "adapter_boundary_only"},
            "automatic_hiring_decision": False,
            "automatic_stage_change": False,
        }

    @app.get("/api/v1/admin/readiness")
    def pilot_readiness(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        return build_readiness_center(db, request.app.state.settings, user)

    @app.post("/api/v1/admin/readiness/checks/{check_id}/test")
    def test_pilot_readiness_check(
        check_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "admin")
        try:
            result = run_readiness_test(
                db,
                request.app.state.settings,
                user,
                check_id,
                request.app.state.notification_sender,
            )
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        db.commit()
        return {
            "result": result,
            "readiness": build_readiness_center(
                db,
                request.app.state.settings,
                user,
            ),
        }

    @app.get("/api/v1/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        session = _decode_signed_payload(
            request.cookies.get("interview_session"), app_settings.session_secret
        )
        user = None
        if session:
            with database.session_factory() as db:
                identity = db.scalar(
                    select(UserIdentity).where(
                        UserIdentity.open_id == session.get("open_id"),
                        UserIdentity.active.is_(True),
                    )
                )
                if identity:
                    user = _user_payload(identity)
        return {
            "authenticated": bool(user),
            "user": user,
            "feishu_configured": app_settings.feishu_oauth_configured,
            "development_login_available": app_settings.environment != "production",
            "login_url": "/api/v1/auth/feishu/login",
        }

    @app.get("/api/v1/auth/feishu/login")
    def feishu_login() -> RedirectResponse:
        if not app_settings.feishu_oauth_configured:
            raise HTTPException(503, "Feishu OAuth is not configured yet")
        state_value = _encode_signed_payload(
            {"nonce": secrets.token_urlsafe(20), "exp": int((utc_now() + timedelta(minutes=10)).timestamp())},
            app_settings.session_secret,
        )
        query = urlencode(
            {
                "client_id": app_settings.feishu_app_id,
                "redirect_uri": app_settings.feishu_redirect_uri,
                "response_type": "code",
                "state": state_value,
                "scope": app_settings.feishu_oauth_scopes,
            }
        )
        response = RedirectResponse(
            f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{query}"
        )
        response.set_cookie(
            "feishu_oauth_state",
            state_value,
            httponly=True,
            secure=app_settings.environment == "production",
            samesite="lax",
            max_age=600,
        )
        return response

    @app.get("/api/v1/auth/feishu/callback")
    async def feishu_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        db: Session = Depends(get_db),
    ) -> RedirectResponse:
        if error:
            return RedirectResponse("/?auth_error=access_denied")
        cookie_state = request.cookies.get("feishu_oauth_state")
        if not code or not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
            raise HTTPException(400, "invalid OAuth state")
        if not _decode_signed_payload(state, app_settings.session_secret):
            raise HTTPException(400, "expired OAuth state")
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": app_settings.feishu_app_id,
                    "client_secret": app_settings.feishu_app_secret,
                    "code": code,
                    "redirect_uri": app_settings.feishu_redirect_uri,
                },
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            if token_data.get("code", 0) != 0 or not token_data.get("access_token"):
                raise HTTPException(502, token_data.get("msg", "Feishu token exchange failed"))
            user_response = await client.get(
                "https://open.feishu.cn/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
            )
            user_response.raise_for_status()
            user_envelope = user_response.json()
            if user_envelope.get("code", 0) != 0:
                raise HTTPException(502, user_envelope.get("msg", "Feishu user lookup failed"))
            user_data = user_envelope.get("data", user_envelope)
        identity = _upsert_feishu_user(db, user_data, app_settings)
        response = RedirectResponse("/?auth=success")
        _set_session_cookie(response, identity, app_settings)
        response.delete_cookie("feishu_oauth_state")
        return response

    @app.post("/api/v1/auth/dev-login")
    def development_login(payload: dict[str, str], db: Session = Depends(get_db)) -> JSONResponse:
        if app_settings.environment == "production":
            raise HTTPException(404, "not found")
        identity = db.scalar(
            select(UserIdentity).where(UserIdentity.open_id == payload.get("open_id"))
        )
        if not identity or not identity.active:
            raise HTTPException(404, "development identity not found")
        response = JSONResponse({"user": _user_payload(identity)})
        _set_session_cookie(response, identity, app_settings)
        return response

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie("interview_session")
        return response

    @app.get("/api/v1/me")
    def current_user(request: Request) -> dict[str, Any]:
        return request.state.user

    @app.get("/api/v1/me/interviews/today")
    def today_interviews(
        request: Request,
        include_demo: bool = False,
        days: int = 0,
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        user = request.state.user
        days = max(0, min(days, 30))
        today = utc_now().date()
        last_day = today + timedelta(days=days)
        assigned_ids = set(
            db.scalars(
                select(InterviewAssignment.interview_round_id).where(
                    InterviewAssignment.user_open_id == user["open_id"]
                )
            ).all()
        )
        rows = db.scalars(select(InterviewRound).order_by(InterviewRound.scheduled_at)).all()
        output = []
        for interview in rows:
            if days:
                if not interview.scheduled_at or not today <= interview.scheduled_at.date() <= last_day:
                    continue
            elif interview.scheduled_at and interview.scheduled_at.date() != today:
                continue
            application = db.get(Application, interview.application_id)
            if not application:
                continue
            candidate = db.get(Candidate, application.candidate_id)
            job = db.get(Job, application.job_id)
            if not candidate or not job:
                continue
            if candidate.source == "demo" and not include_demo:
                continue
            if user["role"] == "hr" and interview.round_type != "hr" and not (include_demo and candidate.source == "demo"):
                continue
            # "My interviews" is assignment-scoped for every role. HR/admin can
            # manage all rounds from the recruiting admin workspace instead.
            if interview.id not in assigned_ids and not (include_demo and candidate.source == "demo"):
                continue
            prior_round_count = len(
                db.scalars(
                    select(InterviewRound).where(
                        InterviewRound.application_id == application.id,
                        InterviewRound.id != interview.id,
                        InterviewRound.status == "completed",
                    )
                ).all()
            )
            management_report = db.scalar(
                select(InterviewReportVersion)
                .where(
                    InterviewReportVersion.application_id == application.id,
                    InterviewReportVersion.status == "locked",
                )
                .order_by(InterviewReportVersion.version_number.desc())
            )
            output.append(
                {
                    "interview_id": interview.id,
                    "candidate": {"id": candidate.id, "display_name": candidate.display_name},
                    "job": {"id": job.id, "title": job.title, "source_job_code": job.source_job_code},
                    "round_type": interview.round_type,
                    "interview_mode": interview.interview_mode,
                    "scheduled_at": interview.scheduled_at,
                    "meeting_source": interview.meeting_source,
                    "status": interview.status,
                    "interviewer_names": interview.interviewer_names,
                    "candidate_dossier": {
                        "prior_round_count": prior_round_count,
                        "management_report": (
                            {
                                "id": management_report.id,
                                "version_label": management_report.version_label,
                                "locked_at": management_report.locked_at,
                                "view_path": f"/?report={management_report.id}",
                            }
                            if management_report
                            else None
                        ),
                    },
                    "routing": {
                        "application_id": application.id,
                        "competency_model_version": job.competency_model_version,
                        "question_bank_version": (interview.plan_payload or {}).get("question_bank_version"),
                        "rule": "schedule -> application -> job -> round_type -> versioned plan",
                    },
                }
            )
        return output

    @app.get("/api/v1/me/action-center")
    def personal_action_center(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        return build_personal_action_center(db, request.state.user)

    @app.post("/api/v1/document-text")
    async def extract_document_text(request: Request) -> dict[str, Any]:
        content = await request.body()
        filename = unquote(request.headers.get("x-filename", "document.txt"))
        text = _extract_document(content, filename)
        return {"filename": filename, "text": text, "character_count": len(text)}

    @app.get("/api/v1/admin/jobs")
    def list_jobs(request: Request, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
        _require_role(request, "hr", "admin")
        jobs = db.scalars(select(Job).where(Job.status != "demo").order_by(Job.created_at.desc())).all()
        seen: set[tuple[str, str | None]] = set()
        output = []
        for job in jobs:
            key = (job.title.strip().lower(), job.source_job_code)
            if key in seen:
                continue
            seen.add(key)
            output.append(_admin_job_payload(db, job))
        return output

    @app.get("/api/v1/admin/jobs/{job_id}")
    def get_admin_job(
        job_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job or job.status == "demo":
            raise HTTPException(404, "job not found")
        return _admin_job_payload(db, job)

    @app.post("/api/v1/admin/jobs", status_code=201)
    def create_admin_job(
        payload: AdminJobSave,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        conflict = _find_job_conflict(
            db,
            title=payload.title,
            source_job_code=payload.source_job_code,
        )
        if conflict:
            raise HTTPException(409, "同名岗位或岗位编号已存在，请打开已有岗位更新 JD")
        job = Job(
            id=new_id("job"),
            title=payload.title,
            source_job_code=payload.source_job_code,
            jd_text=payload.jd_text,
            status=payload.status,
            competencies=[],
            semantic_profile=request.app.state.intelligence.analyze_job_definition(
                payload.title, payload.jd_text
            ),
        )
        db.add(job)
        db.flush()
        draft, changed = build_or_refresh_profile_draft(
            db,
            job=job,
            created_by=user["display_name"],
        )
        record_audit_event(
            db,
            user,
            action="job.created_from_jd",
            resource_type="job",
            resource_id=job.id,
            details={
                "title": job.title,
                "source_job_code": job.source_job_code,
                "jd_character_count": len(job.jd_text),
                "profile_version": draft.version_label,
                "jd_analysis_mode": job.semantic_profile.get("analysis_mode"),
                "excluded_non_job_factor_count": len(
                    job.semantic_profile.get("excluded_non_job_factors", [])
                ),
            },
        )
        db.commit()
        return {
            "job": _admin_job_payload(db, job),
            "talent_profile_draft": version_payload(draft),
            "draft_changed": changed,
            "refreshed_interviews": 0,
            "frozen_in_progress": 0,
        }

    @app.put("/api/v1/admin/jobs/{job_id}")
    def update_admin_job(
        job_id: str,
        payload: AdminJobSave,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job or job.status == "demo":
            raise HTTPException(404, "job not found")
        conflict = _find_job_conflict(
            db,
            title=payload.title,
            source_job_code=payload.source_job_code,
            exclude_job_id=job.id,
        )
        if conflict:
            raise HTTPException(409, "同名岗位或岗位编号已存在，请合并或更换编号")
        before_character_count = len(job.jd_text or "")
        job.title = payload.title
        job.source_job_code = payload.source_job_code
        job.jd_text = payload.jd_text
        job.status = payload.status
        previous_flow = (job.semantic_profile or {}).get("interview_flow")
        job.semantic_profile = request.app.state.intelligence.analyze_job_definition(
            payload.title, payload.jd_text
        )
        if previous_flow:
            job.semantic_profile = {
                **job.semantic_profile,
                "interview_flow": previous_flow,
            }
        db.flush()
        draft, changed = build_or_refresh_profile_draft(
            db,
            job=job,
            created_by=user["display_name"],
        )
        refreshed_interviews, frozen_in_progress = _refresh_planned_job_interviews(
            db, job, request.app.state.intelligence
        )
        record_audit_event(
            db,
            user,
            action="job.jd_updated",
            resource_type="job",
            resource_id=job.id,
            details={
                "title": job.title,
                "source_job_code": job.source_job_code,
                "previous_jd_character_count": before_character_count,
                "jd_character_count": len(job.jd_text),
                "profile_version": draft.version_label,
                "profile_draft_changed": changed,
                "refreshed_interviews": refreshed_interviews,
                "frozen_in_progress": frozen_in_progress,
                "jd_analysis_mode": job.semantic_profile.get("analysis_mode"),
                "excluded_non_job_factor_count": len(
                    job.semantic_profile.get("excluded_non_job_factors", [])
                ),
            },
        )
        db.commit()
        return {
            "job": _admin_job_payload(db, job),
            "talent_profile_draft": version_payload(draft),
            "draft_changed": changed,
            "refreshed_interviews": refreshed_interviews,
            "frozen_in_progress": frozen_in_progress,
        }

    @app.get("/api/v1/admin/jobs/{job_id}/talent-profile")
    def get_talent_profile_center(
        job_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return _talent_profile_center_payload(db, job)

    @app.get("/api/v1/admin/company-profile")
    def get_company_profile_center(
        request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        return build_company_profile_center(db)

    @app.put("/api/v1/admin/company-profile/draft")
    def save_company_profile(
        payload: CompanyProfileDraftSave,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        version = save_company_profile_draft(
            db,
            payload=payload,
            created_by=user["display_name"],
        )
        record_audit_event(
            db,
            user,
            action="company_profile.draft_saved",
            resource_type="company_profile_version",
            resource_id=version.id,
            details={
                "version_label": version.version_label,
                "competency_count": len(payload.competencies),
            },
        )
        db.commit()
        return company_profile_version_payload(version)

    @app.post("/api/v1/admin/company-profile/versions/{version_id}/activate")
    def activate_company_profile(
        version_id: str,
        payload: CompanyProfileActivate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before activating a company profile")
        version = db.get(CompanyProfileVersion, version_id)
        if not version:
            raise HTTPException(404, "company profile version not found")
        if version.status != "draft":
            raise HTTPException(409, "only a draft company profile can be activated")

        current_versions = db.scalars(
            select(CompanyProfileVersion).where(CompanyProfileVersion.status == "active")
        ).all()
        for current in current_versions:
            current.status = "superseded"
        version.status = "active"
        version.approved_by = user["display_name"]
        version.approved_at = utc_now()
        db.flush()
        _publish_company_profile(
            version,
            request.app.state.settings,
            user["display_name"],
        )

        refreshed_interviews = 0
        pending_rounds = db.scalars(
            select(InterviewRound).where(
                InterviewRound.status == "planned"
            )
        ).all()
        frozen_in_progress = db.scalar(
            select(func.count()).select_from(InterviewRound).where(
                InterviewRound.status == "in_progress"
            )
        ) or 0
        for interview in pending_rounds:
            application = db.get(Application, interview.application_id)
            job = db.get(Job, application.job_id) if application else None
            if not job:
                continue
            interview.plan_payload = _build_interview_plan(
                db, request.app.state.intelligence, interview, job
            )
            interview.plan_version = interview.plan_payload["version"]
            refreshed_interviews += 1

        refreshed_job_profiles = 0
        for job in db.scalars(select(Job)).all():
            _, changed = build_or_refresh_profile_draft(
                db,
                job=job,
                created_by=user["display_name"],
            )
            refreshed_job_profiles += int(changed)

        record_audit_event(
            db,
            user,
            action="company_profile.activated",
            resource_type="company_profile_version",
            resource_id=version.id,
            details={
                "version_label": version.version_label,
                "refreshed_interviews": refreshed_interviews,
                "frozen_in_progress": frozen_in_progress,
                "refreshed_job_profiles": refreshed_job_profiles,
            },
        )
        db.commit()
        return {
            **company_profile_version_payload(version),
            "refreshed_interviews": refreshed_interviews,
            "frozen_in_progress": frozen_in_progress,
            "refreshed_job_profiles": refreshed_job_profiles,
        }

    @app.post("/api/v1/admin/company-profile/versions/{version_id}/publish")
    def retry_company_profile_publish(
        version_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        version = db.get(CompanyProfileVersion, version_id)
        if not version:
            raise HTTPException(404, "company profile version not found")
        if version.status != "active":
            raise HTTPException(409, "only the active company profile can be published")
        _publish_company_profile(
            version,
            request.app.state.settings,
            user["display_name"],
        )
        db.commit()
        return company_profile_version_payload(version)

    @app.get("/api/v1/admin/historical-samples/template.csv")
    def download_historical_sample_template(request: Request) -> PlainTextResponse:
        _require_role(request, "hr", "admin")
        template = (
            "\ufeff姓名,最终结果,面试评价,专业能力,问题解决,沟通表达,战略理解,试用期结果\r\n"
            "示例候选人,试用期通过,专业能力良好，能够主动复盘,4,5,4,3,通过\r\n"
        )
        return PlainTextResponse(
            template,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="historical-hiring-template.csv"'},
        )

    @app.post("/api/v1/admin/historical-samples/preview")
    async def preview_historical_samples(
        job_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        content = await request.body()
        filename = unquote(request.headers.get("x-filename", "beisen-export.xlsx"))
        try:
            preview = preview_historical_export(content, filename)
        except HistoricalImportError as error:
            raise HTTPException(422, str(error)) from error
        return {
            **preview,
            "job": {
                "id": job.id,
                "title": job.title,
                "source_job_code": job.source_job_code,
            },
        }

    @app.post("/api/v1/admin/historical-samples/commit", status_code=201)
    def commit_historical_samples(
        payload: HistoricalSampleCommit,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        job = db.get(Job, payload.job_id)
        if not job:
            raise HTTPException(404, "job not found")

        batch = HistoricalSampleImportBatch(
            id=new_id("hsb"),
            job_id=job.id,
            filename=PurePath(payload.filename).name,
            file_hash=payload.file_hash,
            total_rows=payload.total_rows,
            created_by=user["display_name"],
        )
        db.add(batch)
        db.flush()
        imported = 0
        skipped = 0
        submitted_hashes = [item.record_hash for item in payload.samples]
        known_hashes = set(
            db.scalars(
                select(HistoricalHiringSample.record_hash).where(
                    HistoricalHiringSample.job_id == job.id,
                    HistoricalHiringSample.record_hash.in_(submitted_hashes),
                )
            ).all()
        )
        for item in payload.samples:
            if item.record_hash in known_hashes:
                skipped += 1
                continue
            db.add(
                HistoricalHiringSample(
                    id=new_id("hss"),
                    batch_id=batch.id,
                    job_id=job.id,
                    source_row_number=item.row_number,
                    record_hash=item.record_hash,
                    outcome=item.outcome,
                    competency_signals=[signal.model_dump() for signal in item.competency_signals],
                    quality_flags=item.quality_flags,
                    source_quality="historical_reviewed",
                )
            )
            known_hashes.add(item.record_hash)
            imported += 1

        batch.imported_rows = imported
        batch.skipped_duplicates = skipped
        db.flush()
        draft, changed = build_or_refresh_profile_draft(
            db,
            job=job,
            created_by=user["display_name"],
        )
        db.commit()
        return {
            "batch_id": batch.id,
            "imported_rows": imported,
            "skipped_duplicates": skipped,
            "total_rows": payload.total_rows,
            "privacy": "已仅保存招聘结果、结构化能力信号和质量标记；未保存姓名、联系方式、简历或评价原文。",
            "talent_profile_update": {
                "version_label": draft.version_label,
                "draft_changed": changed,
                "status": draft.status,
                "evidence_summary": version_payload(draft)["evidence_summary"],
            },
        }

    @app.post("/api/v1/admin/jobs/{job_id}/talent-profile/draft")
    def create_talent_profile_draft(
        job_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        version, changed = build_or_refresh_profile_draft(
            db,
            job=job,
            created_by=user["display_name"],
        )
        db.commit()
        return {**version_payload(version), "draft_changed": changed}

    @app.post("/api/v1/admin/jobs/{job_id}/talent-profile/versions/{version_id}/activate")
    def activate_talent_profile_version(
        job_id: str,
        version_id: str,
        payload: TalentProfileActivate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before activating a profile")
        job = db.get(Job, job_id)
        version = db.get(TalentProfileVersion, version_id)
        if not job or not version or version.job_id != job_id:
            raise HTTPException(404, "talent profile version not found")
        if version.status != "draft":
            raise HTTPException(409, "only a draft profile can be activated")
        if version.source_mode == "outcome_aggregation" and not version.evidence_summary.get("threshold_met"):
            raise HTTPException(409, "at least three offer-approved samples are required before an outcome-based profile can be activated")

        current_versions = db.scalars(
            select(TalentProfileVersion).where(
                TalentProfileVersion.job_id == job_id,
                TalentProfileVersion.status == "active",
            )
        ).all()
        for current in current_versions:
            current.status = "superseded"
        version.status = "active"
        version.approved_by = user["display_name"]
        version.approved_at = utc_now()
        _publish_talent_profile(
            version,
            job,
            request.app.state.settings,
            user["display_name"],
        )
        db.commit()
        return version_payload(version)

    @app.post("/api/v1/admin/jobs/{job_id}/talent-profile/versions/{version_id}/publish")
    def retry_talent_profile_publish(
        job_id: str,
        version_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        version = db.get(TalentProfileVersion, version_id)
        if not job or not version or version.job_id != job_id:
            raise HTTPException(404, "talent profile version not found")
        if version.status != "active":
            raise HTTPException(409, "only the active profile can be published")
        _publish_talent_profile(
            version,
            job,
            request.app.state.settings,
            user["display_name"],
        )
        db.commit()
        return version_payload(version)

    @app.post("/api/v1/admin/resume-imports", status_code=201)
    def create_resume_import(
        payload: ResumeImportBatchCreate, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        job = db.get(Job, payload.job_id) if payload.job_id else None
        if payload.job_id and not job:
            raise HTTPException(404, "job not found")
        if not job:
            job = Job(
                id=new_id("job"), title=(payload.job_title or "").strip(), jd_text=payload.jd_text.strip(),
                source_job_code=payload.source_job_code.strip() if payload.source_job_code else None,
                status="pilot", competencies=[],
                semantic_profile=request.app.state.intelligence.analyze_job_definition(
                    (payload.job_title or "").strip(), payload.jd_text.strip()
                ),
            )
            db.add(job)
        batch = ResumeImportBatch(
            id=new_id("batch"), job_id=job.id, created_by_open_id=user["open_id"], status="uploading"
        )
        db.add(batch)
        db.commit()
        return {"id": batch.id, "status": batch.status, "job": {"id": job.id, "title": job.title}, "items": []}

    @app.post("/api/v1/admin/resume-imports/{batch_id}/items", status_code=201)
    async def upload_resume_import_item(
        batch_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        batch = db.get(ResumeImportBatch, batch_id)
        if not batch:
            raise HTTPException(404, "resume import batch not found")
        if batch.status == "completed":
            raise HTTPException(409, "resume import batch is already completed")
        content = await request.body()
        filename = unquote(request.headers.get("x-filename", "resume.txt"))[:256]
        digest = hashlib.sha256(content).hexdigest()
        existing = db.scalar(select(ResumeImportItem).where(ResumeImportItem.batch_id == batch.id, ResumeImportItem.content_hash == digest))
        if existing:
            return _resume_item_payload(existing)
        text = _extract_document(content, filename)
        recognized = recognize_resume(text, filename)
        duplicate_candidate_id = _find_duplicate_candidate(db, recognized["fields"])
        if duplicate_candidate_id:
            recognized["warnings"].append("系统中存在相同联系方式的候选人，请确认是否重复建档")
        item = ResumeImportItem(
            id=new_id("rimp"), batch_id=batch.id, filename=filename, content_hash=digest,
            raw_text=text, recognized_payload=recognized,
            status="needs_review" if recognized["warnings"] else "recognized",
            duplicate_candidate_id=duplicate_candidate_id,
        )
        db.add(item)
        db.commit()
        return _resume_item_payload(item)

    @app.get("/api/v1/admin/resume-imports/{batch_id}")
    def get_resume_import(batch_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        batch = db.get(ResumeImportBatch, batch_id)
        if not batch:
            raise HTTPException(404, "resume import batch not found")
        job = db.get(Job, batch.job_id)
        items = db.scalars(select(ResumeImportItem).where(ResumeImportItem.batch_id == batch.id).order_by(ResumeImportItem.created_at)).all()
        return {"id": batch.id, "status": batch.status, "job": {"id": job.id, "title": job.title}, "items": [_resume_item_payload(item) for item in items]}

    @app.patch("/api/v1/admin/resume-imports/{batch_id}/items/{item_id}")
    def update_resume_import_item(
        batch_id: str, item_id: str, payload: ResumeImportItemUpdate,
        request: Request, db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        item = db.get(ResumeImportItem, item_id)
        if not item or item.batch_id != batch_id:
            raise HTTPException(404, "resume import item not found")
        recognized = dict(item.recognized_payload or {})
        recognized["fields"] = payload.model_dump()
        recognized["warnings"] = []
        recognized["human_verified"] = True
        item.recognized_payload = recognized
        item.status = "verified"
        db.commit()
        return _resume_item_payload(item)

    @app.delete("/api/v1/admin/resume-imports/{batch_id}/items/{item_id}", status_code=204)
    def delete_resume_import_item(
        batch_id: str,
        item_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> Response:
        _require_role(request, "hr", "admin")
        item = db.get(ResumeImportItem, item_id)
        if not item or item.batch_id != batch_id:
            raise HTTPException(404, "resume import item not found")
        if item.candidate_id:
            raise HTTPException(409, "candidate profile already created; delete it from the recruiting task list")
        db.delete(item)
        db.commit()
        return Response(status_code=204)

    @app.post("/api/v1/admin/resume-imports/{batch_id}/commit")
    def commit_resume_import(
        batch_id: str, payload: ResumeImportCommit, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        batch = db.get(ResumeImportBatch, batch_id)
        if not batch:
            raise HTTPException(404, "resume import batch not found")
        selected = db.scalars(select(ResumeImportItem).where(ResumeImportItem.batch_id == batch.id, ResumeImportItem.id.in_(payload.item_ids))).all()
        if len(selected) != len(set(payload.item_ids)):
            raise HTTPException(422, "one or more resume items do not belong to this batch")
        created, reused = [], []
        for item in selected:
            if item.candidate_id:
                reused.append(item.candidate_id)
                continue
            fields = (item.recognized_payload or {}).get("fields", {})
            name = (fields.get("name") or "").strip()
            if not name:
                raise HTTPException(422, f"{item.filename}: candidate name is required")
            candidate = Candidate(
                id=new_id("cand"), display_name=name, resume_text=item.raw_text,
                source="resume_batch", source_candidate_id=None,
                retention_until=utc_now() + timedelta(days=payload.retention_days),
            )
            application = Application(
                id=new_id("app"), candidate_id=candidate.id, job_id=batch.job_id,
                current_stage="interview_to_schedule",
                screening_payload={"source": "resume_batch", "screening_completed_externally": True, "import_item_id": item.id},
            )
            profile = CandidateProfile(
                id=new_id("profile"), candidate_id=candidate.id, structured_data=fields,
                recognition_version=(item.recognized_payload or {}).get("recognition_version", "resume-rules-v0.1"),
                source_import_item_id=item.id, verified_by_open_id=user["open_id"], verified_at=utc_now(),
            )
            db.add_all([candidate, application, profile])
            item.candidate_id, item.status = candidate.id, "imported"
            created.append({"candidate_id": candidate.id, "application_id": application.id, "name": name})
        batch.status, batch.completed_at = "completed", utc_now()
        db.commit()
        return {"created": created, "reused_candidate_ids": reused, "job_id": batch.job_id, "next_step": "schedule_interviews"}

    @app.post("/api/v1/interview-tasks", status_code=201)
    def create_interview_task(
        payload: InterviewTaskCreate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        """Create the candidate, job, application and ordered interview rounds atomically."""
        _require_role(request, "hr", "admin")
        application = db.get(Application, payload.application_id) if payload.application_id else None
        if payload.application_id:
            if not application:
                raise HTTPException(404, "application not found")
            existing_round = db.scalar(select(InterviewRound).where(InterviewRound.application_id == application.id))
            if existing_round:
                raise HTTPException(409, "interviews are already scheduled for this candidate")
            candidate = db.get(Candidate, application.candidate_id)
            job = db.get(Job, application.job_id)
            application.current_stage = _stage_for_round(payload.rounds[0].round_type)
        else:
            candidate = Candidate(
                id=new_id("cand"), display_name=payload.candidate_name.strip(),
                resume_text=payload.resume_text.strip(), source="manual_task",
                source_candidate_id=None, retention_until=utc_now() + timedelta(days=payload.retention_days),
            )
            job = db.get(Job, payload.job_id) if payload.job_id else _find_reusable_job(
                db,
                title=payload.job_title,
                source_job_code=payload.source_job_code,
            )
            if payload.job_id and not job:
                raise HTTPException(404, "job not found")
            if job and job.status == "paused":
                raise HTTPException(409, "该岗位已暂停招聘，请先在岗位与 JD 中恢复")
            if not job:
                job = Job(
                    id=new_id("job"), title=payload.job_title.strip(), jd_text=payload.jd_text.strip(),
                    source_job_code=payload.source_job_code.strip() if payload.source_job_code else None,
                    status="pilot", competencies=[],
                    semantic_profile=request.app.state.intelligence.analyze_job_definition(
                        payload.job_title.strip(), payload.jd_text.strip()
                    ),
                )
                db.add(job)
            elif not job.jd_text.strip() and payload.jd_text.strip():
                job.jd_text = payload.jd_text.strip()
                previous_flow = (job.semantic_profile or {}).get("interview_flow")
                job.semantic_profile = request.app.state.intelligence.analyze_job_definition(
                    job.title, job.jd_text
                )
                if previous_flow:
                    job.semantic_profile = {
                        **job.semantic_profile,
                        "interview_flow": previous_flow,
                    }
            application = Application(
                id=new_id("app"), candidate_id=candidate.id, job_id=job.id,
                current_stage=_stage_for_round(payload.rounds[0].round_type),
                screening_payload=payload.screening_payload,
            )
            db.add_all([candidate, application])
        db.flush()

        configured_order = [item.round_type for item in payload.rounds]
        semantic_profile = dict(job.semantic_profile or {})
        semantic_profile["interview_flow"] = {
            "round_order": configured_order,
            "updated_at": utc_now().isoformat(),
            "source": "hr_schedule",
        }
        job.semantic_profile = semantic_profile

        rounds: list[InterviewRound] = []
        for item in payload.rounds:
            interview = InterviewRound(
                id=new_id("round"),
                application_id=application.id,
                round_type=item.round_type,
                interview_mode=item.interview_mode,
                interviewer_names=item.interviewer_names,
                scheduled_at=item.scheduled_at,
                meeting_source=item.meeting_source,
            )
            db.add(interview)
            db.flush()
            _replace_assignments(
                db,
                interview,
                item.interviewer_open_ids,
                item.interviewer_names,
                request.state.user["open_id"],
            )
            plan = _build_interview_plan(
                db, request.app.state.intelligence, interview, job
            )
            interview.plan_version = plan["version"]
            interview.plan_payload = plan
            rounds.append(interview)
        db.commit()
        return {
            "task_id": application.id,
            "candidate": CandidateRead.model_validate(candidate),
            "job": JobRead.model_validate(job),
            "rounds": [InterviewRoundRead.model_validate(item) for item in rounds],
            "active_interview_id": rounds[0].id,
            "routing": {
                "rule": "task -> application -> job -> round_type -> versioned plan",
                "round_order": configured_order,
                "round_count": len(configured_order),
            },
        }

    @app.get("/api/v1/admin/users")
    def list_assignable_users(
        request: Request,
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        _require_role(request, "hr", "admin")
        identities = db.scalars(
            select(UserIdentity)
            .where(UserIdentity.active.is_(True))
            .order_by(UserIdentity.display_name)
        ).all()
        return [_user_payload(item) for item in identities]

    @app.get("/api/v1/admin/interview-tasks")
    def list_interview_tasks(
        request: Request,
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        _require_role(request, "hr", "admin")
        applications = db.scalars(
            select(Application).order_by(Application.created_at.desc())
        ).all()
        output = []
        for application in applications:
            candidate = db.get(Candidate, application.candidate_id)
            job = db.get(Job, application.job_id)
            if not candidate or not job or candidate.source == "demo":
                continue
            rounds = db.scalars(
                select(InterviewRound)
                .where(InterviewRound.application_id == application.id)
                .order_by(InterviewRound.scheduled_at)
            ).all()
            deletion = _application_deletion_status(db, application.id, rounds)
            output.append(
                {
                    "task_id": application.id,
                    "candidate": {"id": candidate.id, "display_name": candidate.display_name, "resume_text": candidate.resume_text},
                    "job": {"id": job.id, "title": job.title, "jd_text": job.jd_text, "source_job_code": job.source_job_code},
                    "current_stage": application.current_stage,
                    "rounds": [_managed_round_payload(db, item) for item in rounds],
                    "deletion": deletion,
                    "created_at": application.created_at,
                }
            )
        return output

    @app.delete("/api/v1/admin/applications/{application_id}")
    def delete_unscheduled_application(
        application_id: str,
        request: Request,
        confirmed: bool = False,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not confirmed:
            raise HTTPException(422, "explicit confirmation is required")
        application = db.get(Application, application_id)
        if not application:
            raise HTTPException(404, "application not found")
        rounds = db.scalars(
            select(InterviewRound).where(InterviewRound.application_id == application.id)
        ).all()
        deletion = _application_deletion_status(db, application.id, rounds)
        if not deletion["allowed"]:
            raise HTTPException(409, deletion["reason"])
        candidate = db.get(Candidate, application.candidate_id)
        job = db.get(Job, application.job_id)
        reports = db.scalars(
            select(InterviewReportVersion).where(InterviewReportVersion.application_id == application.id)
        ).all()
        for report in reports:
            db.delete(report)
        for interview in rounds:
            assignments = db.scalars(
                select(InterviewAssignment).where(
                    InterviewAssignment.interview_round_id == interview.id
                )
            ).all()
            for assignment in assignments:
                db.delete(assignment)
            db.delete(interview)
        has_other_application = bool(
            candidate
            and db.scalar(
                select(Application.id)
                .where(
                    Application.candidate_id == candidate.id,
                    Application.id != application.id,
                )
                .limit(1)
            )
        )
        if candidate and not has_other_application:
            profile = db.scalar(
                select(CandidateProfile).where(CandidateProfile.candidate_id == candidate.id)
            )
            if profile:
                db.delete(profile)
            linked_items = db.scalars(
                select(ResumeImportItem).where(ResumeImportItem.candidate_id == candidate.id)
            ).all()
            for item in linked_items:
                db.delete(item)
            duplicate_items = db.scalars(
                select(ResumeImportItem).where(ResumeImportItem.duplicate_candidate_id == candidate.id)
            ).all()
            for item in duplicate_items:
                item.duplicate_candidate_id = None
        record_audit_event(
            db,
            user,
            action="application.interview_task_deleted",
            resource_type="application",
            resource_id=application.id,
            details={
                "candidate_name": candidate.display_name if candidate else "unknown",
                "job_title": job.title if job else "unknown",
                "scope": "interview_task",
                "round_count": len(rounds),
            },
        )
        db.delete(application)
        db.flush()
        if candidate and not has_other_application:
            db.delete(candidate)
        db.commit()
        return {
            "deleted": True,
            "application_id": application_id,
            "candidate_deleted": bool(candidate and not has_other_application),
        }

    @app.get("/api/v1/admin/action-center")
    def hr_action_center(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        return build_hr_action_center(db)

    @app.get("/api/v1/admin/governance")
    def get_governance_center(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        return build_governance_center(db, request.app.state.settings)

    @app.post("/api/v1/admin/governance/retention/execute")
    def run_retention_cleanup(
        payload: RetentionCleanupRequest,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before deleting expired sensitive materials")
        try:
            result = execute_retention_cleanup(
                db,
                request.app.state.settings,
                user,
                payload.candidate_ids,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        db.commit()
        result["governance"] = build_governance_center(db, request.app.state.settings)
        return result

    @app.get("/api/v1/admin/notifications")
    def get_notification_center(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        return build_notification_center(db, request.app.state.settings)

    @app.post("/api/v1/admin/notifications/sync")
    def refresh_notification_queue(
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        result = sync_notification_queue(db, request.app.state.settings)
        db.commit()
        return result

    @app.post("/api/v1/admin/notifications/dispatch")
    def send_notification_queue(
        payload: NotificationDispatchRequest,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before sending Feishu messages")
        try:
            result = dispatch_notifications(
                db,
                request.app.state.settings,
                request.app.state.notification_sender,
                user,
                payload.notification_ids,
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        db.commit()
        result["queue"] = build_notification_center(db, request.app.state.settings)
        return result

    @app.get("/api/v1/admin/applications/{application_id}/final-review")
    def get_application_final_review(
        application_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        application = db.get(Application, application_id)
        if not application:
            raise HTTPException(404, "application not found")
        return build_final_review(db, application)

    @app.post("/api/v1/admin/applications/{application_id}/final-decision")
    def submit_application_final_decision(
        application_id: str,
        payload: ApplicationFinalDecision,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before changing candidate stage")
        application = db.get(Application, application_id)
        if not application:
            raise HTTPException(404, "application not found")
        review = build_final_review(db, application)
        if payload.decision == "offer_approval" and review["readiness"]["status"] != "ready_for_hr_decision":
            raise HTTPException(409, "all configured interview rounds and their human scorecards are required before offer approval")
        if payload.decision == "reject" and review["readiness"]["scorecards_submitted"] == 0:
            raise HTTPException(409, "a rejection decision requires at least one submitted human scorecard")

        stage_by_decision = {
            "offer_approval": "offer_approval",
            "supplementary_interview": "supplementary_interview",
            "hold": "on_hold",
            "reject": "closed_rejected",
        }
        application.human_final_decision = payload.decision
        application.current_stage = stage_by_decision[payload.decision]
        screening_payload = dict(application.screening_payload or {})
        screening_payload["final_review"] = {
            "decision": payload.decision,
            "decided_by": payload.decided_by,
            "notes": payload.notes,
            "decided_at": utc_now().isoformat(),
            "confirmed_by_hr": True,
            "source": "three_round_final_review",
        }
        application.screening_payload = screening_payload
        profile_draft = None
        job = db.get(Job, application.job_id)
        db.flush()
        if payload.decision == "offer_approval" and job:
            profile_draft = maybe_refresh_outcome_profile_draft(
                db,
                job=job,
                created_by=user["display_name"],
            )
        record_audit_event(
            db,
            user,
            action="application.final_decision",
            resource_type="application",
            resource_id=application.id,
            details={
                "decision": payload.decision,
                "new_stage": application.current_stage,
                "profile_draft_created": bool(profile_draft),
            },
        )
        db.commit()
        result = build_final_review(db, application)
        result["talent_profile_update"] = (
            {
                "status": "draft_ready",
                "version_id": profile_draft.id,
                "version_label": profile_draft.version_label,
            }
            if profile_draft
            else {"status": "waiting_for_sample_threshold"}
        )
        return result

    @app.get("/api/v1/admin/applications/{application_id}/reports")
    def list_application_reports(
        application_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        application = db.get(Application, application_id)
        if not application:
            raise HTTPException(404, "application not found")
        versions = list_report_versions(db, application_id)
        return {
            "application_id": application_id,
            "versions": [report_metadata(item) for item in versions],
            "current_draft_id": next((item.id for item in versions if item.status == "draft"), None),
            "current_locked_id": next((item.id for item in versions if item.status == "locked"), None),
        }

    @app.post("/api/v1/admin/applications/{application_id}/reports/draft", status_code=201)
    def create_application_report_draft(
        application_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        application = db.get(Application, application_id)
        if not application:
            raise HTTPException(404, "application not found")
        report = build_or_refresh_report_draft(db, application, user["display_name"])
        db.commit()
        return report_payload(report, "management")

    @app.post("/api/v1/admin/reports/{report_id}/lock")
    def lock_application_report(
        report_id: str,
        payload: InterviewReportLock,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before locking a report")
        report = db.get(InterviewReportVersion, report_id)
        if not report:
            raise HTTPException(404, "report not found")
        try:
            lock_report(db, report, user["display_name"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        record_audit_event(
            db,
            user,
            action="report.locked",
            resource_type="interview_report",
            resource_id=report.id,
            details={
                "application_id": report.application_id,
                "version_number": report.version_number,
                "content_hash": report.content_hash,
            },
        )
        db.commit()
        return report_payload(report, "management")

    @app.get("/api/v1/reports/{report_id}")
    def view_interview_report(
        report_id: str,
        request: Request,
        audience: str = "management",
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        report = db.get(InterviewReportVersion, report_id)
        if not report:
            raise HTTPException(404, "report not found")
        if not _can_view_report(db, request.state.user, report, audience):
            raise HTTPException(403, "this report is not available to the current user")
        try:
            return report_payload(report, audience)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/api/v1/reports/{report_id}/print", response_class=HTMLResponse)
    def print_interview_report(
        report_id: str,
        request: Request,
        audience: str = "management",
        db: Session = Depends(get_db),
    ) -> HTMLResponse:
        report = db.get(InterviewReportVersion, report_id)
        if not report:
            raise HTTPException(404, "report not found")
        if not _can_view_report(db, request.state.user, report, audience):
            raise HTTPException(403, "this report is not available to the current user")
        try:
            return HTMLResponse(render_report_html(report, audience))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.patch("/api/v1/admin/interviews/{interview_id}")
    def manage_interview_round(
        interview_id: str,
        payload: InterviewRoundManageUpdate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        interview = _interview_or_404(db, interview_id)
        if interview.status in {"in_progress", "completed", "cancelled"}:
            raise HTTPException(409, f"cannot edit interview from status {interview.status}")
        if payload.scheduled_at is not None:
            interview.scheduled_at = payload.scheduled_at
        if payload.meeting_source is not None:
            interview.meeting_source = payload.meeting_source
        if payload.interview_mode is not None and payload.interview_mode != interview.interview_mode:
            interview.interview_mode = payload.interview_mode
            _, _, _, job = _context_or_404(db, interview.id)
            interview.plan_payload = _build_interview_plan(
                db, request.app.state.intelligence, interview, job
            )
            interview.plan_version = interview.plan_payload["version"]
        if payload.interviewer_open_ids is not None:
            interview.interviewer_names = payload.interviewer_names or []
            _replace_assignments(
                db,
                interview,
                payload.interviewer_open_ids,
                payload.interviewer_names or [],
                request.state.user["open_id"],
            )
        db.commit()
        return _managed_round_payload(db, interview)

    @app.post("/api/v1/admin/interviews/{interview_id}/cancel")
    def cancel_interview_round(
        interview_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        interview = _interview_or_404(db, interview_id)
        if interview.status in {"in_progress", "completed"}:
            raise HTTPException(409, f"cannot cancel interview from status {interview.status}")
        interview.status = "cancelled"
        db.commit()
        return _managed_round_payload(db, interview)

    @app.post("/api/v1/candidates", response_model=CandidateRead, status_code=201)
    def create_candidate(
        payload: CandidateCreate, request: Request, db: Session = Depends(get_db)
    ) -> Candidate:
        retention_days = payload.retention_days or request.app.state.settings.retention_days
        if retention_days > request.app.state.settings.max_retention_days:
            raise HTTPException(422, "retention_days exceeds configured maximum")
        candidate = Candidate(
            id=new_id("cand"),
            display_name=payload.display_name,
            resume_text=payload.resume_text,
            source=payload.source,
            source_candidate_id=payload.source_candidate_id,
            resume_asset_url=payload.resume_asset_url,
            retention_until=utc_now() + timedelta(days=retention_days),
        )
        db.add(candidate)
        db.commit()
        return candidate

    @app.post("/api/v1/jobs", response_model=JobRead, status_code=201)
    def create_job(payload: JobCreate, db: Session = Depends(get_db)) -> Job:
        job = Job(
            id=new_id("job"),
            title=payload.title,
            jd_text=payload.jd_text,
            source_job_code=payload.source_job_code,
            competencies=[item.model_dump() for item in payload.competencies],
            semantic_profile=build_local_job_semantic_profile(payload.title, payload.jd_text),
        )
        db.add(job)
        db.commit()
        return job

    @app.post("/api/v1/applications", response_model=ApplicationRead, status_code=201)
    def create_application(payload: ApplicationCreate, db: Session = Depends(get_db)) -> Application:
        if not db.get(Candidate, payload.candidate_id):
            raise HTTPException(404, "candidate not found")
        if not db.get(Job, payload.job_id):
            raise HTTPException(404, "job not found")
        application = Application(
            id=new_id("app"),
            candidate_id=payload.candidate_id,
            job_id=payload.job_id,
            current_stage=payload.current_stage,
            screening_payload=payload.screening_payload,
        )
        db.add(application)
        db.commit()
        return application

    @app.post("/api/v1/interviews", response_model=InterviewRoundRead, status_code=201)
    def create_interview(
        payload: InterviewRoundCreate, db: Session = Depends(get_db)
    ) -> InterviewRound:
        if not db.get(Application, payload.application_id):
            raise HTTPException(404, "application not found")
        interview = InterviewRound(
            id=new_id("round"),
            application_id=payload.application_id,
            round_type=payload.round_type,
            interview_mode=payload.interview_mode,
            interviewer_names=payload.interviewer_names,
            scheduled_at=payload.scheduled_at,
            meeting_source=payload.meeting_source,
        )
        db.add(interview)
        db.commit()
        return interview

    @app.get("/api/v1/interviews/{interview_id}")
    def get_interview(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        interview, application, candidate, job = _context_or_404(db, interview_id)
        expected_bank = f"{interview.round_type}-standard-v0.1"
        latest_prior_context = prior_round_context(db, interview)
        if (
            not interview.plan_payload
            or interview.plan_payload.get("version") != "plan-v1.1"
            or interview.plan_payload.get("interview_mode") != interview.interview_mode
            or interview.plan_payload.get("round_type") != interview.round_type
            or interview.plan_payload.get("question_bank_version") != expected_bank
            or (
                interview.status == "planned"
                and interview.plan_payload.get("semantic_question_assistance", {}).get("status") == "active"
                and interview.plan_payload.get("question_mix", {}).get("resume_jd_match", 0) == 0
            )
            or (
                interview.status == "planned"
                and interview.plan_payload.get("prior_round_context") != latest_prior_context
            )
        ):
            interview.plan_payload = _build_interview_plan(
                db, request.app.state.intelligence, interview, job
            )
            interview.plan_version = interview.plan_payload["version"]
            db.commit()
        sibling_rounds = db.scalars(
            select(InterviewRound)
            .where(InterviewRound.application_id == application.id)
            .order_by(InterviewRound.scheduled_at, InterviewRound.created_at)
        ).all()
        return {
            "interview": InterviewRoundRead.model_validate(interview),
            "application": ApplicationRead.model_validate(application),
            "candidate": CandidateRead.model_validate(candidate),
            "job": JobRead.model_validate(job),
            "rounds": [
                {"id": item.id, "round_type": item.round_type, "scheduled_at": item.scheduled_at, "status": item.status}
                for item in sibling_rounds
            ],
            "routing": {
                "selected_round_id": interview.id,
                "round_type": interview.round_type,
                "question_bank_version": interview.plan_payload.get("question_bank_version"),
                "competency_model_version": job.competency_model_version,
            },
        }

    @app.post("/api/v1/interviews/{interview_id}/plan")
    def generate_plan(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        interview, _, _, job = _context_or_404(db, interview_id)
        plan = _build_interview_plan(
            db, request.app.state.intelligence, interview, job
        )
        interview.plan_version = plan["version"]
        interview.plan_payload = plan
        db.commit()
        return plan

    @app.put("/api/v1/interviews/{interview_id}/questions/progress")
    def update_question_progress(
        interview_id: str,
        payload: QuestionProgressUpdate,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        interview = _interview_or_404(db, interview_id)
        question_ids = {
            item.get("id") for item in (interview.plan_payload or {}).get("questions", [])
        }
        if payload.question_id not in question_ids:
            raise HTTPException(422, "question is not part of this interview plan")
        progress = db.scalar(
            select(InterviewQuestionProgress).where(
                InterviewQuestionProgress.interview_round_id == interview_id,
                InterviewQuestionProgress.question_id == payload.question_id,
            )
        )
        if not progress:
            progress = InterviewQuestionProgress(
                id=new_id("qp"),
                interview_round_id=interview_id,
                question_id=payload.question_id,
                asked_by=payload.asked_by,
            )
            db.add(progress)
        progress.asked = payload.asked
        progress.asked_by = payload.asked_by
        progress.asked_at = utc_now()
        progress.evidence_segment_ids = payload.evidence_segment_ids
        db.commit()
        return _question_progress_payload(interview, db)

    @app.get("/api/v1/interviews/{interview_id}/questions/progress")
    def question_progress(interview_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
        interview = _interview_or_404(db, interview_id)
        return _question_progress_payload(interview, db)

    @app.post("/api/v1/interviews/{interview_id}/notice", response_model=InterviewRoundRead)
    def acknowledge_notice(
        interview_id: str, payload: NoticeAcknowledge, db: Session = Depends(get_db)
    ) -> InterviewRound:
        interview = _interview_or_404(db, interview_id)
        if not payload.candidate_was_notified:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Recording/transcription notice must be completed before acknowledgement",
            )
        interview.notice_status = "acknowledged"
        interview.notice_acknowledged_at = utc_now()
        interview.notice_acknowledged_by = payload.acknowledged_by
        db.commit()
        return interview

    @app.post("/api/v1/interviews/{interview_id}/start", response_model=InterviewRoundRead)
    def start_interview(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> InterviewRound:
        interview, _, _, job = _context_or_404(db, interview_id)
        if interview.status not in {"planned", "ready"}:
            raise HTTPException(409, f"interview cannot start from status {interview.status}")
        if request.app.state.settings.require_recording_notice and interview.notice_status != "acknowledged":
            raise HTTPException(409, "candidate notice acknowledgement is required")
        if not interview.plan_payload:
            interview.plan_payload = _build_interview_plan(
                db, request.app.state.intelligence, interview, job
            )
            interview.plan_version = interview.plan_payload["version"]
        interview.status = "in_progress"
        interview.started_at = utc_now()
        db.commit()
        return interview

    @app.post("/api/v1/interviews/{interview_id}/end", response_model=InterviewRoundRead)
    def end_interview(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> InterviewRound:
        interview, _, _, job = _context_or_404(db, interview_id)
        if interview.status != "in_progress":
            raise HTTPException(409, "only an in-progress interview can be ended")
        interview.status = "completed"
        interview.ended_at = utc_now()
        _upsert_scorecard_draft(db, request.app.state.intelligence, interview, job)
        db.commit()
        return interview

    @app.post(
        "/api/v1/interviews/{interview_id}/segments",
        response_model=TranscriptSegmentRead,
        status_code=201,
    )
    def add_segment(
        interview_id: str,
        payload: TranscriptSegmentCreate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> TranscriptSegment:
        interview, _, _, job = _context_or_404(db, interview_id)
        segment = _persist_segment(db, interview, payload)
        if payload.provider_speaker_id is not None:
            observe_speaker(db, interview, payload.provider_speaker_id, payload.text)
        _analyze_live_with_history(
            db, request.app.state.intelligence, interview, job, segment
        )
        db.commit()
        return segment

    @app.get(
        "/api/v1/interviews/{interview_id}/segments",
        response_model=list[TranscriptSegmentRead],
    )
    def list_segments(interview_id: str, db: Session = Depends(get_db)) -> list[TranscriptSegment]:
        _interview_or_404(db, interview_id)
        return list(
            db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.interview_round_id == interview_id)
                .order_by(TranscriptSegment.start_ms)
            ).all()
        )

    @app.get(
        "/api/v1/interviews/{interview_id}/speaker-mappings",
        response_model=list[SpeakerRoleMappingRead],
    )
    def list_speaker_mappings(
        interview_id: str, db: Session = Depends(get_db)
    ) -> list[dict[str, Any]]:
        _interview_or_404(db, interview_id)
        return speaker_mapping_payloads(db, interview_id)

    @app.put(
        "/api/v1/interviews/{interview_id}/speaker-mappings/{provider_speaker_id}",
        response_model=list[SpeakerRoleMappingRead],
    )
    def update_speaker_mapping(
        interview_id: str,
        provider_speaker_id: int,
        payload: SpeakerRoleMappingUpdate,
        request: Request,
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        if not 0 <= provider_speaker_id <= 9:
            raise HTTPException(422, "provider speaker id must be between 0 and 9")
        interview = _interview_or_404(db, interview_id)
        user = request.state.user
        confirm_speaker_role(
            db,
            interview,
            provider_speaker_id,
            payload.speaker_role,
            str(user.get("display_name") or user.get("open_id") or "当前面试官"),
        )
        record_audit_event(
            db,
            user,
            action="transcript.speaker_role_confirmed",
            resource_type="interview_round",
            resource_id=interview_id,
            details={
                "provider_speaker_id": provider_speaker_id,
                "speaker_role": payload.speaker_role,
            },
        )
        db.commit()
        return speaker_mapping_payloads(db, interview_id)

    @app.get("/api/v1/interviews/{interview_id}/live-state")
    def live_state(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        interview, _, _, job = _context_or_404(db, interview_id)
        analysis = _analyze_live_with_history(
            db, request.app.state.intelligence, interview, job
        )
        db.commit()
        return analysis

    @app.patch("/api/v1/interviews/{interview_id}/suggestions/{suggestion_id}")
    def set_suggestion_status(
        interview_id: str,
        suggestion_id: str,
        payload: SuggestionStatusUpdate,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        interview = _interview_or_404(db, interview_id)
        updated = update_suggestion_status(interview, suggestion_id, payload.status)
        if updated is None:
            raise HTTPException(404, "suggestion not found")
        db.commit()
        return updated

    @app.get(
        "/api/v1/interviews/{interview_id}/evidence", response_model=list[EvidenceRead]
    )
    def list_evidence(interview_id: str, db: Session = Depends(get_db)) -> list[EvidenceItem]:
        _interview_or_404(db, interview_id)
        items = list(
            db.scalars(
                select(EvidenceItem)
                .where(EvidenceItem.interview_round_id == interview_id)
                .order_by(EvidenceItem.created_at)
            ).all()
        )
        return [
            item
            for item in items
            if item.human_status != "rejected"
            and is_usable_evidence_record(
                quote=item.quote,
                direction=item.direction,
                explanation=item.explanation,
                human_status=item.human_status,
            )
        ]

    @app.patch("/api/v1/evidence/{evidence_id}", response_model=EvidenceRead)
    def review_evidence(
        evidence_id: str, payload: EvidenceReview, db: Session = Depends(get_db)
    ) -> EvidenceItem:
        evidence = db.get(EvidenceItem, evidence_id)
        if not evidence:
            raise HTTPException(404, "evidence not found")
        evidence.human_status = payload.status
        evidence.reviewed_by = payload.reviewed_by
        evidence.reviewed_at = utc_now()
        if payload.explanation:
            evidence.explanation = payload.explanation
        db.commit()
        return evidence

    @app.post(
        "/api/v1/interviews/{interview_id}/scorecard/draft",
        response_model=ScorecardRead,
    )
    def draft_scorecard(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> Scorecard:
        interview, _, _, job = _context_or_404(db, interview_id)
        if interview.status != "completed":
            raise HTTPException(409, "scorecard draft requires a completed interview")
        scorecard = _upsert_scorecard_draft(
            db, request.app.state.intelligence, interview, job
        )
        db.commit()
        return scorecard

    @app.get("/api/v1/interviews/{interview_id}/scorecard", response_model=ScorecardRead)
    def get_scorecard(
        interview_id: str, request: Request, db: Session = Depends(get_db)
    ) -> Scorecard:
        scorecard = db.scalar(
            select(Scorecard).where(Scorecard.interview_round_id == interview_id)
        )
        if not scorecard:
            raise HTTPException(404, "scorecard not found")
        recommendation = scorecard.recommendation or {}
        if (
            scorecard.rubric_version not in {"five-level-v0.4", "conversation-review-v1.0"}
            or not recommendation.get("ai_recommendation")
            or not recommendation.get("response_quality")
        ):
            interview, _, _, job = _context_or_404(db, interview_id)
            scorecard = _upsert_scorecard_draft(
                db, request.app.state.intelligence, interview, job
            )
            db.commit()
        return scorecard

    @app.post("/api/v1/interviews/{interview_id}/scorecard/submit", response_model=ScorecardRead)
    def submit_scorecard(
        interview_id: str,
        payload: ScorecardSubmit,
        db: Session = Depends(get_db),
    ) -> Scorecard:
        interview, application, candidate, job = _context_or_404(db, interview_id)
        if interview.status != "completed":
            raise HTTPException(409, "scorecard submission requires a completed interview")
        scorecard = db.scalar(
            select(Scorecard).where(Scorecard.interview_round_id == interview_id)
        )
        if not scorecard:
            raise HTTPException(404, "scorecard draft not found")
        if (
            interview.interview_mode == "structured"
            and payload.decision in {"advance", "reject"}
            and not payload.scores
        ):
            raise HTTPException(422, "structured decisions require at least one evidence-backed human score")
        if (
            interview.interview_mode == "conversation"
            and payload.decision in {"advance", "reject"}
            and len((payload.summary_notes or "").strip()) < 5
        ):
            raise HTTPException(422, "conversation analysis decisions require a written evidence summary")

        ai_by_competency = {item.get("competency_id"): item for item in scorecard.ai_scores}
        evidence = db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.interview_round_id == interview_id,
                EvidenceItem.human_status.in_(["confirmed", "modified"]),
            )
        ).all()
        evidence_by_id = {item.id: item for item in evidence}
        for human_score in payload.scores:
            if human_score.competency_id not in ai_by_competency:
                raise HTTPException(422, f"unknown competency: {human_score.competency_id}")
            invalid = [
                evidence_id
                for evidence_id in human_score.evidence_ids
                if evidence_id not in evidence_by_id
                or evidence_by_id[evidence_id].competency_id != human_score.competency_id
            ]
            if invalid:
                raise HTTPException(422, "human scores must cite human-confirmed evidence from the same competency")

        scorecard.human_scores = [item.model_dump() for item in payload.scores]
        human_by_competency = {item.competency_id: item for item in payload.scores}
        scorecard.final_scores = []
        for ai_score in scorecard.ai_scores:
            human_score = human_by_competency.get(ai_score.get("competency_id"))
            scorecard.final_scores.append({
                "competency_id": ai_score.get("competency_id"),
                "competency_name": ai_score.get("competency_name"),
                "score": human_score.score if human_score else None,
                "assessment": "human_confirmed" if human_score else "not_submitted",
                "evidence_ids": human_score.evidence_ids if human_score else [],
                "note": human_score.note if human_score else None,
            })
        recommendation = dict(scorecard.recommendation or {})
        recommendation["human_decision"] = {
            "decision": payload.decision,
            "summary_notes": payload.summary_notes,
            "submitted_by": payload.submitted_by,
            "submitted_at": utc_now().isoformat(),
            "candidate_stage_changed": False,
        }
        scorecard.recommendation = recommendation
        scorecard.status = "submitted"
        scorecard.submitted_by = payload.submitted_by
        scorecard.submitted_at = utc_now()
        learning = generate_interview_knowledge_proposals(
            db,
            interview=interview,
            job=job,
            scorecard=scorecard,
            candidate_name=candidate.display_name,
        )
        recommendation = dict(scorecard.recommendation or {})
        recommendation["knowledge_learning"] = learning.as_payload()
        scorecard.recommendation = recommendation
        _advance_application_stage(db, application, interview.id)
        db.commit()
        return scorecard

    @app.post("/api/v1/interviews/{interview_id}/scorecard/dismiss", response_model=ScorecardRead)
    def dismiss_scorecard_todo(
        interview_id: str,
        payload: ScorecardDismiss,
        request: Request,
        db: Session = Depends(get_db),
    ) -> Scorecard:
        """Remove a completed round from action centers without deleting evidence."""
        interview = _interview_or_404(db, interview_id)
        if interview.status != "completed":
            raise HTTPException(409, "only completed interview feedback can be dismissed")
        user = request.state.user
        if user["role"] == "interviewer":
            assigned = db.scalar(
                select(InterviewAssignment.id).where(
                    InterviewAssignment.interview_round_id == interview_id,
                    InterviewAssignment.user_open_id == user["open_id"],
                )
            )
            if not assigned:
                raise HTTPException(403, "this interview is not assigned to the current user")
        scorecard = db.scalar(
            select(Scorecard).where(Scorecard.interview_round_id == interview_id)
        )
        if not scorecard:
            raise HTTPException(404, "scorecard draft not found")
        if scorecard.status == "submitted":
            raise HTTPException(409, "submitted feedback does not need to be dismissed")
        recommendation = dict(scorecard.recommendation or {})
        recommendation["todo_dismissal"] = {
            "dismissed_by": payload.dismissed_by,
            "dismissed_at": utc_now().isoformat(),
            "reason": payload.reason,
            "data_deleted": False,
        }
        scorecard.recommendation = recommendation
        scorecard.status = "dismissed"
        record_audit_event(
            db,
            user,
            action="scorecard.todo_dismissed",
            resource_type="interview_round",
            resource_id=interview_id,
            details={"reason": payload.reason, "data_deleted": False},
        )
        db.commit()
        return scorecard

    @app.get("/api/v1/interviews/{interview_id}/interviewer-review")
    def get_interviewer_review(interview_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
        interview = _interview_or_404(db, interview_id)
        existing = db.scalar(
            select(InterviewerQualityReview).where(
                InterviewerQualityReview.interview_round_id == interview_id
            )
        )
        return _interviewer_review_payload(interview, existing, build_interviewer_metrics(db, interview))

    @app.post("/api/v1/interviews/{interview_id}/interviewer-review")
    def submit_interviewer_review(
        interview_id: str,
        payload: InterviewerReviewSubmit,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        interview = _interview_or_404(db, interview_id)
        if interview.status != "completed":
            raise HTTPException(409, "interviewer review requires a completed interview")
        review = db.scalar(
            select(InterviewerQualityReview).where(
                InterviewerQualityReview.interview_round_id == interview_id
            )
        )
        if not review:
            review = InterviewerQualityReview(
                id=new_id("iqr"),
                interview_round_id=interview_id,
                interviewer_names=interview.interviewer_names,
            )
            db.add(review)
        review.automated_metrics = build_interviewer_metrics(db, interview)
        review.human_ratings = payload.ratings
        review.notes = payload.notes
        review.status = "reviewed"
        review.reviewed_by = payload.reviewed_by
        review.reviewed_at = utc_now()
        db.commit()
        return _interviewer_review_payload(interview, review, review.automated_metrics)

    @app.get("/api/v1/jobs/{job_id}/interviewer-quality")
    def job_interviewer_quality(
        job_id: str, request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        application_ids = list(
            db.scalars(select(Application.id).where(Application.job_id == job_id)).all()
        )
        rounds = list(
            db.scalars(
                select(InterviewRound).where(
                    InterviewRound.application_id.in_(application_ids),
                    InterviewRound.status == "completed",
                )
            ).all()
        ) if application_ids else []
        reviews = []
        for interview in rounds:
            review = db.scalar(
                select(InterviewerQualityReview).where(
                    InterviewerQualityReview.interview_round_id == interview.id
                )
            )
            reviews.append(_interviewer_review_payload(interview, review, build_interviewer_metrics(db, interview)))
        coverage = [item["automated_metrics"]["required_question_coverage"] for item in reviews]
        return {
            "job_id": job.id,
            "job_title": job.title,
            "interview_count": len(reviews),
            "average_required_question_coverage": round(sum(coverage) / len(coverage), 2) if coverage else None,
            "flagged_interview_count": sum(bool(item["automated_metrics"]["flags"]) for item in reviews),
            "reviews": reviews,
            "warning": "用于发现流程异常与辅导机会，不应单独作为面试官绩效结论。",
        }

    @app.get("/api/v1/admin/interviewer-quality/overview")
    def interviewer_quality_overview(
        request: Request,
        job_id: str | None = None,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        if job_id and not db.get(Job, job_id):
            raise HTTPException(404, "job not found")
        return build_quality_overview(db, job_id)

    @app.get("/api/v1/interviews/{interview_id}/recordings")
    def list_recordings(interview_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
        _interview_or_404(db, interview_id)
        recordings = db.scalars(
            select(AudioRecording)
            .where(AudioRecording.interview_round_id == interview_id)
            .order_by(AudioRecording.created_at.desc())
        ).all()
        return [_recording_payload(item) for item in recordings]

    @app.get("/api/v1/interviews/{interview_id}/transcript.txt", response_class=PlainTextResponse)
    def download_transcript(
        interview_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> PlainTextResponse:
        interview = _interview_or_404(db, interview_id)
        application = db.get(Application, interview.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        if candidate:
            retention_until = candidate.retention_until
            if retention_until.tzinfo is None:
                retention_until = retention_until.replace(tzinfo=timezone.utc)
            if retention_until <= utc_now():
                raise HTTPException(410, "transcript retention period has expired")
        segments = db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.interview_round_id == interview_id,
                TranscriptSegment.is_final.is_(True),
            )
            .order_by(TranscriptSegment.start_ms)
        ).all()
        speaker_labels = {"candidate": "候选人", "interviewer": "面试官", "unknown": "待确认"}
        text = "\n".join(
            f"[{speaker_labels.get(item.speaker_role, item.speaker_role)}] {item.effective_text}"
            for item in segments
        )
        record_audit_event(
            db,
            request.state.user,
            action="transcript.downloaded",
            resource_type="interview_round",
            resource_id=interview_id,
            details={"segment_count": len(segments)},
        )
        db.commit()
        return PlainTextResponse(
            text or "暂无逐字稿。",
            headers={"Content-Disposition": f'attachment; filename="{interview_id}-transcript.txt"'},
        )

    @app.get("/api/v1/recordings/{recording_id}/download")
    def download_recording(
        recording_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ) -> FileResponse:
        recording = db.get(AudioRecording, recording_id)
        if not recording:
            raise HTTPException(404, "recording not found")
        if not _can_access_interview_round(db, request.state.user, recording.interview_round_id):
            raise HTTPException(403, "this recording is not available to the current user")
        recording_root = request.app.state.settings.recording_dir.resolve()
        recording_path = (recording_root / PurePath(recording.storage_key)).resolve()
        if not recording_path.is_relative_to(recording_root):
            raise HTTPException(400, "invalid recording path")
        if not recording_path.exists():
            raise HTTPException(404, "recording file not found")
        record_audit_event(
            db,
            request.state.user,
            action="recording.downloaded",
            resource_type="audio_recording",
            resource_id=recording.id,
            details={"interview_round_id": recording.interview_round_id},
        )
        db.commit()
        return FileResponse(
            recording_path,
            media_type=recording.mime_type,
            filename=f"{recording.interview_round_id}-{recording.id}.wav",
        )

    @app.post("/api/v1/knowledge/proposals", status_code=201)
    def create_knowledge_proposal(
        request: Request,
        payload: KnowledgeProposalCreate,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        _interview_or_404(db, payload.source_round_id)
        proposal = KnowledgeProposal(
            id=new_id("kp"),
            source_round_id=payload.source_round_id,
            proposal_type=payload.proposal_type,
            payload=payload.payload,
            rationale=payload.rationale,
            status="pending",
        )
        db.add(proposal)
        db.commit()
        return _proposal_payload(proposal)

    @app.get("/api/v1/knowledge/proposals")
    def list_knowledge_proposals(
        request: Request, db: Session = Depends(get_db)
    ) -> list[dict[str, Any]]:
        _require_role(request, "hr", "admin")
        publications = {
            item.proposal_id: item
            for item in db.scalars(select(KnowledgePublication)).all()
        }
        return [
            _proposal_payload(item, publications.get(item.id))
            for item in db.scalars(
                select(KnowledgeProposal).order_by(KnowledgeProposal.created_at.desc())
            ).all()
        ]

    @app.get("/api/v1/admin/knowledge/status")
    def knowledge_status(
        request: Request, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        settings = request.app.state.settings
        vault = inspect_vault(
            settings.resolved_knowledge_vault_dir,
            settings.knowledge_vault_name,
        )
        proposals = db.scalars(select(KnowledgeProposal)).all()
        publications = db.scalars(select(KnowledgePublication)).all()
        counts = {
            "pending": sum(item.status == "pending" for item in proposals),
            "approved_for_publish": sum(
                item.status == "approved_for_publish" for item in proposals
            ),
            "published": sum(item.status == "published" for item in proposals),
            "rejected": sum(item.status == "rejected" for item in proposals),
            "publication_failed": sum(item.status == "failed" for item in publications),
        }
        return {
            "provider": "obsidian_markdown",
            "vault": {
                "configured": vault.configured,
                "exists": vault.exists,
                "writable": vault.writable,
                "path": vault.path,
                "name": vault.vault_name,
                "message": vault.message,
                "open_uri": (
                    f"obsidian://open?vault={quote(vault.vault_name, safe='')}"
                    f"&file={quote('首页', safe='')}"
                    if vault.writable
                    else None
                ),
            },
            "counts": counts,
            "policy": "AI 生成提案，HR 审批，发布成功后才进入正式知识库",
        }

    @app.get("/api/v1/admin/knowledge/system-docs")
    def system_docs_status(
        request: Request,
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        return build_system_docs_status(request.app.state.settings)

    @app.post("/api/v1/admin/knowledge/system-docs/sync")
    def synchronize_system_docs(
        payload: SystemDocsSyncRequest,
        request: Request,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        if not payload.confirmed_by_hr:
            raise HTTPException(422, "HR confirmation is required before syncing system documents")
        try:
            result = sync_system_docs(db, request.app.state.settings, user)
        except KnowledgePublishError as error:
            raise HTTPException(409, str(error)) from error
        db.commit()
        return result

    @app.patch("/api/v1/knowledge/proposals/{proposal_id}")
    def review_knowledge_proposal(
        request: Request,
        proposal_id: str,
        payload: KnowledgeProposalReview,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        _require_role(request, "hr", "admin")
        proposal = db.get(KnowledgeProposal, proposal_id)
        if not proposal:
            raise HTTPException(404, "knowledge proposal not found")
        if proposal.status != "pending":
            raise HTTPException(409, "knowledge proposal has already been reviewed")
        proposal.status = "approved_for_publish" if payload.decision == "approved" else "rejected"
        proposal.reviewed_by = payload.reviewed_by
        proposal.reviewed_at = utc_now()
        db.commit()
        if payload.decision == "approved":
            return _attempt_knowledge_publish(
                db,
                proposal,
                request.app.state.settings,
                payload.reviewed_by,
            )
        return _proposal_payload(proposal)

    @app.post("/api/v1/knowledge/proposals/{proposal_id}/publish")
    def retry_knowledge_publish(
        request: Request,
        proposal_id: str,
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        user = _require_role(request, "hr", "admin")
        proposal = db.get(KnowledgeProposal, proposal_id)
        if not proposal:
            raise HTTPException(404, "knowledge proposal not found")
        if proposal.status == "pending":
            raise HTTPException(409, "knowledge proposal must be approved before publishing")
        if proposal.status == "rejected":
            raise HTTPException(409, "rejected knowledge proposal cannot be published")
        return _attempt_knowledge_publish(
            db,
            proposal,
            request.app.state.settings,
            user["display_name"],
        )

    @app.post("/api/v1/demo/bootstrap", status_code=201)
    def bootstrap_demo(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        now = utc_now()
        candidate = Candidate(
            id=new_id("cand"),
            display_name="演示候选人 A",
            resume_text="5 年互联网运营经验，曾负责用户增长项目和跨部门协作。",
            source="demo",
            source_candidate_id=None,
            retention_until=now + timedelta(days=request.app.state.settings.retention_days),
        )
        job = Job(
            id=new_id("job"),
            title="演示岗位 · 业务运营",
            jd_text="负责业务增长、项目推进、数据复盘与跨团队协作。",
            source_job_code="DEMO-001",
            status="pilot",
            competencies=[],
            semantic_profile=build_local_job_semantic_profile(
                "演示岗位 · 业务运营",
                "负责业务增长、项目推进、数据复盘与跨团队协作。",
            ),
        )
        application = Application(
            id=new_id("app"),
            candidate_id=candidate.id,
            job_id=job.id,
            current_stage="business_interview",
            screening_payload={
                "schema_version": "1.0",
                "tier": "maybe",
                "summary": "仅用于产品演示，不代表真实筛选结论",
                "needs_human_review": True,
            },
        )
        db.add_all([candidate, job, application])
        db.flush()
        rounds = []
        for index, round_type in enumerate(("business", "hr", "ceo")):
            interview = InterviewRound(
                id=new_id("round"),
                application_id=application.id,
                round_type=round_type,
                interviewer_names=[{"hr": "HR 面试官", "business": "业务面试官", "ceo": "CEO"}[round_type]],
                scheduled_at=now + timedelta(hours=index),
                meeting_source="offline",
            )
            db.add(interview)
            db.flush()
            interview.plan_payload = build_plan(db, interview, job.competencies)
            interview.plan_version = interview.plan_payload["version"]
            rounds.append(interview)
        db.commit()
        return {
            "candidate_id": candidate.id,
            "job_id": job.id,
            "application_id": application.id,
            "rounds": [
                {"id": item.id, "round_type": item.round_type, "status": item.status}
                for item in rounds
            ],
            "active_interview_id": rounds[0].id,
            "current_user": {"id": "demo-interviewer", "display_name": "当前面试官"},
            "notice_required": request.app.state.settings.require_recording_notice,
        }

    @app.websocket("/ws/interviews/{interview_id}/live")
    async def interview_live(websocket: WebSocket, interview_id: str):
        if not _websocket_user_can_access(websocket, interview_id, database, app_settings):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                raw = await websocket.receive_json()
                if raw.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "at": datetime.now(timezone.utc).isoformat()})
                    continue
                if raw.get("type") != "transcript.final":
                    await websocket.send_json(
                        {"type": "error", "code": "unsupported_event", "message": "Expected transcript.final"}
                    )
                    continue
                try:
                    payload = TranscriptSegmentCreate.model_validate(raw.get("payload", {}))
                except ValidationError as exc:
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_payload", "message": str(exc)}
                    )
                    continue
                with database.session_factory() as db:
                    interview = db.get(InterviewRound, interview_id)
                    if not interview:
                        await websocket.send_json(
                            {"type": "error", "code": "not_found", "message": "Interview not found"}
                        )
                        continue
                    if interview.status != "in_progress":
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "invalid_state",
                                "message": "Interview must be in progress",
                            }
                        )
                        continue
                    _, _, _, job = _context_or_404(db, interview_id)
                    segment = _persist_segment(db, interview, payload)
                    analysis = _analyze_live_with_history(
                        db, app.state.intelligence, interview, job, segment
                    )
                    db.commit()
                    await websocket.send_json(
                        {
                            "type": "live.update",
                            "segment": TranscriptSegmentRead.model_validate(segment).model_dump(mode="json"),
                            "analysis": analysis,
                        }
                    )
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/interviews/{interview_id}/audio")
    async def interview_audio(websocket: WebSocket, interview_id: str):
        if not _websocket_user_can_access(websocket, interview_id, database, app_settings):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        settings: Settings = app.state.settings
        send_lock = asyncio.Lock()
        recording: AudioRecording | None = None
        recorder: AudioRecordingSession | None = None
        bridge: AudioFrameBridge | None = None
        asr_session = None
        asr_status = "not_configured"
        asr_error: str | None = None
        asr_reconnects = 0
        normal_stop = False
        semantic_tasks: set[asyncio.Task] = set()
        async_semantic_provider = hasattr(app.state.intelligence, "_chat_json")

        async def send_event(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async def handle_asr_event(event: ASREvent) -> None:
            text = event.text.strip()
            if not text:
                return
            if not event.is_final:
                await send_event(
                    {
                        "type": "transcript.interim",
                        "segment": {
                            "speaker_role": "unknown",
                            "speaker_confidence": 0.0,
                            "provider_speaker_id": event.speaker_id,
                            "start_ms": event.start_ms,
                            "end_ms": event.end_ms,
                            "text_raw": text,
                            "is_final": False,
                            "provider": event.provider,
                        },
                    }
                )
                return

            def persist_final_segment() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]] | None:
                with database.session_factory() as db:
                    interview = db.get(InterviewRound, interview_id)
                    if not interview or interview.status != "in_progress":
                        return None
                    _, _, _, job = _context_or_404(db, interview_id)
                    payload = TranscriptSegmentCreate(
                        speaker_role="unknown",
                        speaker_confidence=0.0,
                        provider_speaker_id=event.speaker_id,
                        start_ms=max(0, event.start_ms),
                        end_ms=max(event.start_ms, event.end_ms),
                        text=text,
                        is_final=True,
                    )
                    segment = _persist_segment(db, interview, payload)
                    if event.speaker_id is not None:
                        observe_speaker(db, interview, event.speaker_id, text)
                    db.commit()
                    # Return a fast local analysis immediately. Remote semantic
                    # analysis runs after the write lock has been released.
                    analysis = _analyze_live_with_history(
                        db,
                        app.state.intelligence,
                        interview,
                        job,
                        None if async_semantic_provider else segment,
                    )
                    db.commit()
                    serialized = TranscriptSegmentRead.model_validate(segment).model_dump(mode="json")
                    mappings = speaker_mapping_payloads(db, interview_id)
                    return serialized, mappings, analysis

            persisted = await asyncio.to_thread(persist_final_segment)
            if persisted is None:
                return
            serialized, mappings, analysis = persisted
            await send_event(
                {
                    "type": "transcript.final",
                    "segment": serialized,
                    "analysis": analysis,
                    "provider": event.provider,
                    "speaker_mappings": mappings,
                }
            )

            if not async_semantic_provider:
                return

            def enrich_semantic_analysis() -> dict[str, Any] | None:
                with database.session_factory() as db:
                    interview = db.get(InterviewRound, interview_id)
                    segment = db.get(TranscriptSegment, serialized["id"])
                    if not interview or not segment or interview.status != "in_progress":
                        return None
                    _, _, _, job = _context_or_404(db, interview_id)
                    result = _analyze_live_with_history(
                        db, app.state.intelligence, interview, job, segment
                    )
                    db.commit()
                    return result

            async def run_semantic_enrichment() -> None:
                try:
                    semantic_analysis = await asyncio.to_thread(enrich_semantic_analysis)
                    if semantic_analysis is not None and _analysis_update_is_meaningful(
                        analysis, semantic_analysis
                    ):
                        await send_event(
                            {
                                "type": "analysis.update",
                                "segment_id": serialized["id"],
                                "analysis": semantic_analysis,
                            }
                        )
                except Exception:
                    # ASR and recording must remain real-time even when the
                    # semantic provider or browser connection is unavailable.
                    return

            task = asyncio.create_task(run_semantic_enrichment())
            semantic_tasks.add(task)
            task.add_done_callback(semantic_tasks.discard)

        try:
            start_message = await websocket.receive_json()
            if start_message.get("type") != "audio.start":
                await websocket.send_json(
                    {"type": "error", "code": "missing_audio_start", "message": "Expected audio.start"}
                )
                await websocket.close(code=1008)
                return
            audio_config = start_message.get("audio", {})
            if (
                audio_config.get("format") != "pcm_s16le"
                or audio_config.get("sample_rate") != settings.audio_sample_rate
                or audio_config.get("channels") != settings.audio_channels
            ):
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "unsupported_audio_format",
                        "message": "Audio must be mono PCM s16le at 16 kHz",
                    }
                )
                await websocket.close(code=1003)
                return

            with database.session_factory() as db:
                interview, _, candidate, _ = _context_or_404(db, interview_id)
                if interview.status != "in_progress":
                    await websocket.send_json(
                        {"type": "error", "code": "invalid_state", "message": "Interview must be in progress"}
                    )
                    await websocket.close(code=1008)
                    return
                if settings.require_recording_notice and interview.notice_status != "acknowledged":
                    await websocket.send_json(
                        {"type": "error", "code": "notice_required", "message": "Candidate notice is required"}
                    )
                    await websocket.close(code=1008)
                    return
                recording_id = new_id("rec")
                relative_key = f"{interview.id}/{recording_id}.wav"
                recording = AudioRecording(
                    id=recording_id,
                    interview_round_id=interview.id,
                    storage_key=relative_key,
                    sample_rate=settings.audio_sample_rate,
                    channels=settings.audio_channels,
                    retention_until=candidate.retention_until,
                )
                db.add(recording)
                db.commit()

            recorder = AudioRecordingSession(
                path=settings.recording_dir / PurePath(recording.storage_key),
                sample_rate=settings.audio_sample_rate,
                channels=settings.audio_channels,
                max_chunk_bytes=settings.max_audio_chunk_bytes,
            )
            bridge = app.state.audio_bridge_factory()
            asr_session = app.state.asr_session_factory(handle_asr_event)
            if asr_session.configured:
                try:
                    await asr_session.start()
                    asr_status = "ready"
                except Exception as exc:
                    asr_status = "degraded"
                    asr_error = exc.code if isinstance(exc, ASRProviderError) else "provider_unavailable"
                    await asr_session.close()
            else:
                await asr_session.start()
            recording.pipeline_backend = bridge.backend
            with database.session_factory() as db:
                persisted = db.get(AudioRecording, recording.id)
                if persisted:
                    persisted.pipeline_backend = bridge.backend
                    db.commit()

            await send_event(
                {
                    "type": "audio.ready",
                    "recording_id": recording.id,
                    "audio": {
                        "format": "pcm_s16le",
                        "sample_rate": settings.audio_sample_rate,
                        "channels": settings.audio_channels,
                    },
                    "pipeline": {
                        "backend": bridge.backend,
                        "pipecat_installed": pipecat_available(),
                        "asr_provider": asr_session.name,
                        "asr_status": asr_status,
                        "asr_error": asr_error,
                    },
                }
            )

            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                pcm = message.get("bytes")
                if pcm is not None:
                    try:
                        metrics = recorder.append(pcm)
                        frame_info = await bridge.push(pcm)
                    except InvalidAudioChunk as exc:
                        await websocket.send_json(
                            {"type": "error", "code": "invalid_audio_chunk", "message": str(exc)}
                        )
                        continue
                    if asr_status == "ready":
                        try:
                            await asr_session.push_audio(pcm)
                        except Exception as exc:
                            retryable = not isinstance(exc, ASRProviderError) or exc.retryable
                            asr_error = exc.code if isinstance(exc, ASRProviderError) else "provider_unavailable"
                            await asr_session.close()
                            recovered = False
                            if retryable and asr_reconnects < 2:
                                asr_reconnects += 1
                                await send_event(
                                    {
                                        "type": "asr.status",
                                        "status": "recovering",
                                        "provider": asr_session.name,
                                        "error": asr_error,
                                        "attempt": asr_reconnects,
                                    }
                                )
                                replacement = app.state.asr_session_factory(handle_asr_event)
                                try:
                                    await asyncio.sleep(0.15 * asr_reconnects)
                                    await replacement.start()
                                    await replacement.push_audio(pcm)
                                    asr_session = replacement
                                    asr_status = "ready"
                                    asr_error = None
                                    recovered = True
                                    await send_event(
                                        {
                                            "type": "asr.status",
                                            "status": "ready",
                                            "provider": asr_session.name,
                                            "recovered": True,
                                        }
                                    )
                                except Exception as reconnect_exc:
                                    asr_error = reconnect_exc.code if isinstance(reconnect_exc, ASRProviderError) else "provider_unavailable"
                                    await replacement.close()
                            if not recovered:
                                asr_status = "degraded"
                                await send_event(
                                    {
                                        "type": "asr.status",
                                        "status": asr_status,
                                        "provider": asr_session.name,
                                        "error": asr_error,
                                    }
                                )
                    if recorder.chunk_count == 1 or recorder.chunk_count % 10 == 0:
                        with database.session_factory() as db:
                            persisted = db.get(AudioRecording, recording.id)
                            if persisted:
                                _update_recording_metrics(persisted, recorder)
                                db.commit()
                        await send_event(
                            {
                                "type": "audio.metrics",
                                **metrics,
                                "pipeline_backend": frame_info.backend,
                                "asr_status": asr_status,
                            }
                        )
                    continue
                text_message = message.get("text")
                if text_message:
                    try:
                        command = json.loads(text_message)
                    except (TypeError, ValueError):
                        command = {}
                    if command.get("type") == "audio.stop":
                        normal_stop = True
                        break
        except WebSocketDisconnect:
            pass
        finally:
            # Once the user stops recording, no late semantic refresh may be
            # delivered ahead of the definitive audio.stopped event.
            pending_semantic_tasks = list(semantic_tasks)
            for task in pending_semantic_tasks:
                task.cancel()
            if pending_semantic_tasks:
                await asyncio.gather(*pending_semantic_tasks, return_exceptions=True)
            if asr_session is not None:
                try:
                    if normal_stop and asr_status == "ready":
                        await asr_session.finish()
                    else:
                        await asr_session.close()
                except Exception:
                    asr_status = "degraded"
            if recorder is not None:
                recorder.close()
            if recording is not None:
                with database.session_factory() as db:
                    persisted = db.get(AudioRecording, recording.id)
                    if persisted:
                        if recorder is not None:
                            _update_recording_metrics(persisted, recorder)
                        persisted.status = "completed"
                        persisted.ended_at = utc_now()
                        db.commit()
                try:
                    await send_event(
                        {
                            "type": "audio.stopped",
                            "recording_id": recording.id,
                            "duration_ms": recorder.duration_ms if recorder else 0,
                            "asr_status": asr_status,
                        }
                    )
                except (RuntimeError, WebSocketDisconnect):
                    normal_stop = False
            if normal_stop:
                try:
                    await websocket.close()
                except RuntimeError:
                    pass

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _interview_or_404(db: Session, interview_id: str) -> InterviewRound:
    interview = db.get(InterviewRound, interview_id)
    if not interview:
        raise HTTPException(404, "interview not found")
    return interview


def _analyze_live_with_history(
    db: Session,
    intelligence: Any,
    interview: InterviewRound,
    job: Job,
    latest_segment: TranscriptSegment | None = None,
) -> dict[str, Any]:
    has_candidate_answer = bool(
        db.scalar(
            select(TranscriptSegment.id).where(
                TranscriptSegment.interview_round_id == interview.id,
                TranscriptSegment.is_final.is_(True),
                TranscriptSegment.speaker_role == "candidate",
            ).limit(1)
        )
    )
    if interview.status != "in_progress" or not has_candidate_answer:
        return {
            "provider": "waiting_for_candidate_answer",
            "mode": "waiting",
            "analysis_mode": interview.interview_mode,
            "availability": (
                "waiting_for_start"
                if interview.status in {"planned", "ready"}
                else "waiting_for_candidate_answer"
            ),
            "coverage": [],
            "question_coverage": [],
            "question_coverage_summary": {
                "total": 0,
                "unanswered": 0,
                "shallow": 0,
                "evidenced": 0,
            },
            "active_question_id": None,
            "suggestions": [],
            "suggestion_history": [],
            "current_suggestion_ids": [],
            "evidence": [],
            "evidence_digest": empty_evidence_digest(),
            "transcript_segment_count": 0,
        }
    analysis = intelligence.analyze_live(db, interview, job, latest_segment)
    analysis = merge_suggestion_history(interview, analysis)
    evidence_items = db.scalars(
        select(EvidenceItem)
        .where(EvidenceItem.interview_round_id == interview.id)
        .order_by(EvidenceItem.created_at)
    ).all()
    dimensions = (
        []
        if interview.interview_mode == "conversation"
        else round_evaluation_dimensions(db, interview, job)
    )
    analysis["evidence_digest"] = build_evidence_digest(
        evidence_items,
        dimensions,
        list(analysis.get("question_coverage") or []),
    )
    return analysis


def _analysis_update_is_meaningful(
    previous: dict[str, Any], current: dict[str, Any]
) -> bool:
    """Ignore suggestion-history timestamps while preserving actual AI updates."""
    visible_keys = (
        "model_assistance",
        "coverage",
        "question_coverage",
        "active_question_id",
        "suggestions",
        "evidence",
        "evidence_digest",
    )
    return any(previous.get(key) != current.get(key) for key in visible_keys)


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


def _encode_signed_payload(payload: dict[str, Any], secret: str) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _decode_signed_payload(value: str | None, secret: str) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < int(utc_now().timestamp()):
        return None
    return payload


def _set_session_cookie(response: JSONResponse | RedirectResponse, identity: UserIdentity, settings: Settings) -> None:
    max_age = settings.session_hours * 3600
    token = _encode_signed_payload(
        {"open_id": identity.open_id, "exp": int((utc_now() + timedelta(seconds=max_age)).timestamp())},
        settings.session_secret,
    )
    response.set_cookie(
        "interview_session",
        token,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        max_age=max_age,
    )


def _user_payload(identity: UserIdentity) -> dict[str, Any]:
    return {
        "id": identity.id,
        "open_id": identity.open_id,
        "display_name": identity.display_name,
        "email": identity.email,
        "avatar_url": identity.avatar_url,
        "role": identity.role,
        "identity_source": identity.identity_source,
    }


def _seed_development_users(db: Session) -> None:
    users = [
        ("dev-admin", "开发环境管理员", "admin"),
        ("dev-hr", "开发环境 HR", "hr"),
        ("dev-business", "王经理", "interviewer"),
        ("dev-ceo", "陈总", "interviewer"),
    ]
    changed = False
    for open_id, display_name, role in users:
        if not db.scalar(select(UserIdentity).where(UserIdentity.open_id == open_id)):
            db.add(
                UserIdentity(
                    id=new_id("user"),
                    identity_source="development",
                    open_id=open_id,
                    display_name=display_name,
                    role=role,
                    active=True,
                )
            )
            changed = True
    if changed:
        db.commit()


def _backfill_legacy_assignments(db: Session) -> int:
    """Bind legacy rows only when exactly one active user name matches."""
    identities = db.scalars(
        select(UserIdentity).where(UserIdentity.active.is_(True))
    ).all()
    identities_by_name: dict[str, list[UserIdentity]] = {}
    for identity in identities:
        identities_by_name.setdefault(identity.display_name.strip(), []).append(identity)
    rounds = db.scalars(select(InterviewRound)).all()
    created = 0
    for interview in rounds:
        if db.scalar(
            select(InterviewAssignment).where(
                InterviewAssignment.interview_round_id == interview.id
            )
        ):
            continue
        matched: dict[str, UserIdentity] = {}
        for name in interview.interviewer_names:
            candidates = identities_by_name.get(name.strip(), [])
            if len(candidates) == 1:
                matched[candidates[0].open_id] = candidates[0]
        for identity in matched.values():
            db.add(
                InterviewAssignment(
                    id=new_id("assign"),
                    interview_round_id=interview.id,
                    user_open_id=identity.open_id,
                    display_name=identity.display_name,
                    assigned_by_open_id=None,
                )
            )
            created += 1
    if created:
        db.commit()
    return created


def _upsert_feishu_user(db: Session, data: dict[str, Any], settings: Settings) -> UserIdentity:
    open_id = data.get("open_id") or data.get("sub")
    if not open_id:
        raise HTTPException(502, "Feishu user response did not contain open_id")
    identity = db.scalar(select(UserIdentity).where(UserIdentity.open_id == open_id))
    role = "admin" if open_id in settings.feishu_admin_open_ids else "hr" if open_id in settings.feishu_hr_open_ids else "interviewer"
    if not identity:
        identity = UserIdentity(
            id=new_id("user"),
            identity_source="feishu",
            open_id=open_id,
            display_name=data.get("name") or data.get("en_name") or "飞书用户",
            role=role,
        )
        db.add(identity)
    identity.union_id = data.get("union_id")
    identity.display_name = data.get("name") or identity.display_name
    identity.email = data.get("email") or identity.email
    identity.avatar_url = data.get("avatar_url") or data.get("picture")
    identity.role = role
    identity.last_login_at = utc_now()
    identity.active = True
    db.commit()
    return identity


def _require_role(request: Request, *roles: str) -> dict[str, Any]:
    if request.state.user.get("role") not in roles:
        detail = (
            "this action requires administrator role"
            if roles == ("admin",)
            else "this action requires HR or administrator role"
        )
        raise HTTPException(403, detail)
    return request.state.user


def _can_view_report(
    db: Session,
    user: dict[str, Any],
    report: InterviewReportVersion,
    audience: str,
) -> bool:
    if audience not in {"management", "hr_archive"}:
        return False
    if user.get("role") in {"hr", "admin"}:
        return True
    if audience != "management" or report.status != "locked":
        return False
    round_ids = list(
        db.scalars(
            select(InterviewRound.id).where(
                InterviewRound.application_id == report.application_id
            )
        ).all()
    )
    if not round_ids:
        return False
    assigned = db.scalar(
        select(InterviewAssignment.id).where(
            InterviewAssignment.interview_round_id.in_(round_ids),
            InterviewAssignment.user_open_id == user.get("open_id"),
        )
    )
    return assigned is not None


def _can_access_interview_round(
    db: Session,
    user: dict[str, Any],
    interview_round_id: str,
) -> bool:
    if user.get("role") in {"hr", "admin"}:
        return True
    assigned = db.scalar(
        select(InterviewAssignment.id).where(
            InterviewAssignment.interview_round_id == interview_round_id,
            InterviewAssignment.user_open_id == user.get("open_id"),
        )
    )
    return assigned is not None


def _resume_item_payload(item: ResumeImportItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "filename": item.filename,
        "status": item.status,
        "recognized": item.recognized_payload,
        "duplicate_candidate_id": item.duplicate_candidate_id,
        "candidate_id": item.candidate_id,
        "error_message": item.error_message,
    }


def _find_duplicate_candidate(db: Session, fields: dict[str, Any]) -> str | None:
    phone = fields.get("phone")
    email = (fields.get("email") or "").lower()
    if not phone and not email:
        return None
    profiles = db.scalars(select(CandidateProfile)).all()
    for profile in profiles:
        data = profile.structured_data or {}
        if phone and data.get("phone") == phone:
            return profile.candidate_id
        if email and (data.get("email") or "").lower() == email:
            return profile.candidate_id
    return None


def _replace_assignments(
    db: Session,
    interview: InterviewRound,
    open_ids: list[str],
    names: list[str],
    assigned_by_open_id: str | None,
) -> None:
    existing = db.scalars(
        select(InterviewAssignment).where(
            InterviewAssignment.interview_round_id == interview.id
        )
    ).all()
    for item in existing:
        db.delete(item)
    for open_id, name in zip(open_ids, names):
        identity = db.scalar(
            select(UserIdentity).where(
                UserIdentity.open_id == open_id,
                UserIdentity.active.is_(True),
            )
        )
        if not identity:
            raise HTTPException(422, f"interviewer identity is unknown or inactive: {open_id}")
        db.add(
            InterviewAssignment(
                id=new_id("assign"),
                interview_round_id=interview.id,
                user_open_id=open_id,
                display_name=identity.display_name,
                assigned_by_open_id=assigned_by_open_id,
            )
        )


def _managed_round_payload(db: Session, interview: InterviewRound) -> dict[str, Any]:
    assignments = db.scalars(
        select(InterviewAssignment).where(
            InterviewAssignment.interview_round_id == interview.id
        )
    ).all()
    return {
        "id": interview.id,
        "round_type": interview.round_type,
        "interview_mode": interview.interview_mode,
        "scheduled_at": interview.scheduled_at,
        "meeting_source": interview.meeting_source,
        "status": interview.status,
        "interviewer_names": interview.interviewer_names,
        "assignments": [
            {"open_id": item.user_open_id, "display_name": item.display_name}
            for item in assignments
        ],
    }


def _application_deletion_status(
    db: Session,
    application_id: str,
    rounds: list[InterviewRound] | None = None,
) -> dict[str, Any]:
    """Return the smallest safe deletion decision for an HR task.

    A task can only be hard-deleted while its rounds are still planned and no
    interview material has been produced. Historical business actions remain
    auditable; callers should cancel rather than delete those tasks.
    """
    if rounds is None:
        rounds = db.scalars(
            select(InterviewRound).where(InterviewRound.application_id == application_id)
        ).all()
    if any(
        item.status in {"in_progress", "completed", "cancelled"}
        or item.started_at is not None
        or item.ended_at is not None
        for item in rounds
    ):
        return {
            "allowed": False,
            "reason": "该任务已经开始、完成或取消过面试，不能直接删除；如不再继续，请使用“取消本轮”。",
        }

    round_ids = [item.id for item in rounds]
    if round_ids:
        round_materials = (
            (TranscriptSegment, TranscriptSegment.interview_round_id, "逐字稿"),
            (AudioRecording, AudioRecording.interview_round_id, "录音"),
            (EvidenceItem, EvidenceItem.interview_round_id, "证据"),
            (Scorecard, Scorecard.interview_round_id, "评价"),
            (InterviewerQualityReview, InterviewerQualityReview.interview_round_id, "面试质量复盘"),
            (InterviewQuestionProgress, InterviewQuestionProgress.interview_round_id, "问题记录"),
            (SpeakerRoleMapping, SpeakerRoleMapping.interview_round_id, "说话人记录"),
            (KnowledgeProposal, KnowledgeProposal.source_round_id, "知识提案"),
        )
        for model, round_column, label in round_materials:
            if db.scalar(
                select(model.id)
                .where(round_column.in_(round_ids))
                .limit(1)
            ):
                return {
                    "allowed": False,
                    "reason": f"该任务已经产生{label}，不能直接删除；如不再继续，请使用“取消本轮”。",
                }

    if db.scalar(
        select(InterviewReportVersion.id)
        .where(InterviewReportVersion.application_id == application_id)
        .limit(1)
    ):
        return {
            "allowed": False,
            "reason": "该任务已经产生报告，不能直接删除；如不再继续，请使用“取消本轮”。",
        }
    return {
        "allowed": True,
        "reason": "任务尚未开始且未产生正式面试数据，可以安全删除。",
    }


def _websocket_user_can_access(
    websocket: WebSocket,
    interview_id: str,
    database: Database,
    settings: Settings,
) -> bool:
    if settings.environment == "test":
        return True
    session = _decode_signed_payload(
        websocket.cookies.get("interview_session"), settings.session_secret
    )
    if not session:
        return False
    with database.session_factory() as db:
        identity = db.scalar(
            select(UserIdentity).where(
                UserIdentity.open_id == session.get("open_id"),
                UserIdentity.active.is_(True),
            )
        )
        if not identity:
            return False
        if identity.role in {"hr", "admin"}:
            return True
        return bool(
            db.scalar(
                select(InterviewAssignment).where(
                    InterviewAssignment.interview_round_id == interview_id,
                    InterviewAssignment.user_open_id == identity.open_id,
                )
            )
        )


def _context_or_404(
    db: Session, interview_id: str
) -> tuple[InterviewRound, Application, Candidate, Job]:
    interview = _interview_or_404(db, interview_id)
    application = db.get(Application, interview.application_id)
    if not application:
        raise HTTPException(500, "interview application is missing")
    candidate = db.get(Candidate, application.candidate_id)
    job = db.get(Job, application.job_id)
    if not candidate or not job:
        raise HTTPException(500, "candidate or job is missing")
    return interview, application, candidate, job


def _stage_for_round(round_type: str) -> str:
    return {
        "business": "business_interview",
        "hr": "hr_interview",
        "ceo": "ceo_interview",
        "custom": "supplementary_interview",
    }.get(round_type, "interview")


def _advance_application_stage(
    db: Session,
    application: Application,
    submitted_round_id: str,
) -> None:
    """Move workflow status to the next configured round without making a hiring decision."""
    rounds = list(
        db.scalars(
            select(InterviewRound)
            .where(
                InterviewRound.application_id == application.id,
                InterviewRound.status != "cancelled",
            )
            .order_by(InterviewRound.scheduled_at, InterviewRound.created_at)
        ).all()
    )
    for interview in rounds:
        if interview.id == submitted_round_id:
            is_submitted = True
        else:
            scorecard = db.scalar(
                select(Scorecard).where(Scorecard.interview_round_id == interview.id)
            )
            is_submitted = bool(scorecard and scorecard.status == "submitted")
        if interview.status != "completed" or not is_submitted:
            application.current_stage = _stage_for_round(interview.round_type)
            return
    application.current_stage = "final_review"


def _admin_job_payload(db: Session, job: Job) -> dict[str, Any]:
    versions = list(
        db.scalars(
            select(TalentProfileVersion)
            .where(TalentProfileVersion.job_id == job.id)
            .order_by(TalentProfileVersion.version_number.desc())
        ).all()
    )
    active = next((item for item in versions if item.status == "active"), None)
    draft = next((item for item in versions if item.status == "draft"), None)
    application_count = db.scalar(
        select(func.count()).select_from(Application).where(Application.job_id == job.id)
    ) or 0
    return {
        "id": job.id,
        "title": job.title,
        "source_job_code": job.source_job_code,
        "jd_text": job.jd_text,
        "jd_character_count": len(job.jd_text or ""),
        "status": job.status,
        "application_count": application_count,
        "semantic_profile": job.semantic_profile or build_local_job_semantic_profile(
            job.title, job.jd_text
        ),
        "profile": {
            "state": "active" if active else "draft" if draft else "missing",
            "active_version": active.version_label if active else None,
            "draft_version": draft.version_label if draft else None,
            "version_count": len(versions),
        },
        "created_at": job.created_at,
    }


def _find_job_conflict(
    db: Session,
    *,
    title: str,
    source_job_code: str | None,
    exclude_job_id: str | None = None,
) -> Job | None:
    normalized_title = title.strip().casefold()
    normalized_code = source_job_code.strip().casefold() if source_job_code else None
    jobs = db.scalars(select(Job).where(Job.status != "demo")).all()
    for job in jobs:
        if exclude_job_id and job.id == exclude_job_id:
            continue
        same_code = bool(
            normalized_code
            and job.source_job_code
            and job.source_job_code.strip().casefold() == normalized_code
        )
        same_title_without_code = (
            job.title.strip().casefold() == normalized_title
            and not normalized_code
            and not job.source_job_code
        )
        if same_code or same_title_without_code:
            return job
    return None


def _build_interview_plan(
    db: Session,
    intelligence: Any,
    interview: InterviewRound,
    job: Job,
) -> dict[str, Any]:
    plan = build_plan(db, interview, job.competencies)
    refine = getattr(intelligence, "refine_interview_plan", None)
    return refine(db, interview, job, plan) if callable(refine) else plan


def _refresh_planned_job_interviews(
    db: Session, job: Job, intelligence: Any
) -> tuple[int, int]:
    application_ids = list(
        db.scalars(select(Application.id).where(Application.job_id == job.id)).all()
    )
    if not application_ids:
        return 0, 0
    interviews = list(
        db.scalars(
            select(InterviewRound).where(
                InterviewRound.application_id.in_(application_ids),
                InterviewRound.status.in_(["planned", "in_progress"]),
            )
        ).all()
    )
    refreshed = 0
    frozen = 0
    for interview in interviews:
        if interview.status == "in_progress":
            frozen += 1
            continue
        interview.plan_payload = _build_interview_plan(
            db, intelligence, interview, job
        )
        interview.plan_version = interview.plan_payload["version"]
        refreshed += 1
    return refreshed, frozen


def _find_reusable_job(
    db: Session,
    *,
    title: str,
    source_job_code: str | None,
) -> Job | None:
    normalized_code = source_job_code.strip() if source_job_code else None
    if normalized_code:
        matched = db.scalar(
            select(Job)
            .where(Job.source_job_code == normalized_code)
            .order_by(Job.created_at.desc())
        )
        if matched:
            return matched
    normalized_title = title.strip().casefold()
    return next(
        (
            item
            for item in db.scalars(
                select(Job).where(Job.status != "archived").order_by(Job.created_at.desc())
            ).all()
            if item.title.strip().casefold() == normalized_title
        ),
        None,
    )


def _persist_segment(
    db: Session, interview: InterviewRound, payload: TranscriptSegmentCreate
) -> TranscriptSegment:
    if interview.status != "in_progress":
        raise HTTPException(409, "interview must be in progress")
    segment = TranscriptSegment(
        id=new_id("seg"),
        interview_round_id=interview.id,
        speaker_role=payload.speaker_role,
        speaker_confidence=payload.speaker_confidence,
        provider_speaker_id=payload.provider_speaker_id,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        text_raw=payload.text,
        is_final=payload.is_final,
    )
    db.add(segment)
    db.flush()
    return segment


def _question_progress_payload(interview: InterviewRound, db: Session) -> dict[str, Any]:
    questions = (interview.plan_payload or {}).get("questions", [])
    progress = db.scalars(
        select(InterviewQuestionProgress).where(
            InterviewQuestionProgress.interview_round_id == interview.id
        )
    ).all()
    by_id = {item.question_id: item for item in progress}
    items = [
        {
            "question_id": question.get("id"),
            "required": bool(question.get("required")),
            "asked": bool(by_id.get(question.get("id")) and by_id[question.get("id")].asked),
            "asked_by": by_id[question.get("id")].asked_by if question.get("id") in by_id else None,
            "asked_at": by_id[question.get("id")].asked_at if question.get("id") in by_id else None,
        }
        for question in questions
    ]
    required = [item for item in items if item["required"]]
    asked = sum(item["asked"] for item in required)
    return {
        "interview_id": interview.id,
        "items": items,
        "required_total": len(required),
        "required_asked": asked,
        "required_complete": asked == len(required),
    }


def _interviewer_review_payload(
    interview: InterviewRound,
    review: InterviewerQualityReview | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": review.id if review else None,
        "interview_round_id": interview.id,
        "interviewer_names": interview.interviewer_names,
        "automated_metrics": metrics,
        "human_ratings": review.human_ratings if review else {},
        "notes": review.notes if review else None,
        "status": review.status if review else "ai_draft",
        "reviewed_by": review.reviewed_by if review else None,
        "reviewed_at": review.reviewed_at if review else None,
    }


def _upsert_scorecard_draft(
    db: Session,
    intelligence: Any,
    interview: InterviewRound,
    job: Job,
) -> Scorecard:
    draft = intelligence.draft_scorecard(db, interview, job)
    _assert_scores_have_evidence(draft["ai_scores"])
    scorecard = db.scalar(
        select(Scorecard).where(Scorecard.interview_round_id == interview.id)
    )
    if not scorecard:
        scorecard = Scorecard(id=new_id("score"), interview_round_id=interview.id)
        db.add(scorecard)

    prior_recommendation = dict(scorecard.recommendation or {})
    recommendation = dict(draft["recommendation"])
    if draft.get("model_assistance"):
        recommendation["model_assistance"] = draft["model_assistance"]
    for preserved_key in ("human_decision", "knowledge_learning"):
        if preserved_key in prior_recommendation:
            recommendation[preserved_key] = prior_recommendation[preserved_key]

    scorecard.rubric_version = draft["rubric_version"]
    scorecard.ai_scores = draft["ai_scores"]
    scorecard.recommendation = recommendation
    scorecard.next_round_questions = draft["next_round_questions"]
    if scorecard.status != "submitted":
        scorecard.status = "ai_draft"
    db.flush()
    return scorecard


def _assert_scores_have_evidence(scores: list[dict[str, Any]]) -> None:
    invalid = [item["competency_id"] for item in scores if item.get("score") is not None and not item.get("evidence_ids")]
    if invalid:
        raise HTTPException(
            500,
            f"provider violated evidence-first policy for competencies: {', '.join(invalid)}",
        )


def _talent_profile_center_payload(db: Session, job: Job) -> dict[str, Any]:
    versions = list(
        db.scalars(
            select(TalentProfileVersion)
            .where(TalentProfileVersion.job_id == job.id)
            .order_by(TalentProfileVersion.version_number.desc())
        ).all()
    )
    active = next((item for item in versions if item.status == "active"), None)
    draft = next((item for item in versions if item.status == "draft"), None)
    outcome_samples = collect_outcome_summary(db, job)
    outcome_samples.pop("sample_signature", None)
    return {
        "job": {
            "id": job.id,
            "title": job.title,
            "source_job_code": job.source_job_code,
            "competency_model_version": job.competency_model_version,
        },
        "active_version": version_payload(active) if active else None,
        "draft_version": version_payload(draft) if draft else None,
        "versions": [version_payload(item) for item in versions],
        "outcome_samples": outcome_samples,
        "governance": {
            "initial_baseline": "HR 可基于 JD 和三轮标准能力创建首版",
            "automatic_update_threshold": "至少 3 份进入录用审批的样本",
            "activation": "所有新版本必须由 HR 人工确认",
            "boundary": "招聘决策信号不等同于入职绩效，后续应接入试用期结果校准",
        },
    }


def _publish_talent_profile(
    version: TalentProfileVersion,
    job: Job,
    settings: Settings,
    published_by: str,
) -> None:
    reviewed_at = version.approved_at or utc_now()
    release_version = version.release_version or (
        f"talent-profile-{version.version_label}-{version.id[-8:]}"
    )
    version.release_version = release_version
    version.publication_error = None
    try:
        vault_dir = settings.resolved_knowledge_vault_dir
        if vault_dir is None:
            raise KnowledgePublishError("知识库路径尚未配置")
        result = publish_proposal(
            vault_dir=vault_dir,
            vault_name=settings.knowledge_vault_name,
            proposal_id=version.id,
            proposal_type="profile",
            payload=profile_payload_for_publication(version, job),
            rationale=(
                f"{version.change_summary} 该版本由 HR 人工确认生效；"
                "录用样本仅作为招聘决策信号，不替代入职绩效验证。"
            ),
            source_round_id=f"job-profile:{job.id}",
            round_type="business,hr,ceo",
            job_code=job.source_job_code,
            job_title=job.title,
            reviewed_by=published_by,
            reviewed_at=reviewed_at,
            release_version=release_version,
        )
    except (KnowledgePublishError, OSError) as error:
        version.publication_status = "failed"
        version.publication_error = str(error)
    else:
        version.publication_status = "published"
        version.relative_path = result.relative_path
        version.content_hash = result.content_hash
        version.obsidian_uri = result.obsidian_uri


def _publish_company_profile(
    version: CompanyProfileVersion,
    settings: Settings,
    published_by: str,
) -> None:
    reviewed_at = version.approved_at or utc_now()
    release_version = version.release_version or (
        f"company-profile-{version.version_label}-{version.id[-8:]}"
    )
    version.release_version = release_version
    version.publication_error = None
    try:
        vault_dir = settings.resolved_knowledge_vault_dir
        if vault_dir is None:
            raise KnowledgePublishError("知识库路径尚未配置")
        result = publish_proposal(
            vault_dir=vault_dir,
            vault_name=settings.knowledge_vault_name,
            proposal_id=version.id,
            proposal_type="profile",
            payload=company_profile_payload_for_publication(version),
            rationale=(
                f"{version.change_summary} 该公司通用标准由 HR 人工确认生效；"
                "岗位画像与面试题只能继承当前生效版本。"
            ),
            source_round_id=f"company-profile:{version.id}",
            round_type="business,hr,ceo",
            job_code=None,
            job_title=f"{version.company_name}公司通用标准",
            reviewed_by=published_by,
            reviewed_at=reviewed_at,
            release_version=release_version,
        )
    except (KnowledgePublishError, OSError) as error:
        version.publication_status = "failed"
        version.publication_error = str(error)
    else:
        version.publication_status = "published"
        version.relative_path = result.relative_path
        version.content_hash = result.content_hash
        version.obsidian_uri = result.obsidian_uri


def _attempt_knowledge_publish(
    db: Session,
    proposal: KnowledgeProposal,
    settings: Settings,
    published_by: str,
) -> dict[str, Any]:
    publication = db.scalar(
        select(KnowledgePublication).where(
            KnowledgePublication.proposal_id == proposal.id
        )
    )
    if publication and publication.status == "published":
        return _proposal_payload(proposal, publication)

    reviewed_at = proposal.reviewed_at or utc_now()
    if not publication:
        publication = KnowledgePublication(
            id=new_id("kpub"),
            proposal_id=proposal.id,
            release_version=(
                f"knowledge-{reviewed_at:%Y%m%dT%H%M%SZ}-{proposal.id[-8:]}"
            ),
            status="pending",
            published_by=published_by,
        )
        db.add(publication)
        db.flush()
    else:
        publication.published_by = published_by
        publication.error_message = None

    interview, _, _, job = _context_or_404(db, proposal.source_round_id)
    vault_dir = settings.resolved_knowledge_vault_dir
    try:
        if vault_dir is None:
            raise KnowledgePublishError("知识库路径尚未配置")
        result = publish_proposal(
            vault_dir=vault_dir,
            vault_name=settings.knowledge_vault_name,
            proposal_id=proposal.id,
            proposal_type=proposal.proposal_type,
            payload=proposal.payload or {},
            rationale=proposal.rationale,
            source_round_id=proposal.source_round_id,
            round_type=interview.round_type,
            job_code=job.source_job_code,
            job_title=job.title,
            reviewed_by=published_by,
            reviewed_at=reviewed_at,
            release_version=publication.release_version,
        )
    except (KnowledgePublishError, OSError) as error:
        publication.status = "failed"
        publication.error_message = str(error)
        proposal.status = "approved_for_publish"
    else:
        publication.status = "published"
        publication.relative_path = result.relative_path
        publication.content_hash = result.content_hash
        publication.obsidian_uri = result.obsidian_uri
        publication.error_message = None
        publication.published_at = utc_now()
        proposal.status = "published"
    db.commit()
    return _proposal_payload(proposal, publication)


def _publication_payload(publication: KnowledgePublication | None) -> dict[str, Any] | None:
    if not publication:
        return None
    return {
        "id": publication.id,
        "release_version": publication.release_version,
        "status": publication.status,
        "relative_path": publication.relative_path,
        "content_hash": publication.content_hash,
        "obsidian_uri": publication.obsidian_uri,
        "error_message": publication.error_message,
        "published_by": publication.published_by,
        "published_at": publication.published_at,
        "created_at": publication.created_at,
    }


def _proposal_payload(
    proposal: KnowledgeProposal,
    publication: KnowledgePublication | None = None,
) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "source_round_id": proposal.source_round_id,
        "proposal_type": proposal.proposal_type,
        "payload": proposal.payload,
        "rationale": proposal.rationale,
        "status": proposal.status,
        "reviewed_by": proposal.reviewed_by,
        "reviewed_at": proposal.reviewed_at,
        "created_at": proposal.created_at,
        "publication": _publication_payload(publication),
    }


def _update_recording_metrics(recording: AudioRecording, recorder: AudioRecordingSession) -> None:
    recording.byte_count = recorder.byte_count
    recording.duration_ms = recorder.duration_ms
    recording.peak_level = round(recorder.peak_level, 4)
    recording.chunk_count = recorder.chunk_count


def _recording_payload(recording: AudioRecording) -> dict[str, Any]:
    return {
        "id": recording.id,
        "interview_round_id": recording.interview_round_id,
        "mime_type": recording.mime_type,
        "sample_rate": recording.sample_rate,
        "channels": recording.channels,
        "byte_count": recording.byte_count,
        "duration_ms": recording.duration_ms,
        "peak_level": recording.peak_level,
        "chunk_count": recording.chunk_count,
        "status": recording.status,
        "pipeline_backend": recording.pipeline_backend,
        "started_at": recording.started_at,
        "ended_at": recording.ended_at,
        "retention_until": recording.retention_until,
    }


app = create_app()

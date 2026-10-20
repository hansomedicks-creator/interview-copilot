"""HR-confirmed job rubrics; immutable interview snapshots and evidence-only weights."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, InterviewRound, Job, JobAssessmentVersion, new_id, utc_now
from ..schemas import AssessmentCriterion

SNAPSHOT_KEY = "job_assessment_snapshot"


def job_source(job: Job) -> dict:
    source = {"title": job.title or "", "jd_text": job.jd_text or "",
              "supplementary_requirements": (job.semantic_profile or {}).get("supplementary_requirements", "")}
    source["fingerprint"] = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return source


def versions(db: Session, job_id: str) -> list[JobAssessmentVersion]:
    return list(db.scalars(select(JobAssessmentVersion).where(JobAssessmentVersion.job_id == job_id)
                           .order_by(JobAssessmentVersion.version.desc())))


def version_payload(item: JobAssessmentVersion | None, job: Job) -> dict | None:
    if not item:
        return None
    return {"id": item.id, "version": item.version, "revision": item.revision, "status": item.status,
            "criteria": item.criteria, "recommendations": item.recommendations,
            "recommendation_status": item.recommendation_status, "manually_edited": item.manually_edited,
            "source_changed": item.source.get("fingerprint") != job_source(job)["fingerprint"],
            "total_weight": sum(c["weight"] for c in item.criteria), "updated_by": item.updated_by,
            "activated_at": item.activated_at.isoformat() if item.activated_at else None}


def assessment_payload(db: Session, job: Job) -> dict:
    items = versions(db, job.id)
    return {"active": version_payload(next((v for v in items if v.status == "active"), None), job),
            "draft": version_payload(next((v for v in items if v.status == "draft"), None), job)}


def ensure_draft(db: Session, job: Job, actor: str) -> JobAssessmentVersion:
    items = versions(db, job.id)
    draft = next((v for v in items if v.status == "draft"), None)
    if draft:
        return draft
    active = next((v for v in items if v.status == "active"), None)
    draft = JobAssessmentVersion(id=new_id("rubric"), job_id=job.id, version=(items[0].version + 1 if items else 1),
                                 criteria=deepcopy(active.criteria) if active else [],
                                 recommendations=deepcopy(active.recommendations) if active else [],
                                 recommendation_status=active.recommendation_status if active else "manual_required",
                                 manually_edited=bool(active), source=job_source(job), updated_by=actor)
    db.add(draft)
    db.flush()
    return draft


def refresh_recommendations(db: Session, job: Job, provider, actor: str) -> JobAssessmentVersion:
    draft = ensure_draft(db, job, actor)
    source = job_source(job)
    recommendations = []
    status = "manual_required"
    try:
        recommend = getattr(provider, "recommend_job_assessment", None)
        raw = recommend(source) if callable(recommend) else []
        if not 1 <= len(raw) <= 20:
            raise ValueError("empty rubric")
        recommendations = [AssessmentCriterion.model_validate(c).model_dump() for c in raw]
        source_text = source["jd_text"] + "\n" + source["supplementary_requirements"]
        if (sum(c["weight"] for c in recommendations) != 100
                or len({c["id"] for c in recommendations}) != len(recommendations)
                or any(not c["source_excerpt"] or c["source_excerpt"] not in source_text for c in recommendations)):
            raise ValueError("ungrounded or unbalanced rubric")
        status = "ai_recommended"
    except Exception:
        # No fake semantic weights. This is an explicitly labelled, editable local starter.
        items = (job.semantic_profile or {}).get("interview_dimensions", [])[:10]
        for i, item in enumerate(items):
            recommendations.append({"id": f"criterion-{i+1}", "name": str(item.get("name") or "待补充考核项")[:80],
                "definition": str(item.get("definition") or item.get("evidence_target") or "请补充本岗位的可观察行为要求")[:600],
                "weight": 100 // len(items) + (i < 100 % len(items)), "critical": False,
                "round_types": [item.get("round_type") or "business"],
                "reason": "AI 推荐不可用：暂按等权分配，须由 HR 调整后确认。", "source_excerpt": "",
                "score_anchors": {"1": "已有具体事实表明不能达到该项岗位要求。", "3": "能独立完成该项岗位的常规工作，并说明本人做法。",
                                  "5": "能处理该项工作的复杂情境，说明判断依据和适用边界。"}})
        recommendations = recommendations[-len(items):] if items else []
    draft.recommendations = recommendations
    if not draft.manually_edited:
        draft.criteria = deepcopy(recommendations)
    draft.source = source
    draft.updated_by = actor
    draft.recommendation_status = status
    db.flush()
    return draft


def activate(db: Session, job: Job, draft: JobAssessmentVersion, actor: str) -> None:
    if draft.status != "draft" or not draft.criteria or sum(c["weight"] for c in draft.criteria) != 100:
        raise ValueError("只有总权重为 100% 的非空草稿可以生效")
    if draft.source.get("fingerprint") != job_source(job)["fingerprint"]:
        raise ValueError("JD 已变化，请重新生成推荐并复核考核项")
    for old in versions(db, job.id):
        if old.status == "active":
            old.status = "superseded"
    draft.status = "active"
    draft.updated_by = actor
    draft.activated_at = utc_now()
    db.flush()


def freeze_for_interview(db: Session, interview: InterviewRound) -> dict | None:
    app = db.get(Application, interview.application_id)
    if SNAPSHOT_KEY in (interview.plan_payload or {}):
        return deepcopy(interview.plan_payload[SNAPSHOT_KEY])
    if SNAPSHOT_KEY in (app.screening_payload or {}):
        return deepcopy(app.screening_payload[SNAPSHOT_KEY])
    # Never retrofit standards into legacy interview processes (including their future rounds).
    siblings = db.scalars(select(InterviewRound).where(InterviewRound.application_id == app.id)).all()
    legacy = any(r.plan_payload or r.started_at or r.status != "planned" for r in siblings)
    active = next((v for v in versions(db, app.job_id) if v.status == "active"), None)
    snapshot = None
    if active and not legacy:
        snapshot = {"id": active.id, "version": active.version, "source": deepcopy(active.source),
                    "criteria": deepcopy(active.criteria)}
    app.screening_payload = {**(app.screening_payload or {}), SNAPSHOT_KEY: snapshot}
    return snapshot


def rubric_dimensions(snapshot: dict | None, round_type: str) -> list[dict]:
    return [{**deepcopy(c), "id": f"jd_semantic.rubric-{c['id']}", "description": c["definition"],
             "positive_evidence": [c["score_anchors"]["3"]], "risk_signals": [c["score_anchors"]["1"]],
             "evidence_requirements": ["候选人原话", "与岗位要求相关的本人行为"], "keywords": [],
             "evaluation_source": "confirmed_job_rubric"}
            for c in sorted((snapshot or {}).get("criteria", []), key=lambda c: -c["weight"])
            if round_type in c["round_types"]]


def weighted_assessment(interview: InterviewRound, scores: list[dict]) -> dict | None:
    snapshot = (interview.plan_payload or {}).get(SNAPSHOT_KEY)
    if not snapshot:
        return None
    by_id = {s.get("competency_id"): s for s in scores}
    rows, weighted, observed = [], 0.0, 0
    for c in rubric_dimensions(snapshot, interview.round_type):
        score = by_id.get(c["id"], {})
        value = score.get("score")
        valid = (score.get("assessment") == "full_conversation_semantic" and
                 bool(score.get("evidence_ids")) and
                 isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 1 <= value <= 5)
        if valid:
            weighted += value * c["weight"]
            observed += c["weight"]
        rows.append({"competency_id": c["id"], "name": c["name"], "weight": c["weight"], "critical": c["critical"],
                     "score": value if valid else None, "rationale": score.get("rationale", "尚无可核实的对话证据"),
                     "score_anchors": c["score_anchors"],
                     "evidence_ids": score.get("evidence_ids", []) if valid else []})
    total = sum(c["weight"] for c in rows)
    return {"version": snapshot["version"], "rows": rows, "score": round(weighted / observed, 2) if observed else None,
            "observed_weight": observed, "round_weight": total, "job_weight": 100,
            "provisional": observed < total, "missing_critical": [c["name"] for c in rows if c["critical"] and c["score"] is None],
            "weak_critical": [c["name"] for c in rows if c["critical"] and c["score"] is not None and c["score"] < 3],
            "policy": "仅按本轮已核实项加权（分数×权重÷已核实权重）；未问项不记零分，覆盖不足不代表整体达标。AI 不自动改变招聘阶段。"}


def apply_weighted_assessment(interview: InterviewRound, draft: dict) -> None:
    weighted = weighted_assessment(interview, draft.get("ai_scores", []))
    if weighted is None:
        return
    rec = draft["recommendation"]
    for score in draft.get("ai_scores", []):
        if score.get("assessment") != "full_conversation_semantic":
            score.update(score=None, rationale="待语义评分：本地匹配不计入岗位能力分")
    rec["weighted_assessment"] = weighted
    ai = rec.setdefault("ai_recommendation", {})
    ai["unweighted_score_reference"] = ai.get("overall_score")
    ai["overall_score"] = weighted["score"]
    # A good score on one easy item must not imply overall readiness.
    if ai.get("decision") == "advance" and (weighted["missing_critical"] or weighted["weak_critical"] or not weighted["observed_weight"]):
        ai.update(decision="supplementary_interview", label="建议补充关键验证（加权证据尚不完整）")
        rec["decision"] = ai["decision"]
    ai.update(human_confirmation_required=True, candidate_stage_changed=False)


def source_for_interview(interview: InterviewRound, job: Job) -> dict:
    snapshot = (interview.plan_payload or {}).get(SNAPSHOT_KEY)
    return snapshot["source"] if snapshot else job_source(job)


def compact_rubric(interview: InterviewRound) -> list[dict]:
    return [{key: c[key] for key in ("id", "name", "definition", "weight", "critical", "score_anchors")}
            for c in rubric_dimensions((interview.plan_payload or {}).get(SNAPSHOT_KEY), interview.round_type)]


def work_context(interview: InterviewRound, job: Job) -> dict:
    """Same authoritative requirements for dimension scoring and the final review."""
    source = source_for_interview(interview, job)
    return {"jd_reference": source["jd_text"],
            "supplementary_requirements": source.get("supplementary_requirements", ""),
            "confirmed_job_criteria": compact_rubric(interview)}

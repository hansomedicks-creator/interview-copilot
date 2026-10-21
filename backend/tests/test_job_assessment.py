from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.models import Application, Candidate, InterviewRound, Job
from app.providers.openai_compatible import OpenAICompatibleProvider, IntelligenceProviderError
from app.schemas import AssessmentCriterion, AssessmentDraftSave
from app.services import job_assessment as rubric
from app.services.evaluation_scope import round_evaluation_dimensions
from app.services.planning import build_plan

JD = "独立排查生产异常，设计验证实验并推进改进；与团队协作建立稳定交付流程。"


def criteria():
    return [{"id": "execution", "name": "本人解决生产异常", "definition": "能独立定位生产异常并解释排查顺序与判断依据",
             "weight": 70, "critical": True, "round_types": ["business"], "reason": "核心职责为本人排查交付，优先验证执行判断",
             "source_excerpt": "独立排查生产异常", "score_anchors": {"1": "已有事实显示不能定位常见异常。", "3": "可独立定位并处理常见生产异常。", "5": "能解释复杂异常的排查取舍和判断边界。"}},
            {"id": "collaboration", "name": "协作交付", "definition": "能与相关团队明确责任和交付接口",
             "weight": 30, "critical": False, "round_types": ["business", "hr"], "reason": "跨团队接口支持稳定交付",
             "source_excerpt": "与团队协作建立稳定交付流程", "score_anchors": {"1": "事实显示未能明确交付责任并造成反复遗漏。", "3": "能明确分工并完成常规交接。", "5": "能在复杂冲突中对齐责任并改进协作机制。"}}]


@pytest.fixture
def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'rubric.db'}")
    database.create_all()
    with database.session_factory() as session:
        yield session


def job_and_draft(db):
    job = Job(id="job-test", title="生产工程师", jd_text=JD, semantic_profile={})
    db.add(job); db.flush()
    draft = rubric.refresh_recommendations(db, job, SimpleNamespace(recommend_job_assessment=lambda _: criteria()), "HR")
    return job, draft


def new_interview(db, job, suffix="1", mode="structured"):
    from app.models import utc_now
    candidate = Candidate(id=f"candidate-{suffix}", display_name="合成测试人", resume_text="在工厂独立排查生产异常，改进参数与验证步骤。", retention_until=utc_now())
    application = Application(id=f"app-{suffix}", candidate_id=candidate.id, job_id=job.id, screening_payload={})
    interview = InterviewRound(id=f"round-{suffix}", application_id=application.id, round_type="business", interview_mode=mode)
    db.add_all([candidate, application, interview]); db.flush()
    interview.plan_payload = build_plan(db, interview, [])
    return interview


def test_recommendations_separate_from_manual_and_active_versions(db):
    job, draft = job_and_draft(db)
    assert draft.recommendation_status == "ai_recommended"
    manual = deepcopy(draft.criteria); manual[0]["weight"] = 60; manual[1]["weight"] = 40
    draft.criteria = manual; draft.manually_edited = True
    rubric.refresh_recommendations(db, job, SimpleNamespace(recommend_job_assessment=lambda _: criteria()), "HR")
    assert draft.criteria[0]["weight"] == 60
    assert draft.recommendations[0]["weight"] == 70
    rubric.activate(db, job, draft, "HR")
    next_draft = rubric.ensure_draft(db, job, "HR")
    assert next_draft.version == 2 and next_draft.id != draft.id
    next_draft.criteria = criteria()
    assert draft.criteria[0]["weight"] == 60


def test_unbalanced_or_stale_draft_cannot_activate(db):
    job, draft = job_and_draft(db)
    draft.criteria = [criteria()[0]]
    with pytest.raises(ValueError, match="100"):
        rubric.activate(db, job, draft, "HR")
    draft.criteria = criteria()
    job.jd_text += "新的职责"
    with pytest.raises(ValueError, match="JD"):
        rubric.activate(db, job, draft, "HR")


def test_new_process_freezes_version_and_jd_for_every_round(db):
    job, first = job_and_draft(db); rubric.activate(db, job, first, "HR")
    interview = new_interview(db, job)
    assert interview.plan_payload[rubric.SNAPSHOT_KEY]["version"] == 1
    job.jd_text += "需要重新调整工作范围。"
    draft = rubric.ensure_draft(db, job, "HR"); draft.criteria = [{**c, "weight": 50} for c in criteria()]
    rubric.activate(db, job, draft, "HR")
    rebuilt = build_plan(db, interview, [])
    assert rebuilt[rubric.SNAPSHOT_KEY]["version"] == 1
    assert rebuilt[rubric.SNAPSHOT_KEY]["source"]["jd_text"] == JD
    second_round = InterviewRound(id="round-hr", application_id=interview.application_id, round_type="hr")
    db.add(second_round); db.flush()
    second_round.plan_payload = build_plan(db, second_round, [])
    assert second_round.plan_payload[rubric.SNAPSHOT_KEY]["version"] == 1
    assert [c["weight"] for c in round_evaluation_dimensions(db, second_round, job)] == [30]
    newer = new_interview(db, job, "2")
    assert newer.plan_payload[rubric.SNAPSHOT_KEY]["version"] == 2


def test_existing_unweighted_process_not_retrofitted(db):
    job, draft = job_and_draft(db)
    interview = new_interview(db, job)
    rubric.activate(db, job, draft, "HR")
    assert build_plan(db, interview, [])[rubric.SNAPSHOT_KEY] is None


def test_legacy_started_interview_not_retrofitted(db):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    application = db.get(Application, interview.application_id)
    application.screening_payload = {}
    interview.plan_payload = {"questions": []}; interview.status = "in_progress"
    assert rubric.freeze_for_interview(db, interview) is None


@pytest.mark.parametrize("weight", [True, -1, 0, 101, 2.5, "30"])
def test_weights_are_bounded_integers(weight):
    with pytest.raises(ValidationError):
        AssessmentCriterion.model_validate({**criteria()[0], "weight": weight})


def test_duplicate_or_incomplete_criteria_rejected():
    with pytest.raises(ValidationError):
        AssessmentDraftSave(version_id="v", revision=1, criteria=[criteria()[0], criteria()[0]])
    with pytest.raises(ValidationError):
        AssessmentCriterion.model_validate({**criteria()[0], "round_types": []})
    with pytest.raises(ValidationError):
        AssessmentCriterion.model_validate({**criteria()[0], "score_anchors": {"1": "unknown"}})


def scored(key, score):
    return {"competency_id": f"jd_semantic.rubric-{key}", "score": score,
            "assessment": "full_conversation_semantic", "evidence_ids": ["synthetic-evidence"], "rationale": "基于岗位相关原话"}


def test_weighted_score_uses_semantic_evidence_and_reports_unknowns(db):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    result = rubric.weighted_assessment(interview, [scored("execution", 2), scored("collaboration", 5)])
    assert result["score"] == 2.9 and result["observed_weight"] == 100
    assert result["weak_critical"] == ["本人解决生产异常"]
    incomplete = rubric.weighted_assessment(interview, [scored("collaboration", 5)])
    assert incomplete["score"] == 5 and incomplete["observed_weight"] == 30 and incomplete["provisional"]
    assert incomplete["rows"][0]["score"] is None
    fallback = rubric.weighted_assessment(interview, [{**scored("execution", 5), "assessment": "local_rule"}])
    assert fallback["score"] is None
    assert rubric.weighted_assessment(interview, [{**scored("execution", 5), "evidence_ids": []}])["score"] is None


def test_partial_score_never_automatically_advances_stage(db):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    output = {"ai_scores": [scored("collaboration", 5)], "recommendation": {"ai_recommendation": {"decision": "advance", "overall_score": 4}}}
    rubric.apply_weighted_assessment(interview, output)
    assert output["recommendation"]["ai_recommendation"]["decision"] == "supplementary_interview"
    assert not output["recommendation"]["ai_recommendation"]["candidate_stage_changed"]
    assert db.get(Application, interview.application_id).current_stage == "interview"


def test_invalid_model_output_is_labelled_manual_required(db):
    job, draft = job_and_draft(db)
    invalid = criteria(); invalid[0]["source_excerpt"] = "虚构的JD内容"
    rubric.refresh_recommendations(db, job, SimpleNamespace(recommend_job_assessment=lambda _: invalid), "HR")
    assert draft.recommendation_status == "manual_required"
    assert draft.recommendations == []


def test_noncritical_unknown_does_not_force_question_completion(db):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    output = {"ai_scores": [scored("execution", 4)], "recommendation": {"ai_recommendation": {"decision": "advance"}}}
    rubric.apply_weighted_assessment(interview, output)
    assert output["recommendation"]["ai_recommendation"]["decision"] == "advance"
    assert output["recommendation"]["weighted_assessment"]["provisional"]


def test_report_keeps_weighted_reference_and_escapes_text():
    from app.services.reporting import _weighted_report_html
    html = _weighted_report_html({"version": 1, "score": 2.9, "observed_weight": 100, "round_weight": 100,
        "rows": [{"name": "<script>bad()</script>", "weight": 70, "score": 2, "rationale": "岗位相关行为"}], "policy": "人工决定"})
    assert "2.9 / 5" in html and "70%" in html and "人工决定" in html
    assert "<script>" not in html


def test_provider_receives_supplement_and_refuses_silent_truncation(monkeypatch):
    service = OpenAICompatibleProvider(Settings(llm_api_key="test-only", llm_model="test-model"))
    seen = {}
    def chat(**kw):
        seen.update(kw); return {"criteria": criteria()}
    monkeypatch.setattr(service, "_chat_json", chat)
    source = {"title": "生产工程师", "jd_text": JD, "supplementary_requirements": "本人动手解决问题"}
    assert service.recommend_job_assessment(source) == criteria()
    assert seen["payload"]["supplementary_requirements"] == source["supplementary_requirements"]
    with pytest.raises(ValueError):
        service.recommend_job_assessment({**source, "jd_text": "长" * 20000})


def test_rubric_propagates_to_resume_question_prompt(db, monkeypatch):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    service = OpenAICompatibleProvider(Settings(llm_api_key="test-only", llm_model="test-model"))
    seen = {}
    def chat(**kw):
        seen.update(kw); return {"questions": []}
    monkeypatch.setattr(service, "_chat_json", chat)
    job.jd_text = "后续改过的JD不可覆盖旧面试"
    service.refine_interview_plan(db, interview, job, interview.plan_payload)
    assert seen["payload"]["jd"] == JD
    assert seen["payload"]["round_dimensions"][0]["confirmed_weight"] == 70


def test_new_round_preparation_uses_inherited_frozen_source_before_plan_is_saved(db, monkeypatch):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    first = new_interview(db, job)
    job.jd_text = "后来改为完全不同的管理岗位职责"
    job.title = "新的岗位名称"
    second = InterviewRound(id="new-round", application_id=first.application_id, round_type="business")
    db.add(second); db.flush()
    plan = build_plan(db, second, [])
    assert not second.plan_payload
    service = OpenAICompatibleProvider(Settings(llm_api_key="test-only", llm_model="test-model"))
    seen = {}
    def chat(**kw):
        seen.update(kw)
        return {"questions": []}
    monkeypatch.setattr(service, "_chat_json", chat)
    service.refine_interview_plan(db, second, job, plan)
    assert seen["payload"]["jd"] == JD
    assert seen["payload"]["job_title"] == "生产工程师"


def test_weighted_verdict_adjustment_is_explained_in_report_and_recommendation(db):
    job, draft = job_and_draft(db); rubric.activate(db, job, draft, "HR")
    interview = new_interview(db, job)
    original = "候选人解释了协作交付流程，对话模型建议通过。"
    output = {"ai_scores": [scored("collaboration", 5)], "recommendation": {
        "ai_recommendation": {"decision": "advance", "rationale": original},
        "candidate_analysis": {"status": "complete", "summary": original}}}
    rubric.apply_weighted_assessment(interview, output)
    rec = output["recommendation"]
    assert rec["candidate_analysis"]["dialogue_summary"] == original
    assert rec["candidate_analysis"]["summary"].startswith("按已确认岗位标准，本轮建议补充关键验证")
    assert "本人解决生产异常" in rec["decision_adjustment"]["reason"]
    assert rec["summary"] == rec["ai_recommendation"]["rationale"] == rec["candidate_analysis"]["summary"]
    assert rec["decision"] == rec["ai_recommendation"]["decision"] == "supplementary_interview"


def test_api_draft_activate_recommend_conflict_and_auth(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}", settings=Settings(environment="development", provider_mode="mock", recording_dir=tmp_path / "recordings"))
    with TestClient(app) as client:
        app.state.intelligence.recommend_job_assessment = lambda _: criteria()
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        response = client.post("/api/v1/admin/jobs", json={"title": "合成岗位", "jd_text": JD, "supplementary_requirements": "需要亲自处理生产问题"})
        assert response.status_code == 201, response.text
        job = response.json()["job"]; url = f"/api/v1/admin/jobs/{job['id']}/assessment"
        draft = job["assessment"]["draft"]
        unbalanced = deepcopy(criteria()); unbalanced[0]["weight"] = 60
        saved = client.put(url, json={"version_id": draft["id"], "revision": draft["revision"], "criteria": unbalanced})
        assert saved.status_code == 200
        assert client.put(url, json={"version_id": draft["id"], "revision": draft["revision"], "criteria": criteria()}).status_code == 409
        d = saved.json()["draft"]
        assert client.post(url + "/activate", json={"version_id": d["id"], "revision": d["revision"], "confirmed": True}).status_code == 422
        updated = client.put(f"/api/v1/admin/jobs/{job['id']}", json={"title": "合成岗位", "jd_text": JD + "进一步关注现场判断。"})
        assert updated.status_code == 200
        assert updated.json()["job"]["assessment"]["draft"]["criteria"][0]["weight"] == 60
        d = updated.json()["job"]["assessment"]["draft"]
        balanced = client.put(url, json={"version_id": d["id"], "revision": d["revision"], "criteria": criteria()}).json()["draft"]
        active = client.post(url + "/activate", json={"version_id": balanced["id"], "revision": balanced["revision"], "confirmed": True})
        assert active.status_code == 200 and active.json()["active"]["version"] == 1
        client.post("/api/v1/auth/logout")
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-business"}).status_code == 200
        assert client.post(url + "/recommend").status_code == 403
        assert client.put(url, json={"version_id": balanced["id"], "revision": balanced["revision"], "criteria": criteria()}).status_code == 403

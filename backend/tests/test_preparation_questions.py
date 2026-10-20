from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.models import InterviewRound, Job
from app.providers.openai_compatible import OpenAICompatibleProvider, IntelligenceProviderError
from app.services.preparation_questions import validated_preparation_questions
from app.services.job_assessment import SNAPSHOT_KEY
from app.services.question_analysis import analyze_question_answers
from test_hybrid_guidance import segment

RESUME = "工单系统中补上租户隔离，在读写接口增加租户校验，并修复越权访问。"
JD = "本人负责服务部署与排障，独立处理权限隔离故障。"
DIMS = [{"id": "execution", "name": "独立处理权限故障"}]


def probe():
    return {"dimension_id": "execution", "resume_quote": RESUME,
            "experience_anchor": "租户隔离", "job_requirement_quote": "独立处理权限隔离故障",
            "question": "租户隔离只在登录时检查，为什么还挡不住跨客户查看工单？",
            "follow_up": "不要预先套用这句追问", "probe_target": "核实能否区分认证和每次数据访问的授权边界",
            "listen_for": "能把租户身份、工单归属和读写入口的校验联系起来，而非只说有登录"}


def validate(items):
    return validated_preparation_questions(items, DIMS, RESUME, JD, "business")


def test_probe_has_two_real_sources_and_no_prefabricated_followup():
    result = validate([probe()])
    assert len(result) == 1
    assert result[0]["job_requirement_quote"] in JD
    assert result[0]["experience_anchor"] in result[0]["question"]
    assert result[0]["resume_quote"] in RESUME
    assert result[0]["follow_up"] == ""
    assert not result[0]["required"]


def test_paraphrased_action_can_keep_a_real_shared_object_without_forcing_repetition():
    result = validate([{**probe(), 'experience_anchor': '工单系统中补上租户隔离',
                       'question': '工单系统缺少租户隔离，你当时是怎么发现的？'}])
    assert result
    assert result[0]['experience_anchor'] in RESUME
    assert result[0]['experience_anchor'] in result[0]['question']


def test_observed_behavior_is_retained_but_unknown_clauses_never_become_evidence():
    from app.services.utterance_quality import separate_evidence_rationale
    observed, limits = separate_evidence_rationale('本人按阻塞项重新排期。对应岗位资源取舍要求。但未说明其他项目的做法。', '需核实覆盖范围')
    assert '重新排期' in observed and '未说明' not in observed
    assert '未说明' in limits
    assert separate_evidence_rationale('未提供任何相关证据。无法判断。', '')[0] == ''


@pytest.mark.parametrize("change", [
    {"resume_quote": "不存在的虚构项目经历和成绩"}, {"experience_anchor": "虚构经历"},
    {"experience_anchor": "项目"}, {"job_requirement_quote": "带领五十人的技术团队"},
    {"dimension_id": "invented"}, {"question": "租户隔离这里面哪个判断最难？"},
    {"question": "你简历里写了租户隔离，最后有什么结果？"},
    {"question": "你凭什么可以胜任这个岗位？"}, {"probe_target": "能力"}, {"listen_for": "量化指标"},
])
def test_ungrounded_generic_or_empty_rationale_is_not_published(change):
    assert validate([{**probe(), **change}]) == []


def test_duplicate_question_and_different_anchor_same_template_are_not_two_probes():
    second = {**probe(), "experience_anchor": "读写接口", "question": probe()["question"].replace("租户隔离", "读写接口")}
    assert len(validate([probe(), deepcopy(probe()), second])) == 1


def test_resume_clue_never_becomes_an_unanswered_question_or_evidence():
    clue = {"id": "clue", "question": RESUME, "is_preparation_reference": True}
    result = analyze_question_answers([clue], [], [])
    assert result["states"] == []
    assert result["suggestions"] == []


def test_dimension_scoring_and_final_review_receive_same_frozen_jd(monkeypatch):
    service = OpenAICompatibleProvider(Settings(provider_mode="production", llm_api_key="synthetic", llm_model="synthetic"))
    interview = InterviewRound(round_type="business", interviewer_names=[], plan_payload={SNAPSHOT_KEY: {
        "source": {"jd_text": JD, "supplementary_requirements": "不设置下属，需本人处理"}, "criteria": []}})
    job = Job(title="工程师", jd_text="后来改过的JD，不应覆盖本轮要求")
    seen = []
    def fake(**kw):
        seen.append(kw)
        assert kw["payload"]["jd_reference"] == JD
        assert kw["payload"]["supplementary_requirements"] == "不设置下属，需本人处理"
        return {"competency_assessments": []} if kw["schema_name"] == "full_conversation_competency_assessment" else {}
    monkeypatch.setattr(service, "_chat_json", fake)
    rows = [segment("q", "interviewer", "你怎么检查租户隔离？"), segment("a", "candidate", RESUME)]
    service._assess_full_conversation(interview, job, [], rows)
    class DB:
        def get(self, *args):
            return None
    service._assess_free_dialogue(DB(), interview, job, rows)
    assert len(seen) == 2


def test_overlong_job_requirements_are_not_silently_cut_before_final_review(monkeypatch):
    service = OpenAICompatibleProvider(Settings(provider_mode="production", llm_api_key="synthetic", llm_model="synthetic"))
    monkeypatch.setattr(service, "_chat_json", lambda **kw: pytest.fail("oversized material must not be sent"))
    class DB:
        def get(self, *args):
            return None
    with pytest.raises(IntelligenceProviderError) as error:
        service._assess_free_dialogue(DB(), InterviewRound(round_type="business"), Job(title="工程师", jd_text=JD * 1000), [])
    assert error.value.code == "context_overflow"


@pytest.mark.parametrize("score,direction", [(4, "support"), (1.5, "negative")])
def test_end_to_end_rubric_scores_do_not_disappear_into_catalog_ids(tmp_path, monkeypatch, score, direction):
    from scripts.evaluate_job_readiness import synthetic_case
    from app.services.job_assessment import apply_weighted_assessment
    from test_candidate_analysis import dialogue_result

    database = Database(f"sqlite:///{tmp_path / 'work.db'}")
    database.create_all()
    cases = json.loads((Path(__file__).parent / 'fixtures/job_readiness_cases.json').read_text(encoding='utf-8'))['cases']
    lookup = {c['id']: c for c in cases}
    service = OpenAICompatibleProvider(Settings(provider_mode="production", llm_api_key="synthetic", llm_model="synthetic"))
    def fake(**kw):
        ids = [r['segment_id'] for r in kw['payload'].get('transcript', []) if r['speaker_role'] == 'candidate']
        if kw['schema_name'] == 'full_conversation_competency_assessment':
            assert kw['payload']['jd_reference'] == cases[2]['jd']
            return {'competency_assessments': [{'competency_id': kw['payload']['competencies'][0]['id'],
                'score': score, 'confidence': .85, 'direction': direction,
                'rationale': '候选人说明本人检查读写接口并解释权限校验顺序。但未说明其他服务的细节。',
                'limitations': '未提及其他服务的发布细节，保持未知，不扣分。', 'evidence_segment_ids': ids}]}
        if kw['schema_name'] == 'free_dialogue_job_evidence_batch':
            return dialogue_result(ids)
        return {}
    monkeypatch.setattr(service, '_chat_json', fake)
    try:
        with database.session_factory() as db:
            interview, job = synthetic_case(db, cases[2], lookup)
            # An empty list of prepared questions still permits all dialogue assessment.
            interview.plan_payload = {**interview.plan_payload, 'questions': [], 'required_questions': [], 'optional_questions': []}
            draft = service.draft_scorecard(db, interview, job)
            apply_weighted_assessment(interview, draft)
            assert [s['competency_id'] for s in draft['ai_scores']] == ['jd_semantic.rubric-core-work']
            weighted = draft['recommendation']['weighted_assessment']
            assert weighted['score'] == score
            assert weighted['observed_weight'] == 100
            assert weighted['rows'][0]['evidence_ids']
            assert draft['recommendation']['ai_recommendation']['candidate_stage_changed'] is False
            if score < 3:
                assert draft['recommendation']['ai_recommendation']['decision'] != 'advance'
    finally:
        database.engine.dispose()


def test_refresh_preparation_only_before_start_and_keeps_optional_questions(tmp_path):
    from test_api import make_client, bootstrap, acknowledge_and_start
    with make_client(tmp_path) as client:
        rid = bootstrap(client)['active_interview_id']
        result = client.post(f'/api/v1/interviews/{rid}/preparation/refresh')
        assert result.status_code == 200
        plan = result.json()['plan_payload']
        assert plan['preparation_policy'] == 'experience-role-probes-v2'
        assert all(not q['required'] for q in plan['questions'] if q.get('source') == 'resume_jd_match')
        acknowledge_and_start(client, rid)
        assert client.post(f'/api/v1/interviews/{rid}/preparation/refresh').status_code == 409
        assert client.post(f'/api/v1/interviews/{rid}/end').status_code == 200
        assert client.post(f'/api/v1/interviews/{rid}/preparation/refresh').status_code == 409


def test_pending_plan_refreshes_once_but_running_plan_is_not_replaced(tmp_path):
    from test_api import make_client, bootstrap, acknowledge_and_start
    from app.services.preparation_questions import PREPARATION_POLICY
    with make_client(tmp_path) as client:
        rid = bootstrap(client)['active_interview_id']
        def mark_legacy():
            with client.app.state.database.session_factory() as db:
                row = db.get(InterviewRound, rid)
                row.plan_payload = {**row.plan_payload, 'preparation_policy': 'legacy'}
                db.commit()
        mark_legacy()
        first = client.get(f'/api/v1/interviews/{rid}').json()['interview']['plan_payload']
        assert first['preparation_policy'] == PREPARATION_POLICY
        acknowledge_and_start(client, rid)
        mark_legacy()
        assert client.get(f'/api/v1/interviews/{rid}').json()['interview']['plan_payload']['preparation_policy'] == 'legacy'


def test_refresh_does_not_overwrite_when_another_browser_starts_during_generation(tmp_path, monkeypatch):
    import app.main as main_module
    from test_api import make_client, bootstrap
    with make_client(tmp_path) as client:
        rid = bootstrap(client)['active_interview_id']
        original = main_module._build_interview_plan
        with client.app.state.database.session_factory() as db:
            original_plan = db.get(InterviewRound, rid).plan_payload
        def race(db, intelligence, interview, job):
            result = original(db, intelligence, interview, job)
            with client.app.state.database.session_factory() as other:
                other.get(InterviewRound, rid).status = 'in_progress'
                other.commit()
            return {**result, 'test_must_not_be_saved': True}
        monkeypatch.setattr(main_module, '_build_interview_plan', race)
        assert client.post(f'/api/v1/interviews/{rid}/preparation/refresh').status_code == 409
        with client.app.state.database.session_factory() as db:
            row = db.get(InterviewRound, rid)
            assert row.status == 'in_progress'
            assert row.plan_payload == original_plan

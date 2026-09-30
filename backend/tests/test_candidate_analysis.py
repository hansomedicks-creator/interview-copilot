import json

import httpx
import pytest

from app.models import Application, InterviewRound, Job
from app.providers.openai_compatible import IntelligenceProviderError, OpenAICompatibleProvider
from app.services.candidate_analysis import SCORE_GUIDE, HIRING_BAR, validated_fit_analysis
from test_answer_review_batches import provider, result_for, rows
from test_api import make_client, bootstrap, acknowledge_and_start
from test_hybrid_guidance import segment
from app.services.suggestion_history import merge_suggestion_history


def dialogue_result(ids, sufficient=True):
    return {"sufficient_evidence": sufficient, "score": 3.8 if sufficient else None, "confidence": .85,
        "decision": "advance", "positive_evidence": ["能说明接口开发与部署操作"], "risks": [],
        "unknowns": [], "evidence_segment_ids": ids, "risk_evidence_segment_ids": [],
        "rationale": "面试官关注是否亲自完成交付。候选人说明了日志检查和部署步骤，有独立执行的具体表现。",
        "job_fit_analysis": [{"topic": "execution", "analysis": "岗位要求独立交付，候选人能解释排障步骤，体现亲自履职。", "evidence_segment_ids": ids}],
        "next_round_questions": []}


@pytest.mark.parametrize("all_fail", [False, True])
def test_hybrid_report_not_gated_by_prepared_questions_or_other_model_failures(tmp_path, monkeypatch, all_fail):
    service = provider()
    called = []
    def fake(**kw):
        called.append(kw["schema_name"])
        if all_fail or kw["schema_name"] in {"full_conversation_competency_assessment", "interview_scorecard_assistance"}:
            raise IntelligenceProviderError("timeout", "synthetic timeout")
        if kw["schema_name"] == "answer_logic_and_consistency_review":
            return result_for(kw["payload"]["transcript"])
        if kw["schema_name"] == "free_dialogue_job_evidence_batch":
            ids = [row["segment_id"] for row in kw["payload"]["transcript"] if row["speaker_role"] == "candidate"]
            return dialogue_result(ids)
        return {"pairs": []}
    monkeypatch.setattr(service, "_chat_json", fake)
    with make_client(tmp_path) as client:
        rid = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, rid)
        for i, row in enumerate(rows(1)):
            assert client.post(f"/api/v1/interviews/{rid}/segments", json={"speaker_role": row.speaker_role,
                "start_ms": i * 1000, "end_ms": i * 1000 + 999, "text": row.effective_text, "is_final": True}).status_code == 201
        with client.app.state.database.session_factory() as db:
            interview = db.get(InterviewRound, rid)
            job = db.get(Job, db.get(Application, interview.application_id).job_id)
            draft = service.draft_scorecard(db, interview, job)
            assert interview.interview_mode == "structured"
            assert interview.status == "in_progress"
        report = draft["recommendation"]["candidate_analysis"]
        ai = draft["recommendation"]["ai_recommendation"]
        assert "free_dialogue_job_evidence_batch" in called
        assert ai["required_questions_asked"] == 0
        assert ai["process_warning"] is None
        assert report["planned_question_dependency"] is False
        assert len(report["score_guide"]) == 5
        if all_fail:
            assert ai["overall_score"] is None
            assert report["status"] == "unavailable"
            assert report["error_code"] == "timeout"
            assert draft["recommendation"]["conversation_assessment"]["completed_batches"] == 0
        else:
            assert ai["overall_score"] == 3.8
            assert ai["decision"] == "advance"
            assert report["details"][0]["quotes"]
            assert "独立执行" in report["summary"]
            assert report["job_fit_analysis"][0]["title"] == "亲自履职与判断"
            assert report["assessment_policy"] == "job-readiness-strict-v1"


def test_insufficient_for_score_still_returns_specific_dialogue_analysis(monkeypatch):
    service = provider()
    monkeypatch.setattr(service, "_chat_json", lambda **kw: dialogue_result(["a0"], sufficient=False))
    class DB:
        def get(self, *args):
            return None
    judgment = service._assess_free_dialogue(DB(), InterviewRound(round_type="hr", interviewer_names=[]),
        Job(title="开发", jd_text="需要自己开发与部署"), rows(1))
    assert judgment["overall_score"] is None
    assert judgment["analysis_details"][0]["analysis"].startswith("面试官关注")
    assert judgment["analysis_details"][0]["quotes"]
    assert judgment["decision"] != "reject"


@pytest.mark.parametrize("decision,expected", [("advance", "advance"), ("reject", "hold")])
def test_synthesis_can_resolve_batch_disagreement_but_not_invent_rejection(monkeypatch, decision, expected):
    service = provider()
    result = {**dialogue_result(["a0"]), "unknowns": ["非关键细节未提及"]}
    results = [result, {**result, "evidence_segment_ids": ["a1"], "decision": "hold"}]
    output = {**dialogue_result(["a0", "a1"]), "decision": decision}
    monkeypatch.setattr(service, "_chat_json", lambda **kw: output)
    judgment = {"decision": "hold", "label": "保留", "rationale": "原观察", "confidence": .85,
                "overall_score": 3.8, "batch_status": {"failed_batches": 0}}
    service._synthesize_candidate_summary(Job(title="开发"), InterviewRound(round_type="business"), results, judgment)
    assert judgment["decision"] == expected


@pytest.mark.parametrize("kind,expected_calls,code", [("timeout", 2, "timeout"), ("connect", 2, "connection_error"), ("auth", 1, "authentication_error")])
def test_review_retries_are_bounded_and_errors_are_distinguished(kind, expected_calls, code):
    service = provider()
    calls = []
    def handler(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if kind == "connect":
            raise httpx.ConnectError("synthetic connection", request=request)
        return httpx.Response(401)
    service.client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(IntelligenceProviderError) as exc:
        service._chat_json(instructions="JSON test", payload={"transcript": []},
            schema_name="free_dialogue_job_evidence_batch", schema={"type": "object"})
    assert exc.value.code == code
    assert len(calls) == expected_calls


def test_output_truncation_retries_once_with_more_output_budget():
    service = provider()
    budgets = []
    def handler(request):
        budgets.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if len(budgets) == 1 else "stop",
            "message": {"content": '{"ok": true}'}}]})
    service.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert service._chat_json(instructions="JSON test", payload={}, schema_name="candidate_dialogue_summary",
        schema={"type": "object"}, max_tokens=1000) == {"ok": True}
    assert budgets == [1000, 2000]


def test_rubric_does_not_assign_missing_data_a_zero():
    assert len(SCORE_GUIDE) == 5
    assert "真实" not in SCORE_GUIDE[0]["range"]
    assert "已有明确事实" in SCORE_GUIDE[0]["meaning"]


def test_fit_sections_validate_topics_and_all_references():
    valid = {"topic": "core_work", "analysis": "独立完成部署符合岗位要求", "evidence_segment_ids": ["a0"]}
    sections = validated_fit_analysis([valid, valid, {**valid, "topic": "execution", "evidence_segment_ids": ["fabricated"]},
        {**valid, "topic": "transfer", "evidence_segment_ids": ["a0", "interviewer"]},
        {**valid, "topic": "gaps", "evidence_segment_ids": []}, {**valid, "topic": "batch1"}], {"a0"})
    assert len(sections) == 1
    assert sections[0]["title"] == "核心工作匹配"


def test_strict_policy_is_used_for_batch_and_summary_without_question_requirement(monkeypatch):
    service = provider()
    calls = []
    def fake(**kw):
        calls.append(kw)
        assert HIRING_BAR in kw["instructions"]
        assert "管理经验不能替代执行型岗位" in kw["instructions"]
        assert "实习、应届或培养岗" in kw["instructions"]
        assert "不因此压低能力分" in kw["instructions"]
        assert "job_fit_analysis" in kw["schema"]["required"]
        return dialogue_result(["a0"])
    monkeypatch.setattr(service, "_chat_json", fake)
    class DB:
        def get(self, *args):
            return None
    job = Job(title="部署工程师", jd_text="独立部署与排障")
    interview = InterviewRound(round_type="business", interviewer_names=[])
    result = service._assess_free_dialogue(DB(), interview, job, rows(1))
    service._synthesize_candidate_summary(job, interview, [dialogue_result(["a0"])], result)
    assert len(calls) == 2
    assert calls[-1]["payload"]["jd_reference"] == job.jd_text
    assert result["job_fit_analysis"][0]["evidence_segment_ids"] == ["a0"]


@pytest.mark.parametrize("score,sufficient,expected", [(2.4, True, 2.4), (None, False, None)])
def test_synthesized_score_supersedes_fragment_average(monkeypatch, score, sufficient, expected):
    service = provider()
    monkeypatch.setattr(service, "_chat_json", lambda **kw: {**dialogue_result(["a0"]),
        "score": score, "sufficient_evidence": sufficient, "decision": "hold"})
    judgment = {"decision": "hold", "rationale": "原观察", "confidence": .85,
                "overall_score": 4.5, "batch_status": {"failed_batches": 0}}
    service._synthesize_candidate_summary(Job(title="开发"), InterviewRound(round_type="business"), [dialogue_result(["a0"])], judgment)
    assert judgment["overall_score"] == expected
    assert judgment["decision"] != "advance"


def test_over_budget_summary_preserves_observations_without_unbounded_upload(monkeypatch):
    service = provider()
    monkeypatch.setattr(service, "_chat_json", lambda **kw: pytest.fail("must not upload oversized payload"))
    judgment = {"rationale": "已验证的具体观察"}
    service._synthesize_candidate_summary(Job(title="开发", jd_text="岗位要求" * 1000),
        InterviewRound(round_type="business"), [dialogue_result(["a0"])], judgment)
    assert judgment["summary_status"] == "batch_details_only"
    assert judgment["rationale"] == "已验证的具体观察"


@pytest.mark.parametrize("quote,summary,expected_count", [
    ("我只负责审核，代码是团队写的", "你主要把关方案，执行由团队承担", 1),
    ("我本人完成了全部开发", "你独立完成开发", 0),
])
def test_followup_paraphrase_does_not_replace_quote_validation(quote, summary, expected_count):
    proposed = {"question_id": "q", "competency_id": "execution", "evidence_gap": "ownership_boundary",
        "basis_segment_id": "a", "basis_quote": quote, "answer_summary": summary,
        "question": "碰到有问题的设计时，你会怎么修改？", "reason": "核实是否能亲自处理设计问题"}
    actual = "我只负责审核，代码是团队写的"
    result = OpenAICompatibleProvider._validated_suggestions([proposed], [], [{"id": "execution"}],
        [{"id": "q", "competency_id": "execution", "question": "你怎么负责这个项目的？"}],
        [segment("a", "candidate", actual)])
    assert len(result) == expected_count
    if result:
        assert result[0]["basis_quote"] == actual
        assert result[0]["question"].startswith(f"我理解你的意思是：{summary}。")
        assert actual not in result[0]["question"]


def test_paraphrase_changes_alone_do_not_make_followup_jump():
    interview = InterviewRound(suggestion_history=[])
    first = {"question_id": "q", "source": "llm_semantic_evidence_gap", "evidence_gap": "ownership_boundary",
        "basis_quote": "我负责审核", "evidence_segment_ids": ["a"], "answer_summary": "主要负责把关", "question": "怎么把关？"}
    merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [first]})
    merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [{**first,
        "answer_summary": "你负责方案把关", "question": "如何把关呢？"}]})
    assert interview.suggestion_history[0]["answer_summary"] == first["answer_summary"]
    assert interview.suggestion_history[0]["question"] == first["question"]
    changed = {**first, "basis_quote": "我也会改代码", "evidence_segment_ids": ["a2"], "answer_summary": "你也参与执行"}
    merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [changed]})
    assert interview.suggestion_history[0]["answer_summary"] == changed["answer_summary"]

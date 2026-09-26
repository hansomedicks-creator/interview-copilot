from datetime import datetime, timedelta, timezone

import pytest

from app.models import InterviewRound, TranscriptSegment
from app.models import Job
from app.config import Settings
from app.providers.openai_compatible import OpenAICompatibleProvider, _answer_logic_schema
from app.services.question_analysis import analyze_question_answers, assess_response_quality, interviewer_turns
from app.services.suggestion_history import merge_suggestion_history, update_suggestion_status
from test_api import make_client, bootstrap, acknowledge_and_start


def segment(sid, role, text):
    return TranscriptSegment(id=sid, speaker_role=role, text_raw=text, start_ms=0, end_ms=1,
                             is_final=True, created_at=datetime.now(timezone.utc))


def suggestion(qid, quote="团队完成了项目", gap="ownership_boundary"):
    return {"question_id": qid, "question": "你本人负责其中哪一步？", "evidence_gap": gap,
            "basis_quote": quote, "evidence_segment_ids": [f"answer-{qid}"], "priority": "high"}


def test_latest_suggestion_is_first_and_current_persists_across_refresh():
    interview = InterviewRound(suggestion_history=[])
    first = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [suggestion("A")]})
    first_id = first["current_suggestion_ids"][0]
    refreshed = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": []})
    assert refreshed["current_suggestion_ids"] == [first_id]
    waiting = merge_suggestion_history(interview, {"active_question_id": "B", "suggestions": []})
    assert waiting["current_suggestion_ids"] == []
    assert waiting["suggestion_history"][0]["status"] == "active"
    latest = merge_suggestion_history(interview, {"active_question_id": "B", "suggestions": [suggestion("B")]})
    assert latest["suggestion_history"][0]["question_id"] == "B"
    assert latest["current_suggestion_ids"] == [latest["suggestion_history"][0]["id"]]


def test_same_gap_changes_only_with_substantive_evidence_and_never_reopens_skipped():
    interview = InterviewRound(suggestion_history=[])
    original = suggestion("A")
    first = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [original]})
    sid = first["current_suggestion_ids"][0]
    merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [{**original, "question": "换一个同义问法"}]})
    assert interview.suggestion_history[0]["question"] == original["question"]
    updated = {**original, "question": "你为何这样选？", "basis_quote": "我负责部署与故障排查", "evidence_segment_ids": ["new-answer"]}
    merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [updated]})
    assert interview.suggestion_history[0]["question"] == updated["question"]
    update_suggestion_status(interview, sid, "skipped")
    assert merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [updated]})["current_suggestion_ids"] == []


def test_resolved_suggestion_enters_history():
    interview = InterviewRound(suggestion_history=[])
    first = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [suggestion("A")]})
    result = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [], "resolved_suggestion_ids": first["current_suggestion_ids"]})
    assert result["current_suggestion_ids"] == []
    assert result["suggestion_history"][0]["status"] == "resolved"


def test_latest_gap_replaces_current_without_discarding_older_high_priority():
    interview = InterviewRound(suggestion_history=[])
    merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [suggestion("A")]})
    newer = {**suggestion("A", gap="decision_basis"), "priority": "normal"}
    result = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [newer]})
    current = next(item for item in result["suggestion_history"] if item["id"] in result["current_suggestion_ids"])
    assert current["evidence_gap"] == "decision_basis"
    assert len(result["suggestion_history"]) == 2


def test_fast_local_pass_cannot_replace_semantic_card():
    interview = InterviewRound(suggestion_history=[])
    semantic = {**suggestion("A"), "source": "llm_semantic_evidence_gap", "question": "你怎样隔离不同使用者的数据？"}
    merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [semantic]})
    local = {**suggestion("A", quote="然后继续说了一个新片段"), "source": "question_gap"}
    result = merge_suggestion_history(interview, {"active_question_id": "A", "suggestions": [local]})
    assert result["suggestion_history"][0]["question"] == semantic["question"]


@pytest.mark.parametrize("question,kind", [
    ("今天过来还方便吗？", "small_talk"),
    ("好，那我们继续聊下一段经历。", "transition"),
    ("你现在是在武汉对吧？", "confirmation"),
    ("你可以先做一下自我介绍。", "instruction"),
    ("什么时候可以到岗？", "administrative"),
])
def test_small_talk_does_not_generate_followup_or_coverage(question, kind):
    rows = [segment("q", "interviewer", question), segment("a", "candidate", "是的，我觉得没有什么问题。")]
    result = analyze_question_answers([], rows, [])
    assert result["interviewer_turns"][0]["turn_type"] == kind
    assert result["states"] == result["suggestions"] == []
    assert assess_response_quality(rows)["score"] is None


def test_ad_hoc_interview_question_can_generate_followup_and_fragment_is_not_a_turn():
    rows = [segment("q", "interviewer", "你刚才说这个工具已经给团队用了"),
            segment("q2", "interviewer", "如果十个人同时使用，最容易出现什么问题？"),
            segment("a", "candidate", "我们一起弄好了，感觉用起来挺好的。")]
    result = analyze_question_answers([], rows, [])
    assert len(result["interviewer_turns"]) == 1
    assert len(result["states"]) == 1
    assert result["suggestions"][0]["basis_quote"]
    assert "十个人" in result["suggestions"][0]["source_question_text"]


def test_model_classification_can_recognize_question_without_fallback_keywords():
    rows = [segment("q", "interviewer", "如果再来一次呢"), segment("a", "candidate", "我会先确认授权范围，再让业务使用。")]
    override = {"q": {"text": "如果再来一次呢", "turn_type": "interview_question", "confidence": .95, "reason": "接续上一轮工具上线的风险取舍"}}
    result = analyze_question_answers([], rows, [], override)
    assert result["active_question_id"] == "adhoc:q"
    assert result["interviewer_turns"][0]["source"] == "semantic"


def test_short_direct_answer_not_penalized_and_quality_is_paired():
    rows = [segment("q", "interviewer", "这个项目你负责哪一部分？"), segment("a", "candidate", "我负责模型接口和部署，前端不是我做的。")]
    result = analyze_question_answers([], rows, [])
    assert result["states"][0]["status"] == "evidenced"
    quality = assess_response_quality(rows)
    assert quality["question_answer_pairs"][0]["question"] == rows[0].effective_text
    assert quality["score"] is None  # The offline fallback does not infer an IQ/length score.


@pytest.mark.parametrize("initial,target", [("conversation", "structured"), ("structured", "conversation")])
def test_mode_change_persists_before_start_even_when_schedule_has_passed(tmp_path, initial, target):
    with make_client(tmp_path) as client:
        boot = bootstrap(client)
        rid = boot["active_interview_id"]
        first = client.patch(f"/api/v1/admin/interviews/{rid}", json={"interview_mode": initial,
            "scheduled_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        assert first.status_code == 200
        before = client.get(f"/api/v1/interviews/{rid}").json()["interview"]
        changed = client.patch(f"/api/v1/admin/interviews/{rid}", json={"interview_mode": target})
        assert changed.status_code == 200
        after = client.get(f"/api/v1/interviews/{rid}").json()["interview"]
        assert after["interview_mode"] == after["plan_payload"]["interview_mode"] == target
        assert after["plan_version"] == after["plan_payload"]["version"]
        assert after["plan_version"] != before["plan_version"]
        acknowledge_and_start(client, rid)
        assert client.patch(f"/api/v1/admin/interviews/{rid}", json={"interview_mode": initial}).status_code == 409


def test_answer_quality_validates_question_answer_alignment_and_supports_clarification():
    rows = [segment("q", "interviewer", "这个项目你负责什么结果？"),
            segment("a", "candidate", "您这里说的结果，是指业务结果还是技术性能？")]
    output = {"sufficient_evidence": True, "logic_score": 4, "confidence": .8,
        "dimensions": [{"id": "clarification_behavior", "status": "coherent", "explanation": "先澄清问题含义",
                        "segment_ids": ["a"], "question_segment_ids": ["q"]}]}
    # Clarification is a valid observable response, even when not job-performance evidence.
    review = OpenAICompatibleProvider._validated_answer_logic(output, rows)
    assert review is not None
    assert review["dimensions"][0]["question_quotes"] == [rows[0].effective_text]
    output["dimensions"][0]["question_segment_ids"] = ["fabricated"]
    assert OpenAICompatibleProvider._validated_answer_logic(output, rows) is None


def test_quality_schema_contains_all_seven_observable_dimensions():
    dimensions = _answer_logic_schema()["properties"]["dimensions"]["items"]["properties"]["id"]["enum"]
    assert set(["question_comprehension", "response_relevance", "information_structure", "causal_coherence",
                "decision_reasoning", "clarification_behavior", "consistency"]).issubset(dimensions)


@pytest.mark.parametrize("score,decision,risks,risk_ids,unknowns,expected", [
    (4.9, "hold", [], [], [], "hold"),
    (4.9, "advance", [], [], ["独立部署尚待核实"], "supplementary_interview"),
    (1.2, "reject", ["明确岗位风险"], ["invented-id"], [], "hold"),
    (1.2, "reject", ["候选人明确表示不负责部署"], ["a"], [], "reject"),
    (3.2, "advance", [], [], [], "advance"),
])
def test_direction_requires_context_not_mean_score(monkeypatch, score, decision, risks, risk_ids, unknowns, expected):
    class ReadOnlyDatabase:
        def get(self, *args):
            return None

    provider = OpenAICompatibleProvider(Settings(environment="test", llm_api_key="synthetic-test",
        llm_model="synthetic", llm_base_url="https://example.invalid/v1"))
    rows = [segment("q", "interviewer", "项目部署是你负责的吗？"),
            segment("a", "candidate", "我负责模型接口，部署不是我负责的。")]
    def synthetic_review(**kwargs):
        assert kwargs["schema_name"] == "free_dialogue_job_evidence_batch"
        assert [item["segment_id"] for item in kwargs["payload"]["transcript"]] == ["q", "a"]
        return {"sufficient_evidence": True, "score": score, "confidence": .9,
                "decision": decision, "risks": risks, "risk_evidence_segment_ids": risk_ids,
                "unknowns": unknowns, "positive_evidence": ["明确说明本人职责边界"],
                "evidence_segment_ids": ["a"], "rationale": "基于实际职责和本轮回答判断"}
    monkeypatch.setattr(provider, "_chat_json", synthetic_review)
    interview = InterviewRound(application_id="synthetic", round_type="business", interviewer_names=[], status="completed")
    result = provider._assess_free_dialogue(ReadOnlyDatabase(), interview, Job(title="开发", jd_text="负责接口开发和部署"), rows)
    assert result["decision"] == expected
    assert result["overall_score"] == score
    assert result["batch_status"]["planned_question_dependency"] is False
    assert interview.status == "completed"

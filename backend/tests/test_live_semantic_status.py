"""Offline live-assistance regressions; no model calls or candidate data."""
from copy import deepcopy
from types import SimpleNamespace
import logging

import pytest

from app.config import Settings
from app.models import InterviewRound, Job, TranscriptSegment
from app.providers import openai_compatible as adapter
from app.services.suggestion_history import merge_suggestion_history, update_suggestion_status


@pytest.fixture
def live(monkeypatch):
    question = TranscriptSegment(id="q", speaker_role="interviewer", text_raw="你负责的工具如何隔离用户数据？")
    answer = TranscriptSegment(id="a", speaker_role="candidate", text_raw="我负责接口，登录以后就可以查看所有工单。")
    baseline = {"active_question_id": "adhoc:q", "interviewer_turns": [{"segment_ids": ["q"], "text": question.effective_text}],
        "question_coverage": [{"question_id": "adhoc:q", "question": question.effective_text,
            "competency_id": "interviewer_ad_hoc", "evidence_segment_ids": ["a"]}],
        "suggestions": [{"question": "其中最关键的一步是怎么运作的？", "source": "question_gap"}], "evidence": []}
    fallback_calls = []
    def fallback(db, interview, job, latest_segment=None):
        fallback_calls.append(latest_segment)
        return deepcopy(baseline)
    provider = adapter.OpenAICompatibleProvider(Settings(environment="test", llm_api_key="synthetic-secret",
        llm_model="synthetic", llm_base_url="https://example.invalid/v1"),
        fallback=SimpleNamespace(analyze_live=fallback, name="mock-rules-v0.1"))
    monkeypatch.setattr(provider, "_segments", lambda *args: [question, answer])
    monkeypatch.setattr(provider, "_persist_validated_evidence", lambda *args: None)
    monkeypatch.setattr(adapter, "active_company_profile", lambda db: None)
    interview = InterviewRound(id="synthetic-round", application_id="application", interview_mode="conversation",
        round_type="business", plan_payload={}, suggestion_history=[])
    db = SimpleNamespace(get=lambda *args: None, flush=lambda: None)
    job = Job(title="开发", jd_text="负责数据访问权限和接口")
    def run(latest=answer):
        return provider.analyze_live(db, interview, job, latest)
    yield SimpleNamespace(provider=provider, interview=interview, answer=answer, question=question,
        baseline=baseline, calls=fallback_calls, run=run)
    provider.client.close()


def success_output(live):
    return {"suggestions": [{"question_id": "adhoc:q", "competency_id": "interviewer_ad_hoc",
        "basis_segment_id": "a", "basis_quote": live.answer.effective_text,
        "answer_summary": "你实现了登录，但工单似乎没有按用户隔离",
        "question": "如果换一个用户登录，接口在哪一步检查工单归属？",
        "reason": "登录验证不等于数据授权，需要核实访问边界", "evidence_gap": "mechanism", "priority": "high"}],
        "evidence": [], "answer_complete": True}


@pytest.mark.parametrize("code", ["timeout", "connection_error", "rate_limited", "authentication_error",
    "insufficient_balance", "invalid_response", "output_truncated", "upstream_error"])
def test_failure_reason_survives_local_refresh_without_templates_or_keyword_evidence(live, monkeypatch, caplog, code):
    def fail(**kwargs):
        raise adapter.IntelligenceProviderError(code, "raw-upstream-private-body")
    monkeypatch.setattr(live.provider, "_chat_json", fail)
    with caplog.at_level(logging.INFO, logger=adapter.__name__):
        for expected in range(1, 4):
            live.answer.text_raw += "补充"
            result = live.run()
            assert result["suggestions"] == result["evidence"] == []
            assert result["suggestion_policy"] == "semantic_only"
            assert result["model_assistance"]["consecutive_failures"] == expected
        for latest in [None, live.question, live.answer]:
            refreshed = live.run(latest)
            assert refreshed["model_assistance"] == result["model_assistance"]
            assert refreshed["availability"] == "semantic_unavailable"
            assert refreshed["suggestions"] == []
    assert result["model_assistance"]["status"] == "degraded"
    assert result["model_assistance"]["error_code"] == code
    assert result["model_assistance"]["elapsed_ms"] >= 0
    assert result["model_assistance"]["checked_at"]
    assert all(call is None for call in live.calls)
    assert f"error_code={code}" in caplog.text
    for private in ["synthetic-secret", "raw-upstream-private-body", live.answer.effective_text, live.question.effective_text]:
        assert private not in caplog.text


def test_real_success_clears_error_and_history_preserves_only_current_semantic_card(live, monkeypatch):
    def fail(**kwargs):
        raise adapter.IntelligenceProviderError("timeout", "timeout")
    monkeypatch.setattr(live.provider, "_chat_json", fail)
    live.run()
    def succeed(**kwargs):
        assert all(item.get("source") == "llm_semantic_evidence_gap" for item in kwargs["payload"]["pending_suggestions"])
        return success_output(live)
    monkeypatch.setattr(live.provider, "_chat_json", succeed)
    live.answer.text_raw += "我是这样做的。"
    result = merge_suggestion_history(live.interview, live.run())
    assert result["model_assistance"]["status"] == "active"
    assert result["model_assistance"]["error_code"] is None
    assert result["model_assistance"]["consecutive_failures"] == 0
    assert result["suggestions"][0]["question"].startswith("我理解你的意思是：")
    card_id = result["current_suggestion_ids"][0]
    repeated = merge_suggestion_history(live.interview, live.run())
    assert repeated["suggestions"] == []  # Same answer is not billed again.
    assert repeated["availability"] == "semantic_ready"  # No phantom pending request after a page refresh.
    assert repeated["current_suggestion_ids"] == [card_id]
    assert repeated["suggestion_history"][0]["question"] == result["suggestions"][0]["question"]
    live.baseline["active_question_id"] = "adhoc:next"
    changed = merge_suggestion_history(live.interview, live.run(None))
    assert changed["availability"] == "semantic_analysis_pending"
    assert changed["current_suggestion_ids"] == []
    assert changed["suggestion_history"][0]["id"] == card_id
    update_suggestion_status(live.interview, card_id, "skipped")
    live.baseline["active_question_id"] = "adhoc:q"
    assert merge_suggestion_history(live.interview, live.run(None))["current_suggestion_ids"] == []


@pytest.mark.parametrize("output", [{}, {"suggestions": None, "evidence": []},
    {"suggestions": [], "evidence": {}}, {"suggestions": [], "evidence": [], "resolved_suggestions": None}])
def test_bad_response_structure_is_visible_failure_not_crash(live, monkeypatch, output):
    monkeypatch.setattr(live.provider, "_chat_json", lambda **kwargs: output)
    result = live.run()
    assert result["model_assistance"]["error_code"] == "invalid_response"
    assert result["suggestions"] == []


def test_success_without_useful_followup_does_not_fill_with_template(live, monkeypatch):
    monkeypatch.setattr(live.provider, "_chat_json", lambda **kwargs: {"suggestions": [], "evidence": []})
    result = live.run()
    assert result["availability"] == "semantic_ready"
    assert result["suggestions"] == []
    assert live.run()["suggestions"] == []


@pytest.mark.parametrize("kind", ["template", "old_question", "old_answer"])
def test_model_suggestion_cannot_be_generic_or_borrow_another_turn(live, monkeypatch, kind):
    output = success_output(live)
    if kind == "template":
        output["suggestions"][0]["question"] = "其中最关键的一步是怎么运作的？"
    elif kind == "old_question":
        output["suggestions"][0]["question_id"] = "adhoc:old"
        live.baseline["question_coverage"].append({"question_id": "adhoc:old", "question": "旧问题", "competency_id": "interviewer_ad_hoc"})
    else:
        old = TranscriptSegment(id="old", speaker_role="candidate", text_raw=live.answer.text_raw)
        monkeypatch.setattr(live.provider, "_segments", lambda *args: [old, live.question, live.answer])
        output["suggestions"][0]["basis_segment_id"] = "old"
    monkeypatch.setattr(live.provider, "_chat_json", lambda **kwargs: output)
    assert live.run()["suggestions"] == []


def test_legacy_rules_remain_stored_but_hidden_in_semantic_only_mode():
    interview = InterviewRound(suggestion_history=[])
    template = {"source": "question_gap", "question_id": "q", "evidence_gap": "mechanism", "question": "固定模板"}
    first = merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [template]})
    old_id = first["current_suggestion_ids"][0]
    current = merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [], "suggestion_policy": "semantic_only"})
    assert current["current_suggestion_ids"] == current["suggestion_history"] == []
    assert interview.suggestion_history[0]["id"] == old_id
    semantic = {**template, "source": "llm_semantic_evidence_gap", "question": "核实工单归属"}
    updated = merge_suggestion_history(interview, {"active_question_id": "q", "suggestions": [semantic], "suggestion_policy": "semantic_only"})
    assert updated["suggestion_history"][0]["question"] == "核实工单归属"

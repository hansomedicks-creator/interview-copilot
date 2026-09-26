import json

import pytest

from app.config import Settings
from app.models import InterviewRound, Job
from app.providers.openai_compatible import IntelligenceProviderError, OpenAICompatibleProvider, _bounded_json
from app.services.answer_review_batches import answer_batches, payload_size, review_payload
from test_hybrid_guidance import segment


def rows(count=10):
    return [item for i in range(count) for item in (
        segment(f"q{i}", "interviewer", f"项目{i}的部署是你负责的吗？"),
        segment(f"a{i}", "candidate", f"项目{i}我负责模型接口和部署。" + "我先检查日志，再核对配置，出现异常会回滚。" * 7))]


def provider():
    return OpenAICompatibleProvider(Settings(environment="test", llm_api_key="test-only",
        llm_model="synthetic", llm_base_url="https://example.invalid/v1", llm_max_context_chars=2000))


def result_for(transcript):
    ids = [item["segment_id"] for item in transcript if item["speaker_role"] == "candidate"]
    return {"sufficient_evidence": True, "logic_score": 4, "confidence": .8,
            "dimensions": [{"id": "causal_coherence", "status": "coherent",
                            "explanation": "描述检查顺序与处理方式", "segment_ids": ids[:4]}]}


def test_batches_preserve_every_turn_and_exact_serialized_budget():
    transcript = rows(40)
    batches, skipped = answer_batches(transcript, "开发", "business", 2000)
    assert not skipped
    assert len(batches) > 1
    assert [row.id for batch in batches for row in batch] == [row.id for row in transcript]
    for batch in batches:
        payload = review_payload("开发", "business", batch)
        assert payload_size(payload) <= 2000
        assert json.loads(_bounded_json(payload, 2000)) == payload
        ids = {row.id for row in batch}
        assert all("q" + sid[1:] in ids for sid in ids if sid.startswith("a"))


def test_streaming_fragments_remain_in_same_logical_answer():
    transcript = [segment("q", "interviewer", "这个项目你负责"),
                  segment("q2", "interviewer", "哪一步？"),
                  segment("a", "candidate", "接口"), segment("a2", "candidate", "还有部署。")]
    batches, skipped = answer_batches(transcript, "开发", "business", 2000)
    assert not skipped
    assert [[row.id for row in batch] for batch in batches] == [["q", "q2", "a", "a2"]]


def test_long_answer_is_explicitly_unreviewed_not_clipped(monkeypatch):
    transcript = rows(2)
    transcript[1].text_raw = "我负责接口部署。" * 500
    service = provider()
    def fake(**kw):
        assert "a0" not in [row["segment_id"] for row in kw["payload"]["transcript"]]
        return result_for(kw["payload"]["transcript"])
    monkeypatch.setattr(service, "_chat_json", fake)
    baseline = {"recommendation": {"response_quality": {"score": 5}}}
    service._attach_answer_logic_review(baseline, InterviewRound(round_type="business"), Job(title="开发"), transcript)
    review = baseline["recommendation"]["answer_logic_review"]
    assert review["batch_status"]["oversized_segment_ids"] == ["q0", "a0"]
    assert review["logic_score"] is None
    assert baseline["recommendation"]["response_quality"]["score"] is None


@pytest.mark.parametrize("failure", [False, True])
def test_all_batches_processed_and_partial_failure_never_keeps_old_score(monkeypatch, failure):
    service = provider()
    calls = []
    def fake(**kw):
        payload = kw["payload"]
        assert payload_size(payload) <= 2000
        assert json.loads(_bounded_json(payload, 2000)) == payload
        if kw["schema_name"] == "answer_cross_batch_candidates":
            return {"pairs": []}
        calls.append([row["segment_id"] for row in payload["transcript"]])
        if failure and len(calls) == 2:
            raise IntelligenceProviderError("timeout", "synthetic failure")
        return result_for(payload["transcript"])
    monkeypatch.setattr(service, "_chat_json", fake)
    baseline = {"recommendation": {"response_quality": {"score": 5}}}
    transcript = rows(12)
    service._attach_answer_logic_review(baseline, InterviewRound(round_type="hr"), Job(title="开发"), transcript)
    review = baseline["recommendation"]["answer_logic_review"]
    assert [sid for call in calls for sid in call] == [row.id for row in transcript]
    assert review["batch_status"]["failed_batches"] == int(failure)
    assert review["logic_score"] == (None if failure else 4)
    assert baseline["recommendation"]["response_quality"]["score"] == (None if failure else 4)
    if failure:
        assert review["batch_status"]["failed_segment_ids"] == calls[1]


@pytest.mark.parametrize("corrected", [False, True])
def test_cross_batch_nomination_must_be_rechecked_against_original_qa(monkeypatch, corrected):
    service = provider()
    transcript = rows(2)
    transcript[3].text_raw = "我刚才说得不准确，部署是同事负责，我只负责接口。" if corrected else "部署一直是同事负责，我没有做过部署。"
    reviews = [{"batch_index": i, "evidence_segment_ids": [f"a{i}"]} for i in range(2)]
    schemas = []
    def fake(**kw):
        schemas.append(kw["schema_name"])
        if kw["schema_name"] == "answer_cross_batch_candidates":
            return {"pairs": [["a0", "a1"], ["a0", "fabricated"]]}
        assert [item["segment_id"] for item in kw["payload"]["transcript"]] == ["q0", "a0", "q1", "a1"]
        result = result_for(kw["payload"]["transcript"])
        result["consistency_flags"] = [] if corrected else [{"flag_type": "ownership_shift", "severity": "medium",
            "description": "同一部署工作的负责人口径不同，需要核实", "segment_ids": ["a0", "a1"],
            "verification_question": "请澄清你和同事分别负责的部署步骤？"}]
        return result
    monkeypatch.setattr(service, "_chat_json", fake)
    flags, status = service._cross_batch_answer_review(InterviewRound(round_type="business"), Job(title="开发"), transcript, reviews)
    assert schemas == ["answer_cross_batch_candidates", "answer_logic_and_consistency_review"]
    assert len(flags) == (0 if corrected else 1)
    assert status["verified_pairs"] == 1
    assert not status["pending_checks"]


def test_distant_claims_cannot_hide_intervening_corrections(monkeypatch):
    service = provider()
    transcript = rows(20)
    reviews = [{"batch_index": 0, "evidence_segment_ids": ["a0"]}, {"batch_index": 1, "evidence_segment_ids": ["a19"]}]
    def fake(**kw):
        assert kw["schema_name"] == "answer_cross_batch_candidates"
        return {"pairs": [["a0", "a19"]]}
    monkeypatch.setattr(service, "_chat_json", fake)
    flags, status = service._cross_batch_answer_review(InterviewRound(round_type="business"), Job(title="开发"), transcript, reviews)
    assert flags == []
    assert status["pending_checks"] == 1
    assert status["pending_pairs"][0]["segment_ids"] == ["a0", "a19"]


def test_malformed_batch_response_is_failure_not_completed_review(monkeypatch):
    service = provider()
    monkeypatch.setattr(service, "_chat_json", lambda **kw: {"summary": "looks fine"})
    baseline = {"recommendation": {}}
    service._attach_answer_logic_review(baseline, InterviewRound(round_type="business"), Job(title="开发"), rows(1))
    review = baseline["recommendation"]["answer_logic_review"]
    assert review["batch_status"]["failed_batches"] == 1
    assert review["logic_score"] is None


@pytest.mark.parametrize("failure", ["malformed", "timeout"])
def test_cross_review_failure_is_visible_and_never_confirms_conflict(monkeypatch, failure):
    service = provider()
    def fake(**kw):
        if failure == "timeout":
            raise IntelligenceProviderError("timeout", "synthetic timeout")
        return {"summary": "missing pairs"}
    monkeypatch.setattr(service, "_chat_json", fake)
    flags, status = service._cross_batch_answer_review(InterviewRound(round_type="hr"), Job(title="开发"), rows(2),
        [{"batch_index": 0, "evidence_segment_ids": ["a0"]}, {"batch_index": 1, "evidence_segment_ids": ["a1"]}])
    assert flags == []
    assert status["pending_checks"] == 1
    assert status["completed_windows"] == 0


def test_valid_unknown_result_is_not_a_low_score_or_service_failure(monkeypatch):
    service = provider()
    monkeypatch.setattr(service, "_chat_json", lambda **kw: {"sufficient_evidence": False})
    baseline = {"recommendation": {}}
    service._attach_answer_logic_review(baseline, InterviewRound(round_type="business"), Job(title="开发"), rows(1))
    review = baseline["recommendation"]["answer_logic_review"]
    assert review["status"] == "insufficient_evidence"
    assert review["batch_status"]["completed_batches"] == 1
    assert review["batch_status"]["failed_batches"] == 0
    assert review["logic_score"] is None

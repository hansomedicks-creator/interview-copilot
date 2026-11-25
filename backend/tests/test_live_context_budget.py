from copy import deepcopy
import json

import pytest

from app.providers.openai_compatible import _compact_live_payload, _live_question_context, IntelligenceProviderError
from app.schemas import CompanyProfileDraftSave
from app.services.company_profile import default_company_profile_template, save_company_profile_draft, active_company_profile
from test_api import make_client


def test_live_budget_preserves_current_answer_and_weights_without_duplicates():
    payload = {"transcript": [{"segment_id": f"old-{i}", "text": "历史回答" * 100, "text_raw": "历史回答" * 100} for i in range(20)] + [
        {"segment_id": "q", "speaker_role": "interviewer", "text": "客户不接受你提出的方案，你怎么判断？"},
        {"segment_id": "a", "speaker_role": "candidate", "text": "我先确认对方是在意交付风险还是费用，再给两个不同选择。"}],
        "latest_segment_id": "a", "current_answer_context": {"question_id": "adhoc:q", "source_segment_ids": ["a"], "text": "我先确认对方是在意交付风险还是费用，再给两个不同选择。"},
        "interviewer_logical_turn": {"segment_ids": ["q"], "text": "客户不接受你提出的方案，你怎么判断？"},
        "questions": [{"question_id": "old", "question": "旧问题"}, {"question_id": "adhoc:q", "is_current": True}],
        "competencies": [{"id": "judgement", "confirmed_weight": 80, "critical": True}],
        "transcript_correction_context": {"resume_reference": "简历" * 3000, "jd_reference": "岗位" * 3000}}
    original = deepcopy(payload)
    result = _compact_live_payload(payload, 2000)
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000
    assert payload == original
    assert result["transcript"][-2:] == payload["transcript"][-2:]
    assert result["competencies"] == payload["competencies"]
    assert [q["question_id"] for q in result["questions"]] == ["adhoc:q"]
    assert "text" not in result["current_answer_context"]


def test_oversized_current_answer_fails_explicitly_instead_of_silent_clipping():
    payload = {"transcript": [{"segment_id": "a", "text": "具体回答" * 1000}], "latest_segment_id": "a", "current_answer_context": {"source_segment_ids": ["a"]}}
    with pytest.raises(IntelligenceProviderError) as error:
        _compact_live_payload(payload, 2000)
    assert error.value.code == "context_overflow"


def test_current_adhoc_question_survives_many_old_turns():
    states = [{"question_id": f"adhoc:{i}", "question": f"问题{i}"} for i in range(30)]
    result = _live_question_context([], {"active_question_id": "adhoc:0", "question_coverage": states})
    assert result[0]["question_id"] == "adhoc:0"
    assert result[0]["is_current"]


def test_hr_principles_saved_as_draft_without_automatic_activation(tmp_path):
    draft = default_company_profile_template()
    draft.update(hr_question_principles=["根据实际经历核实判断依据"], change_summary="测试 HR 提问方法配置")
    with make_client(tmp_path) as client:
        with client.app.state.database.session_factory() as db:
            version = save_company_profile_draft(db, payload=CompanyProfileDraftSave.model_validate(draft), created_by="测试 HR")
            assert version.profile_payload["hr_question_principles"] == ["根据实际经历核实判断依据"]
            assert active_company_profile(db) is None

from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.services.job_semantics import (
    build_local_job_semantic_profile,
    normalize_job_semantic_profile,
)


def test_deepseek_structured_requests_disable_default_thinking_mode():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )

    provider = OpenAICompatibleProvider(
        Settings(
            environment="test",
            provider_mode="production",
            llm_base_url="https://api.deepseek.com",
            llm_api_key="test-secret",
            llm_model="deepseek-v4-flash",
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = provider._chat_json(
        instructions="return a tiny object",
        payload={"input": "safe"},
        schema_name="tiny",
        schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    )

    assert result == {"ok": True}
    assert captured["thinking"] == {"type": "disabled"}


def test_local_semantic_profile_excludes_non_job_factors_from_questions():
    jd = (
        "负责企业客户的 AI 应用需求分析，设计交付方案并推动跨团队上线；"
        "对项目采用率和客户续约结果负责。偏好华科背景和男生。"
    )

    profile = build_local_job_semantic_profile("AI 应用工程师", jd)

    excluded = {item["text"] for item in profile["excluded_non_job_factors"]}
    questions = " ".join(item["question"] for item in profile["interview_dimensions"])
    assert {"华科", "男生"}.issubset(excluded)
    assert "华科" not in questions
    assert "男生" not in questions
    assert len(profile["interview_dimensions"]) == 7


def test_semantic_model_output_requires_exact_safe_jd_evidence():
    jd = "负责分析客户需求，设计 AI 应用方案并推动上线，对使用率和续约结果负责。"
    raw = {
        "role_mission": "帮助客户把 AI 应用转化为持续使用和续约结果。",
        "business_outcomes": ["提高客户使用率", "形成续约结果"],
        "work_scenarios": ["跨团队推进客户方案上线"],
        "interview_dimensions": [
            {
                "name": "虚构条件",
                "round_type": "business",
                "definition": "错误维度",
                "job_context": "候选人必须来自华科",
                "source_excerpt": "候选人必须来自华科",
                "evidence_target": "学校背景",
                "question": "你是不是华科毕业？",
                "follow_up": "请说明。",
            }
        ],
    }

    profile = normalize_job_semantic_profile(
        raw,
        title="AI 应用工程师",
        jd_text=jd,
        provider="test-model",
    )

    questions = " ".join(item["question"] for item in profile["interview_dimensions"])
    assert len(profile["interview_dimensions"]) == 7
    assert profile["analysis_mode"] == "local_structured_fallback"
    assert profile["model_assistance"]["error_code"] == "invalid_semantic_shape"
    assert "华科" not in questions
    assert all(item["source_excerpt"] in jd for item in profile["interview_dimensions"])


def test_semantic_profile_rejects_unwritten_targets_and_abstract_culture_fit():
    jd = "负责设计 AI 应用方案并推动上线，对客户实际使用效果负责。"
    safe_dimension = {
        "name": "方案落地",
        "round_type": "business",
        "definition": "把需求转化为可上线方案",
        "job_context": "AI 应用方案上线",
        "source_excerpt": "负责设计 AI 应用方案并推动上线",
        "evidence_target": "本人行动与上线结果",
        "question": "请讲一次你设计 AI 应用方案并推动上线的经历。",
        "follow_up": "你的行动和实际结果是什么？",
    }
    culture_dimension = {
        **safe_dimension,
        "name": "文化契合",
        "round_type": "hr",
        "question": "你如何证明自己与我们的文化契合？",
    }
    raw = {
        "role_mission": "将 AI 应用方案转化为实际使用效果。",
        "business_outcomes": ["至少上线 3 个方案", "形成可验证的客户使用效果"],
        "work_scenarios": ["在客户约束下推动方案上线"],
        "interview_dimensions": [safe_dimension, culture_dimension],
    }

    profile = normalize_job_semantic_profile(
        raw,
        title="AI 应用工程师",
        jd_text=jd,
        provider="test-model",
    )

    assert "至少上线 3 个方案" not in profile["business_outcomes"]
    assert "形成可验证的客户使用效果" in profile["business_outcomes"]
    assert all("文化契合" not in item["name"] for item in profile["interview_dimensions"])

from __future__ import annotations

import re
from typing import Any


PROFILE_VERSION = "jd-semantic-v0.1"
ROUND_COUNTS = {"business": 3, "hr": 2, "ceo": 2}

_NON_JOB_FACTOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("性别偏好", re.compile(r"男生|女生|男性|女性|限男|限女|性别.{0,4}(?:男|女)")),
    ("年龄偏好", re.compile(r"(?:年龄|岁数).{0,8}\d{2}|\d{2}\s*(?:岁|周岁)(?:以下|以内|左右)?")),
    ("婚育偏好", re.compile(r"未婚|已婚|婚育|已育|未育|生育")),
    ("籍贯或户籍偏好", re.compile(r"籍贯|户籍|本地人|外地人")),
    ("特定学校或学历光环", re.compile(r"清华|北大|华科|复旦|交大|浙大|985|211|双一流|名校")),
    ("外貌或身体条件偏好", re.compile(r"形象气质|颜值|身高\s*\d|体重\s*\d|身体健康无疾病")),
)

_UNSUPPORTED_FIT_PATTERN = re.compile(r"文化契合|价值观匹配|性格匹配|气场匹配")
_QUANTIFIED_CONSTRAINT_PATTERN = re.compile(
    r"至少|至多|不低于|不高于|不超过|\d+(?:\.\d+)?\s*(?:%|％|个|项|次|年|月|天|倍|万)"
)


def detect_non_job_factors(jd_text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for reason, pattern in _NON_JOB_FACTOR_PATTERNS:
        for match in pattern.finditer(jd_text):
            value = match.group(0).strip()
            if value and not any(item["text"] == value for item in findings):
                findings.append({"text": value, "reason": reason})
    return findings


def contains_non_job_factor(text: str) -> bool:
    return any(pattern.search(text) for _, pattern in _NON_JOB_FACTOR_PATTERNS)


def build_local_job_semantic_profile(title: str, jd_text: str) -> dict[str, Any]:
    """Structured, anti-bias fallback used only when semantic model analysis is unavailable."""
    excluded = detect_non_job_factors(jd_text)
    sentences = _safe_sentences(jd_text)
    primary = sentences[:4] or [f"承担{title}岗位职责并形成可验证结果"]
    role_mission = _role_mission(title, primary[0])
    outcomes = [_outcome_from_sentence(item) for item in primary[:4]]
    scenarios = [_scenario_from_sentence(item) for item in primary[:3]]
    dimensions: list[dict[str, Any]] = []
    templates = {
        "business": [
            ("岗位职责证据", "请讲一个最能证明你能够承担这项职责的真实案例。", "请说明当时的目标、约束、本人关键判断、行动和量化结果。", "真实项目行为与结果"),
            ("复杂问题解决", "围绕这项职责，请讲一次信息不完整或结果偏离预期的经历。", "你如何定义问题、排除假设并修正方案？", "问题定义、验证过程和复盘"),
            ("交付与复盘", "如果由你接手这项工作，最先建立的目标和验证机制是什么？", "请结合过往案例说明指标、节奏和止损条件。", "目标拆解、执行闭环和复盘能力"),
        ],
        "hr": [
            ("岗位动机与投入", "这项职责的哪些部分真正吸引你，哪些部分可能消耗你？", "请用过去一段持续投入或主动退出的经历说明。", "真实动机、投入条件和风险预期"),
            ("协作方式适配", "完成这项职责通常需要怎样的协作方式？请讲一次最相近的经历。", "相关方目标不一致时，你如何建立共识并保持关系？", "协作行为、冲突处理和稳定投入"),
        ],
        "ceo": [
            ("业务价值理解", "你认为这项职责最终应为公司创造什么业务价值？", "请给出领先指标、结果指标以及二者失真时的判断方法。", "职责与经营结果的连接"),
            ("优先级与取舍", "如果资源只能支持这项职责的一半工作，你会保留什么、放弃什么？", "你的判断依据、风险边界和调整信号分别是什么？", "优先级、长期价值和风险判断"),
        ],
    }
    for round_type, count in ROUND_COUNTS.items():
        for index in range(count):
            source = primary[index % len(primary)]
            name, question, follow_up, evidence_target = templates[round_type][index]
            dimensions.append(
                {
                    "id": f"{round_type}-{index + 1}",
                    "name": name,
                    "round_type": round_type,
                    "definition": f"结合{title}的实际职责核实{name}，不以抽象标签代替行为证据。",
                    "job_context": source,
                    "source_excerpt": source,
                    "evidence_target": evidence_target,
                    "question": f"JD 中的具体职责是“{source}”。{question}",
                    "follow_up": follow_up,
                }
            )
    return {
        "version": PROFILE_VERSION,
        "analysis_mode": "local_structured_fallback",
        "provider": "local-structured-rules",
        "role_mission": role_mission,
        "business_outcomes": _unique(outcomes)[:4],
        "work_scenarios": _unique(scenarios)[:4],
        "interview_dimensions": dimensions,
        "excluded_non_job_factors": excluded,
        "model_assistance": {"status": "fallback", "error_code": None},
    }


def normalize_job_semantic_profile(
    raw: dict[str, Any],
    *,
    title: str,
    jd_text: str,
    provider: str,
) -> dict[str, Any]:
    fallback = build_local_job_semantic_profile(title, jd_text)
    dimensions: list[dict[str, Any]] = []
    round_seen = {key: 0 for key in ROUND_COUNTS}
    for item in raw.get("interview_dimensions", []):
        if not isinstance(item, dict):
            continue
        round_type = str(item.get("round_type", ""))
        if round_type not in ROUND_COUNTS or round_seen[round_type] >= ROUND_COUNTS[round_type]:
            continue
        source_excerpt = str(item.get("source_excerpt", "")).strip()
        question = str(item.get("question", "")).strip()
        follow_up = str(item.get("follow_up", "")).strip()
        semantic_text = " ".join(
            str(item.get(key, ""))
            for key in (
                "name",
                "definition",
                "job_context",
                "source_excerpt",
                "evidence_target",
                "question",
                "follow_up",
            )
        )
        if (
            len(source_excerpt) < 4
            or source_excerpt not in jd_text
            or contains_non_job_factor(semantic_text)
            or _UNSUPPORTED_FIT_PATTERN.search(semantic_text)
            or not question
            or not follow_up
        ):
            continue
        round_seen[round_type] += 1
        dimensions.append(
            {
                "id": f"{round_type}-{round_seen[round_type]}",
                "name": str(item.get("name") or "岗位职责核实")[:64],
                "round_type": round_type,
                "definition": str(item.get("definition") or "核实岗位相关的可观察行为证据")[:300],
                "job_context": str(item.get("job_context") or source_excerpt)[:300],
                "source_excerpt": source_excerpt[:300],
                "evidence_target": str(item.get("evidence_target") or "具体情境、本人行动与可验证结果")[:300],
                "question": question[:500],
                "follow_up": follow_up[:500],
            }
        )
    model_dimension_count = len(dimensions)
    if model_dimension_count == 0:
        fallback["model_assistance"] = {
            "status": "fallback",
            "error_code": "invalid_semantic_shape",
        }
        return fallback

    for round_type, required_count in ROUND_COUNTS.items():
        missing = required_count - round_seen[round_type]
        if missing <= 0:
            continue
        fallback_items = [
            item for item in fallback["interview_dimensions"] if item["round_type"] == round_type
        ]
        dimensions.extend(fallback_items[round_seen[round_type] : required_count])

    role_mission = str(raw.get("role_mission", "")).strip()
    if len(role_mission) < 8 or contains_non_job_factor(role_mission):
        role_mission = fallback["role_mission"]
    outcomes = _safe_text_list(
        raw.get("business_outcomes"), 4, source_text=jd_text
    )
    scenarios = _safe_text_list(raw.get("work_scenarios"), 4)
    return {
        "version": PROFILE_VERSION,
        "analysis_mode": (
            "llm_semantic"
            if model_dimension_count == sum(ROUND_COUNTS.values())
            else "hybrid_semantic"
        ),
        "provider": provider,
        "role_mission": role_mission[:500],
        "business_outcomes": outcomes or fallback["business_outcomes"],
        "work_scenarios": scenarios or fallback["work_scenarios"],
        "interview_dimensions": dimensions,
        "excluded_non_job_factors": detect_non_job_factors(jd_text),
        "model_dimension_count": model_dimension_count,
        "model_assistance": {"status": "active", "error_code": None},
    }


def job_semantic_schema() -> dict[str, Any]:
    dimension = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "round_type": {"type": "string", "enum": ["business", "hr", "ceo"]},
            "definition": {"type": "string"},
            "job_context": {"type": "string"},
            "source_excerpt": {"type": "string"},
            "evidence_target": {"type": "string"},
            "question": {"type": "string"},
            "follow_up": {"type": "string"},
        },
        "required": ["name", "round_type", "definition", "job_context", "source_excerpt", "evidence_target", "question", "follow_up"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "role_mission": {"type": "string"},
            "business_outcomes": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
            "work_scenarios": {"type": "array", "maxItems": 4, "items": {"type": "string"}},
            "interview_dimensions": {"type": "array", "minItems": 7, "maxItems": 7, "items": dimension},
        },
        "required": ["role_mission", "business_outcomes", "work_scenarios", "interview_dimensions"],
    }


def _safe_sentences(jd_text: str) -> list[str]:
    items: list[str] = []
    for token in re.split(r"[。；;\n]+|(?:^|\s)\d+[.、]\s*", jd_text):
        value = re.sub(r"^[\s•·\-*（()一二三四五六七八九十、:：]+", "", token).strip()
        if 6 <= len(value) <= 220 and not contains_non_job_factor(value):
            items.append(value)
    return _unique(items)


def _safe_text_list(
    value: Any, limit: int, *, source_text: str | None = None
) -> list[str]:
    if not isinstance(value, list):
        return []
    return _unique(
        [
            str(item).strip()[:300]
            for item in value
            if str(item).strip()
            and not contains_non_job_factor(str(item))
            and not _UNSUPPORTED_FIT_PATTERN.search(str(item))
            and not (
                source_text
                and any(
                    match.group(0) not in source_text
                    for match in _QUANTIFIED_CONSTRAINT_PATTERN.finditer(str(item))
                )
            )
        ]
    )[:limit]


def _role_mission(title: str, responsibility: str) -> str:
    return f"{title}需要在真实业务约束下承担“{responsibility}”，并形成可衡量、可复盘的岗位结果。"


def _outcome_from_sentence(sentence: str) -> str:
    return f"围绕“{sentence}”形成可验证的交付结果与复盘"


def _scenario_from_sentence(sentence: str) -> str:
    return f"在资源、时间或跨团队约束下完成“{sentence}”"


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))

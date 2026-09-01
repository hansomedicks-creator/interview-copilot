from __future__ import annotations

import re


_LEADING_FILLER = re.compile(
    r"^[\s，,。！？!?；;：:]*"
    r"(?:嗯+|呃+|额+|哦+|啊+|诶+|唔+|那个|这个嘛|怎么说呢|就是说|然后呢|然后|就是|"
    r"对对对|对+|好(?:的)?|是的|行)[\s，,。！？!?；;：:]*",
    re.IGNORECASE,
)
_FILLER_TOKEN = re.compile(
    r"(?:嗯+|呃+|额+|哦+|啊+|诶+|唔+|那个|怎么说呢|就是说|就是|然后|对对对|好的|是的)"
)
_QUESTION_MARKERS = (
    "请问",
    "想问",
    "还有几个问题",
    "后面是什么流程",
    "几轮面试",
    "什么时候通知",
    "岗位职责是什么",
    "薪资",
    "福利",
)
_ABSENCE_MARKERS = (
    "没有证据",
    "无证据",
    "证据不足",
    "未提供",
    "未提及",
    "未说明",
    "未体现",
    "无法判断",
    "无法评估",
    "无法证明",
    "不能证明",
    "缺乏证据",
    "信息不足",
    "回答不足",
)
_SUBSTANCE_MARKERS = (
    "我",
    "负责",
    "做",
    "判断",
    "选择",
    "决定",
    "因为",
    "所以",
    "发现",
    "分析",
    "设计",
    "开发",
    "排查",
    "调整",
    "解决",
    "完成",
    "达成",
    "上线",
    "交付",
    "提升",
    "降低",
    "缩短",
    "增加",
    "减少",
    "公开",
    "进展",
    "适应",
    "夜班",
    "排班",
    "困难",
    "难",
    "失败",
    "问题",
    "客户",
    "团队",
    "项目",
    "数据",
    "代码",
    "系统",
    "方案",
    "流程",
)


def trim_leading_fillers(text: str) -> str:
    """Remove discourse fillers from the beginning while preserving a verbatim suffix."""
    value = str(text or "").strip()
    previous = None
    while value and value != previous:
        previous = value
        value = _LEADING_FILLER.sub("", value, count=1).strip()
    return value


def substantive_character_count(text: str) -> int:
    value = _FILLER_TOKEN.sub("", str(text or ""))
    return len(re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%％]", "", value))


def is_substantive_utterance(text: str, *, min_chars: int = 8) -> bool:
    value = trim_leading_fillers(text)
    if substantive_character_count(value) < min_chars:
        return False
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", value)
    if not compact or len(set(compact)) < 4:
        return False
    return any(marker in value for marker in _SUBSTANCE_MARKERS) or substantive_character_count(value) >= 14


def looks_like_candidate_question(text: str) -> bool:
    value = trim_leading_fillers(text)
    if any(marker in value for marker in _QUESTION_MARKERS):
        return True
    return bool(
        len(value) <= 28
        and (value.endswith(("？", "?")) or value.endswith(("吗", "呢")))
        and not any(marker in value for marker in ("因为", "所以", "我会", "我先", "我负责"))
    )


def is_evidence_worthy_utterance(text: str, *, min_chars: int = 6) -> bool:
    return is_substantive_utterance(text, min_chars=min_chars) and not looks_like_candidate_question(text)


def best_substantive_quote(text: str, *, max_chars: int = 180) -> str:
    """Select a concise verbatim clause instead of preserving leading filler noise."""
    source = str(text or "").strip()
    clauses = [
        trim_leading_fillers(item.strip(" \t\r\n，,。！？!?；;：:\"“”'‘’"))
        for item in re.split(r"[。！？!?；;\n]", source)
    ]
    candidates = [item for item in clauses if is_evidence_worthy_utterance(item)]
    if not candidates:
        trimmed = trim_leading_fillers(source)
        return trimmed[:max_chars] if is_evidence_worthy_utterance(trimmed) else ""

    def score(value: str) -> tuple[int, int]:
        marker_score = sum(marker in value for marker in _SUBSTANCE_MARKERS)
        return marker_score, min(substantive_character_count(value), max_chars)

    return max(candidates, key=score)[:max_chars]


def describes_absence_instead_of_behavior(*values: str) -> bool:
    combined = " ".join(str(value or "") for value in values)
    return any(marker in combined for marker in _ABSENCE_MARKERS)


def is_usable_evidence_record(
    *,
    quote: str,
    direction: str,
    explanation: str,
    human_status: str,
) -> bool:
    """Keep human-reviewed records; hide legacy pending records that fail the new gate."""
    if human_status in {"confirmed", "modified"}:
        return True
    return (
        direction in {"support", "negative"}
        and is_evidence_worthy_utterance(quote)
        and not describes_absence_instead_of_behavior(explanation)
    )

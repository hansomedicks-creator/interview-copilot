from __future__ import annotations

from typing import Any

from ..models import TranscriptSegment
from .utterance_quality import best_substantive_quote, is_evidence_worthy_utterance


ANSWER_LOGIC_BOUNDARY = (
    "本模块只检查回答文本中的因果、时间线、责任边界和跨轮表述一致性；"
    "不能仅凭语音、停顿、表情或措辞判断候选人是否撒谎。异常项只用于提出核验问题，"
    "不得直接作为录用或淘汰依据。"
)


def build_local_answer_logic_review(
    segments: list[TranscriptSegment],
) -> dict[str, Any]:
    """Provide a safe baseline; semantic providers may replace it after full review."""
    candidate_segments = [
        item
        for item in segments
        if item.speaker_role == "candidate"
        and is_evidence_worthy_utterance(item.effective_text)
    ]
    if len(candidate_segments) < 2:
        return {
            "status": "insufficient_evidence",
            "sufficient_evidence": False,
            "logic_score": None,
            "confidence": 0.0,
            "label": "有效回答不足，暂不能分析",
            "summary": "至少需要两段有效候选人回答，才能检查前后逻辑和表述一致性。",
            "dimensions": [],
            "consistency_flags": [],
            "verification_questions": [],
            "evidence_segment_ids": [],
            "boundary": ANSWER_LOGIC_BOUNDARY,
        }
    strongest = sorted(
        candidate_segments,
        key=lambda item: len(item.effective_text),
        reverse=True,
    )[:4]
    return {
        "status": "semantic_review_pending",
        "sufficient_evidence": True,
        "logic_score": None,
        "confidence": 0.0,
        "label": "等待语义一致性核验",
        "summary": "本地规则不会把语速、停顿或关键词当作撒谎信号；需要语义模型复核完整回答。",
        "dimensions": [
            {
                "id": "cross_turn_consistency",
                "name": "跨回答一致性",
                "status": "unknown",
                "explanation": "尚未完成跨回答语义核验。",
                "segment_ids": [],
                "quotes": [],
            }
        ],
        "consistency_flags": [],
        "verification_questions": [],
        "evidence_segment_ids": [item.id for item in strongest],
        "boundary": ANSWER_LOGIC_BOUNDARY,
    }


def quotes_for_segments(
    segment_ids: list[str],
    candidate_segments: dict[str, TranscriptSegment],
    *,
    max_chars: int = 140,
) -> list[dict[str, str]]:
    output = []
    for segment_id in segment_ids:
        segment = candidate_segments.get(segment_id)
        if not segment:
            continue
        quote = best_substantive_quote(segment.effective_text, max_chars=max_chars)
        if quote:
            output.append({"segment_id": segment_id, "quote": quote})
    return output

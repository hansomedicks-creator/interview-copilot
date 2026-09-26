"""Bounded answer review: never silently drop the start of a logical Q&A."""
from __future__ import annotations

import json
from typing import Any

from ..models import TranscriptSegment
from .answer_logic import ANSWER_LOGIC_BOUNDARY


def review_payload(job_title: str, round_type: str, segments: list[TranscriptSegment]) -> dict:
    return {
        "job_title_context_only": job_title,
        "round_type": round_type,
        "transcript": [{"segment_id": row.id, "speaker_role": row.speaker_role,
                        "text": row.effective_text} for row in segments],
    }


def payload_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def logical_turns(segments: list[TranscriptSegment]) -> list[list[TranscriptSegment]]:
    turns: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    has_answer = False
    for row in segments:
        if row.speaker_role == "interviewer" and has_answer:
            turns.append(current)
            current, has_answer = [], False
        current.append(row)
        has_answer = has_answer or row.speaker_role == "candidate"
    if current:
        turns.append(current)
    return turns


def answer_batches(segments: list[TranscriptSegment], job_title: str, round_type: str,
                   max_chars: int) -> tuple[list[list[TranscriptSegment]], list[str]]:
    batches: list[list[TranscriptSegment]] = []
    skipped: list[str] = []
    current: list[TranscriptSegment] = []
    for turn in logical_turns(segments):
        if payload_size(review_payload(job_title, round_type, turn)) > max_chars:
            if current:
                batches.append(current)
                current = []
            # Do not turn a clipped half-answer into a confident quality score.
            skipped.extend(row.id for row in turn)
            continue
        if current and payload_size(review_payload(job_title, round_type, current + turn)) > max_chars:
            batches.append(current)
            current = []
        current.extend(turn)
    if current:
        batches.append(current)
    return batches, skipped


def merge_reviews(reviews: list[dict], coverage: dict[str, Any]) -> dict:
    ids = list(dict.fromkeys(sid for review in reviews for sid in review["evidence_segment_ids"]))
    complete = not coverage["failed_batches"] and not coverage["oversized_segment_ids"]
    dimensions: dict[str, dict] = {}
    flags: dict[tuple, dict] = {}
    for review in reviews:
        for dim in review["dimensions"]:
            target = dimensions.setdefault(dim["id"], {**dim, "observations": [], "segment_ids": [],
                "question_segment_ids": [], "question_quotes": [], "quotes": []})
            target["observations"].append(dim)
            for key in ("segment_ids", "question_segment_ids", "question_quotes", "quotes"):
                for item in dim.get(key, []):
                    if item not in target[key]:
                        target[key].append(item)
            statuses = {item["status"] for item in target["observations"]}
            target["status"] = "needs_verification" if "needs_verification" in statuses else "coherent" if "coherent" in statuses else "unknown"
            target["explanation"] = "；".join(dict.fromkeys(item["explanation"] for item in target["observations"]))
        for flag in review["consistency_flags"]:
            flags[(flag["flag_type"], tuple(sorted(flag["segment_ids"])))] = flag
    weights = [max(1, len(review["evidence_segment_ids"])) for review in reviews]
    score = round(sum(review["logic_score"] * weight for review, weight in zip(reviews, weights)) / sum(weights), 1) if reviews and complete else None
    confidence = round(sum(review["confidence"] * weight for review, weight in zip(reviews, weights)) / sum(weights), 2) if reviews else 0
    return {
        "status": "model_assessed" if reviews and complete else "partial" if reviews else "insufficient_evidence" if complete else "semantic_unavailable",
        "sufficient_evidence": bool(reviews) and complete, "logic_score": score,
        "confidence": confidence if complete else round(confidence * coverage["completed_batches"] / max(1, coverage["total_batches"] + bool(coverage["oversized_segment_ids"])), 2),
        "label": "完整问答分段评价" if complete else "问答评价尚未完整",
        "summary": f"已处理 {coverage['completed_batches']}/{coverage['total_batches']} 个问答批次；{len(reviews)} 批有可核验评价。" + ("评分仅为本轮回答表现参考。" if complete else "部分内容未完成分析，暂不汇总回答质量分。"),
        "dimensions": list(dimensions.values()), "consistency_flags": list(flags.values()),
        "verification_questions": list(dict.fromkeys(q for review in reviews for q in review["verification_questions"])),
        "evidence_segment_ids": ids, "boundary": ANSWER_LOGIC_BOUNDARY,
        "batch_status": coverage, "batch_reviews": reviews,
    }

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    EvidenceItem,
    InterviewRound,
    SpeakerRoleMapping,
    TranscriptSegment,
    new_id,
    utc_now,
)


_INTERVIEWER_MARKERS: tuple[tuple[str, float], ...] = (
    ("请介绍", 2.6),
    ("请你", 2.2),
    ("请您", 2.2),
    ("能不能", 1.7),
    ("能否", 1.7),
    ("为什么", 1.6),
    ("具体说说", 2.0),
    ("举个例子", 2.0),
    ("你怎么看", 2.0),
    ("你刚才", 1.5),
    ("你说的", 1.6),
    ("你当时", 1.5),
    ("你是怎么", 1.8),
    ("有没有", 1.3),
    ("能说说", 1.7),
    ("这个岗位", 1.0),
    ("我们公司", 1.5),
    ("面试流程", 1.6),
    ("还有什么问题", 2.4),
    ("今天就先到这", 3.2),
    ("感谢您的时间", 3.0),
    ("如果都没有", 2.4),
)

_CANDIDATE_MARKERS: tuple[tuple[str, float], ...] = (
    ("我负责", 2.5),
    ("我参与", 2.2),
    ("我的职责", 2.3),
    ("我的项目", 2.1),
    ("我们团队", 1.6),
    ("上一份工作", 2.4),
    ("上一家公司", 2.4),
    ("当时", 1.1),
    ("最终", 1.2),
    ("结果", 0.8),
    ("复盘", 1.1),
    ("离职", 1.7),
    ("期望薪资", 1.8),
    ("我的优势", 2.0),
    ("我认为", 1.0),
)

_CANDIDATE_QUESTION_MARKERS: tuple[str, ...] = (
    "我想了解",
    "我想问",
    "想请问",
    "贵公司",
    "这个岗位未来",
    "团队目前",
)


def observe_speaker(
    db: Session,
    interview: InterviewRound,
    provider_speaker_id: int,
    text: str,
) -> SpeakerRoleMapping:
    mapping = db.scalar(
        select(SpeakerRoleMapping).where(
            SpeakerRoleMapping.interview_round_id == interview.id,
            SpeakerRoleMapping.provider_speaker_id == provider_speaker_id,
        )
    )
    if mapping is None:
        mapping = SpeakerRoleMapping(
            id=new_id("speaker"),
            interview_round_id=interview.id,
            provider_speaker_id=provider_speaker_id,
        )
        db.add(mapping)
        db.flush()

    if mapping.source != "human":
        interviewer_score, candidate_score = _utterance_scores(text)
        mapping.interviewer_score += interviewer_score
        mapping.candidate_score += candidate_score
        mapping.sample_count += 1
        mapping.updated_at = utc_now()
        _resolve_automatic_mappings(db, interview.id)
    db.flush()
    _apply_mappings(db, interview.id)
    return mapping


def confirm_speaker_role(
    db: Session,
    interview: InterviewRound,
    provider_speaker_id: int,
    speaker_role: str,
    confirmed_by: str,
) -> SpeakerRoleMapping:
    mapping = db.scalar(
        select(SpeakerRoleMapping).where(
            SpeakerRoleMapping.interview_round_id == interview.id,
            SpeakerRoleMapping.provider_speaker_id == provider_speaker_id,
        )
    )
    if mapping is None:
        mapping = SpeakerRoleMapping(
            id=new_id("speaker"),
            interview_round_id=interview.id,
            provider_speaker_id=provider_speaker_id,
            sample_count=0,
        )
        db.add(mapping)
    mapping.speaker_role = speaker_role
    mapping.confidence = 1.0
    mapping.source = "human"
    mapping.confirmed_by = confirmed_by
    mapping.confirmed_at = utc_now()
    mapping.updated_at = utc_now()
    db.flush()
    _resolve_automatic_mappings(db, interview.id)
    _apply_mappings(db, interview.id)
    return mapping


def speaker_mapping_payloads(db: Session, interview_id: str) -> list[dict[str, Any]]:
    mappings = list(
        db.scalars(
            select(SpeakerRoleMapping)
            .where(SpeakerRoleMapping.interview_round_id == interview_id)
            .order_by(SpeakerRoleMapping.provider_speaker_id)
        ).all()
    )
    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.interview_round_id == interview_id,
                TranscriptSegment.provider_speaker_id.is_not(None),
            )
            .order_by(TranscriptSegment.start_ms)
        ).all()
    )
    samples: dict[int, str] = {}
    counts: dict[int, int] = {}
    for segment in segments:
        speaker_id = segment.provider_speaker_id
        if speaker_id is None:
            continue
        counts[speaker_id] = counts.get(speaker_id, 0) + 1
        samples.setdefault(speaker_id, segment.effective_text[:120])
    return [
        {
            "provider_speaker_id": item.provider_speaker_id,
            "speaker_label": _speaker_label(item.provider_speaker_id),
            "speaker_role": item.speaker_role,
            "confidence": round(item.confidence, 2),
            "source": item.source,
            "sample_count": max(item.sample_count, counts.get(item.provider_speaker_id, 0)),
            "sample_text": samples.get(item.provider_speaker_id),
            "confirmed_by": item.confirmed_by,
        }
        for item in mappings
    ]


def _utterance_scores(text: str) -> tuple[float, float]:
    normalized = "".join(text.split())
    interviewer = sum(weight for marker, weight in _INTERVIEWER_MARKERS if marker in normalized)
    candidate = sum(weight for marker, weight in _CANDIDATE_MARKERS if marker in normalized)
    is_question = "？" in normalized or "?" in normalized
    candidate_question = any(marker in normalized for marker in _CANDIDATE_QUESTION_MARKERS)
    if is_question:
        if candidate_question:
            candidate += 2.2
        else:
            interviewer += 1.2
    if normalized.startswith(("请", "能否", "能不能", "方便说说")):
        interviewer += 1.0
    if normalized.startswith("我") and len(normalized) >= 12:
        candidate += 0.7
    if "你" in normalized or "您" in normalized:
        interviewer += 0.35
    return interviewer, candidate


def _utterance_role(text: str) -> tuple[str, float]:
    """Infer one turn independently when cloud diarization reuses a speaker id."""
    interviewer, candidate = _utterance_scores(text)
    dominant = max(interviewer, candidate)
    difference = abs(interviewer - candidate)
    if dominant < 2.2 or difference < 1.2:
        return "unknown", 0.0
    role = "interviewer" if interviewer > candidate else "candidate"
    confidence = min(0.94, 0.72 + 0.16 * difference / max(interviewer + candidate, 1.0))
    return role, round(confidence, 2)


def _resolve_automatic_mappings(db: Session, interview_id: str) -> None:
    mappings = list(
        db.scalars(
            select(SpeakerRoleMapping).where(
                SpeakerRoleMapping.interview_round_id == interview_id
            )
        ).all()
    )
    for item in mappings:
        if item.source == "human":
            continue
        role, confidence = _direct_role(item)
        item.speaker_role = role
        item.confidence = confidence
        item.source = "semantic_auto"

    known = [item for item in mappings if item.speaker_role != "unknown" and item.confidence >= 0.72]
    unresolved = [item for item in mappings if item.speaker_role == "unknown" and item.sample_count > 0]
    human_candidates = [item for item in mappings if item.source == "human" and item.speaker_role == "candidate"]
    human_interviewers = [item for item in mappings if item.source == "human" and item.speaker_role == "interviewer"]
    if human_candidates:
        for item in mappings:
            if item.source != "human":
                item.speaker_role, item.confidence, item.source = "interviewer", 0.9, "paired_auto"
    elif human_interviewers and len(mappings) == 2:
        for item in mappings:
            if item.source != "human":
                item.speaker_role, item.confidence, item.source = "candidate", 0.9, "paired_auto"
    elif len(mappings) == 2 and len(known) == 1 and len(unresolved) == 1:
        opposite = "candidate" if known[0].speaker_role == "interviewer" else "interviewer"
        unresolved[0].speaker_role = opposite
        unresolved[0].confidence = min(0.82, max(0.72, known[0].confidence - 0.06))
        unresolved[0].source = "paired_auto"

    auto_candidates = [
        item for item in mappings if item.source != "human" and item.speaker_role == "candidate"
    ]
    if len(auto_candidates) > 1:
        winner = max(auto_candidates, key=lambda item: item.candidate_score - item.interviewer_score)
        for item in auto_candidates:
            if item is winner:
                continue
            role, confidence = _direct_role(item)
            if role == "candidate":
                item.speaker_role, item.confidence = "unknown", 0.0
            else:
                item.speaker_role, item.confidence = role, confidence


def _direct_role(mapping: SpeakerRoleMapping) -> tuple[str, float]:
    interviewer = mapping.interviewer_score
    candidate = mapping.candidate_score
    dominant = max(interviewer, candidate)
    difference = abs(interviewer - candidate)
    if dominant < 2.0 or difference < 1.2:
        return "unknown", 0.0
    role = "interviewer" if interviewer > candidate else "candidate"
    confidence = min(
        0.96,
        0.64 + 0.22 * difference / max(interviewer + candidate, 1.0) + 0.025 * min(mapping.sample_count, 4),
    )
    return role, round(confidence, 2)


def _apply_mappings(db: Session, interview_id: str) -> None:
    mappings = {
        item.provider_speaker_id: item
        for item in db.scalars(
            select(SpeakerRoleMapping).where(
                SpeakerRoleMapping.interview_round_id == interview_id
            )
        ).all()
    }
    segments = list(
        db.scalars(
            select(TranscriptSegment).where(
                TranscriptSegment.interview_round_id == interview_id,
                TranscriptSegment.provider_speaker_id.is_not(None),
            )
        ).all()
    )
    changed_to_interviewer: set[str] = set()
    has_human_anchor = any(item.source == "human" for item in mappings.values())
    for segment in segments:
        mapping = mappings.get(segment.provider_speaker_id)
        if mapping is None:
            continue
        previous = segment.speaker_role
        turn_role, turn_confidence = _utterance_role(segment.effective_text)
        strong_turn_override = (
            turn_role != "unknown"
            and turn_role != mapping.speaker_role
            and turn_confidence >= 0.88
            and (
                mapping.source not in {"human", "paired_auto"}
                or (
                    mapping.speaker_role == "candidate"
                    and any(
                        marker in segment.effective_text
                        for marker in ("今天就先到这", "感谢您的时间", "面试就先到这里")
                    )
                )
            )
        )
        if (
            mapping.source == "human"
            or (has_human_anchor and mapping.source == "paired_auto")
        ) and not strong_turn_override:
            segment.speaker_role = mapping.speaker_role
            segment.speaker_confidence = mapping.confidence
            if mapping.source == "human":
                segment.corrected_by = mapping.confirmed_by
                segment.corrected_at = mapping.confirmed_at
        else:
            if turn_role != "unknown":
                segment.speaker_role = turn_role
                segment.speaker_confidence = turn_confidence
            else:
                segment.speaker_role = mapping.speaker_role
                segment.speaker_confidence = mapping.confidence
        if previous == "candidate" and segment.speaker_role == "interviewer":
            changed_to_interviewer.add(segment.id)
    if changed_to_interviewer:
        evidence = db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.interview_round_id == interview_id,
                EvidenceItem.human_status == "pending",
            )
        ).all()
        for item in evidence:
            if changed_to_interviewer.intersection(item.segment_ids or []):
                item.human_status = "rejected"
                item.explanation = f"{item.explanation} 说话人已更正为面试官，系统自动排除该证据。"


def _speaker_label(provider_speaker_id: int) -> str:
    return f"声源 {chr(65 + provider_speaker_id)}"

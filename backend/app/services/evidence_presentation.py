from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Any, Iterable

from ..models import EvidenceItem
from .utterance_quality import is_usable_evidence_record, substantive_character_count


_REVIEW_PRIORITY = {"confirmed": 3, "modified": 3, "pending": 1}
_IMPORTANT_UNANSWERED_SOURCES = {
    "resume_jd_match",
    "resume_personalized",
    "prior_round",
    "prior_round_followup",
}


def build_evidence_digest(
    evidence_items: Iterable[EvidenceItem],
    dimensions: list[dict[str, Any]],
    question_states: list[dict[str, Any]],
    *,
    max_evidence: int = 8,
    max_unknowns: int = 4,
) -> dict[str, Any]:
    """Build a concise, traceable view while leaving raw evidence untouched."""
    raw_items = list(evidence_items)
    eligible = [
        item
        for item in raw_items
        if item.human_status != "rejected"
        and is_usable_evidence_record(
            quote=item.quote,
            direction=item.direction,
            explanation=item.explanation,
            human_status=item.human_status,
        )
    ]
    dimension_by_id = {
        str(item.get("id")): item for item in dimensions if item.get("id")
    }
    question_by_segment = _question_index(question_states)

    clusters: list[list[EvidenceItem]] = []
    for item in sorted(eligible, key=_primary_rank, reverse=True):
        cluster = next(
            (candidate for candidate in clusters if _belongs_to_cluster(item, candidate)),
            None,
        )
        if cluster is None:
            clusters.append([item])
        else:
            cluster.append(item)

    digest_items = [
        _cluster_payload(cluster, dimension_by_id, question_by_segment)
        for cluster in clusters
    ]
    digest_items.sort(
        key=lambda item: (
            item["human_status"] in {"confirmed", "modified"},
            item["strength"],
            item["related_count"],
        ),
        reverse=True,
    )
    visible = digest_items[:max_evidence]
    support = [item for item in visible if item["direction"] == "support"]
    risks = [item for item in visible if item["direction"] == "negative"]
    unknowns = _unknown_items(question_states, max_unknowns=max_unknowns)
    return {
        "summary": {
            "support": len(support),
            "risk": len(risks),
            "unknown": len(unknowns),
            "raw_count": len(raw_items),
            "eligible_count": len(eligible),
            "cluster_count": len(digest_items),
            "hidden_cluster_count": max(0, len(digest_items) - len(visible)),
        },
        "key_evidence": support,
        "risks": risks,
        "unknowns": unknowns,
        "policy": "相近原话仅在界面合并；原始证据完整保留。未回答和回答较浅属于未知项，不作为反向证据。",
    }


def empty_evidence_digest() -> dict[str, Any]:
    return {
        "summary": {
            "support": 0,
            "risk": 0,
            "unknown": 0,
            "raw_count": 0,
            "eligible_count": 0,
            "cluster_count": 0,
            "hidden_cluster_count": 0,
        },
        "key_evidence": [],
        "risks": [],
        "unknowns": [],
        "policy": "面试开始并出现候选人回答后，才会整理关键事实。",
    }


def _question_index(
    question_states: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for state in question_states:
        for segment_id in state.get("evidence_segment_ids") or []:
            current = indexed.get(str(segment_id))
            if current is None or state.get("status") == "evidenced":
                indexed[str(segment_id)] = state
    return indexed


def _belongs_to_cluster(item: EvidenceItem, cluster: list[EvidenceItem]) -> bool:
    primary = cluster[0]
    if item.competency_id != primary.competency_id or item.direction != primary.direction:
        return False
    item_segments = {str(value) for value in item.segment_ids or []}
    for existing in cluster:
        existing_segments = {str(value) for value in existing.segment_ids or []}
        if item_segments and existing_segments and item_segments & existing_segments:
            return True
        if _text_similarity(item.quote, existing.quote) >= 0.64:
            return True
    return False


def _text_similarity(left: str, right: str) -> float:
    left_normalized = _normalize(left)
    right_normalized = _normalize(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        shorter = min(len(left_normalized), len(right_normalized))
        longer = max(len(left_normalized), len(right_normalized))
        if shorter >= 10:
            return shorter / max(1, longer)
    sequence = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    left_pairs = _bigrams(left_normalized)
    right_pairs = _bigrams(right_normalized)
    jaccard = len(left_pairs & right_pairs) / max(1, len(left_pairs | right_pairs))
    return max(sequence, jaccard)


def _normalize(value: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%]", "", str(value or "")).lower()


def _bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}


def _primary_rank(item: EvidenceItem) -> tuple[int, float, int]:
    return (
        _REVIEW_PRIORITY.get(item.human_status, 0),
        float(item.strength or 0),
        substantive_character_count(item.quote),
    )


def _cluster_payload(
    cluster: list[EvidenceItem],
    dimension_by_id: dict[str, dict[str, Any]],
    question_by_segment: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    primary = max(cluster, key=_primary_rank)
    dimension = dimension_by_id.get(primary.competency_id, {})
    competency_name = str(dimension.get("name") or primary.competency_id)
    source_question = next(
        (
            question_by_segment[str(segment_id)]
            for segment_id in primary.segment_ids or []
            if str(segment_id) in question_by_segment
        ),
        None,
    )
    if source_question is None:
        source_question = next(
            (
                question_by_segment[str(segment_id)]
                for item in cluster
                for segment_id in item.segment_ids or []
                if str(segment_id) in question_by_segment
            ),
            None,
        )
    description = str(dimension.get("description") or "").strip()
    if primary.direction == "support":
        impact = f"支持对“{competency_name}”的判断"
        why = (
            f"岗位判断依据：{description}"
            if description
            else f"这段回答为“{competency_name}”提供了可核对的事实。"
        )
    else:
        impact = f"提示“{competency_name}”可能存在风险"
        why = (
            f"需要复核这段回答是否与岗位要求冲突：{description}"
            if description
            else f"需要结合完整上下文复核这段回答是否与“{competency_name}”要求冲突。"
        )
    review_note = (
        "已人工确认，可作为本轮评价依据。"
        if primary.human_status in {"confirmed", "modified"}
        else "待面试官核对语境，当前结论应保留不确定性。"
    )
    all_segment_ids = list(
        dict.fromkeys(
            str(segment_id)
            for item in cluster
            for segment_id in item.segment_ids or []
        )
    )
    return {
        "id": primary.id,
        "primary_evidence_id": primary.id,
        "evidence_ids": [item.id for item in cluster],
        "competency_id": primary.competency_id,
        "competency_name": competency_name,
        "direction": primary.direction,
        "strength": round(float(primary.strength or 0), 2),
        "quote": primary.quote,
        "segment_ids": all_segment_ids,
        "human_status": primary.human_status,
        "related_count": len(cluster),
        "source_question_id": source_question.get("question_id") if source_question else None,
        "source_question_text": source_question.get("question") if source_question else None,
        "source_question_kind": source_question.get("source") if source_question else None,
        "decision_impact": impact,
        "why_it_matters": why,
        "review_note": review_note,
    }


def _unknown_items(
    question_states: list[dict[str, Any]], *, max_unknowns: int
) -> list[dict[str, Any]]:
    candidates = []
    for state in question_states:
        status = state.get("status")
        if status == "shallow":
            priority = 3
            reason = "候选人已回答，但当前信息不足以形成稳定判断。"
        elif status == "unanswered" and (
            state.get("required") or state.get("source") in _IMPORTANT_UNANSWERED_SOURCES
        ):
            priority = 2 if state.get("required") else 1
            reason = "本轮尚未获得回答；未验证不等于候选人不符合。"
        else:
            continue
        candidates.append(
            (
                priority,
                {
                    "question_id": state.get("question_id"),
                    "competency_id": state.get("competency_id"),
                    "competency_name": state.get("competency_name") or "待验证事项",
                    "source_question_text": state.get("question") or "",
                    "status": status,
                    "reason": reason,
                    "missing_dimensions": list(state.get("missing_dimensions") or []),
                    "basis_quote": state.get("basis_quote") or "",
                    "decision_impact": "暂不支持正向或反向结论",
                },
            )
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in candidates[:max_unknowns]]

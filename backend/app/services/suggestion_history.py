from __future__ import annotations

import re
from typing import Any

from ..models import InterviewRound, new_id, utc_now


def merge_suggestion_history(
    interview: InterviewRound, analysis: dict[str, Any]
) -> dict[str, Any]:
    """Keep useful prompts stable and prefer the latest logical question."""
    history = [dict(item) for item in (interview.suggestion_history or [])]
    by_key = {str(item.get("dedupe_key")): item for item in history}
    now = utc_now().isoformat()
    current_ids: list[str] = []
    active_question = analysis.get("active_question_id")
    for item in history:
        if item.get("status") == "active" and item["id"] in analysis.get("resolved_suggestion_ids", []):
            item.update(status="resolved", resolved_at=now, is_current=False)
        if item.get("question_id") != active_question:
            item["is_current"] = False
    for suggestion in list(analysis.get("suggestions", []))[:6]:
        key = _dedupe_key(suggestion)
        existing = by_key.get(key)
        if existing:
            if existing.get("status") in {"addressed", "skipped", "resolved"}:
                continue
            if existing.get("source") == "llm_semantic_evidence_gap" and suggestion.get("source") != "llm_semantic_evidence_gap":
                # Fast ASR updates must not overwrite a semantic card with a
                # local template while the next semantic response is pending.
                current_ids.append(existing["id"])
                continue
            substantive_update = any(existing.get(field) != suggestion.get(field) for field in (
                "basis_quote", "evidence_segment_ids", "evidence_gap", "follow_up_purpose"
            ))
            existing["occurrence_count"] = int(existing.get("occurrence_count", 1)) + 1
            if existing.get("status") == "deferred":
                existing["status"] = "active"
                existing["resolved_at"] = None
            semantic_promotion = (
                suggestion.get("source") == "llm_semantic_evidence_gap"
                and existing.get("source") != "llm_semantic_evidence_gap"
            )
            if semantic_promotion or substantive_update:
                existing["last_seen_at"] = now
                # The fast local pass may have created an older card in legacy
                # sessions. A validated semantic suggestion must replace that
                # template instead of inheriting its frozen wording.
                for field in (
                    "question",
                    "reason",
                    "source",
                    "basis_quote",
                    "answer_summary",
                    "evidence_segment_ids",
                    "source_question_text",
                    "priority",
                    "evidence_gap",
                    "follow_up_purpose",
                ):
                    if field in suggestion:
                        existing[field] = suggestion[field]
            # Mere paraphrasing stays stable; new evidence may update the card.
            # Priority is only allowed to move upward.
            priority_rank = {"low": 0, "normal": 1, "high": 2}
            if priority_rank.get(str(suggestion.get("priority")), 1) > priority_rank.get(
                str(existing.get("priority")), 1
            ):
                existing["priority"] = suggestion.get("priority")
            current_ids.append(existing["id"])
            continue
        item = {
            "id": new_id("sg"),
            "dedupe_key": key,
            "status": "active",
            "created_at": now,
            "last_seen_at": now,
            "occurrence_count": 1,
            **suggestion,
        }
        history.append(item)
        by_key[key] = item
        current_ids.append(item["id"])
    active_items = [item for item in reversed(history) if item.get("status") == "active"]
    active_items.sort(
        key=lambda item: (
            bool(active_question and item.get("question_id") == active_question),
            str(item.get("last_seen_at") or ""),
            str(item.get("created_at") or ""),
        ), reverse=True,
    )
    current = next((item for item in active_items if active_question and item.get("question_id") == active_question), None)
    for item in history:
        item["is_current"] = item is current
    interview.suggestion_history = history
    analysis["suggestion_history"] = sorted(reversed(history), key=lambda item: (
        str(item.get("last_seen_at") or ""), str(item.get("created_at") or "")
    ), reverse=True)
    analysis["current_suggestion_ids"] = [current["id"]] if current else []
    return analysis


def update_suggestion_status(
    interview: InterviewRound, suggestion_id: str, status: str
) -> dict[str, Any] | None:
    history = [dict(item) for item in (interview.suggestion_history or [])]
    selected = None
    for item in history:
        if item.get("id") == suggestion_id:
            item["status"] = status
            item["is_current"] = False
            item["resolved_at"] = utc_now().isoformat() if status != "active" else None
            selected = item
            break
    if selected is not None:
        interview.suggestion_history = history
    return selected


def _dedupe_key(item: dict[str, Any]) -> str:
    question_id = str(item.get("question_id") or "")
    gap = str(item.get("evidence_gap") or item.get("answer_status") or "")
    if question_id:
        return f"{question_id}|{gap}"
    normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(item.get("question", "")))
    return f"free|{normalized[:120]}"

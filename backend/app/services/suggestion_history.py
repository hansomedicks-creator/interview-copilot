from __future__ import annotations

import re
from typing import Any

from ..models import InterviewRound, new_id, utc_now


def merge_suggestion_history(
    interview: InterviewRound, analysis: dict[str, Any]
) -> dict[str, Any]:
    """Keep useful prompts stable without forcing the conversation backwards."""
    history = [dict(item) for item in (interview.suggestion_history or [])]
    by_key = {str(item.get("dedupe_key")): item for item in history}
    now = utc_now().isoformat()
    current_ids: list[str] = []
    for suggestion in list(analysis.get("suggestions", []))[:6]:
        key = _dedupe_key(suggestion)
        existing = by_key.get(key)
        if existing:
            existing["last_seen_at"] = now
            existing["occurrence_count"] = int(existing.get("occurrence_count", 1)) + 1
            if existing.get("status") == "deferred":
                existing["status"] = "active"
                existing["resolved_at"] = None
            semantic_promotion = (
                suggestion.get("source") == "llm_semantic_evidence_gap"
                and existing.get("source") != "llm_semantic_evidence_gap"
            )
            if semantic_promotion:
                # The fast local pass may have created an older card in legacy
                # sessions. A validated semantic suggestion must replace that
                # template instead of inheriting its frozen wording.
                for field in (
                    "question",
                    "reason",
                    "source",
                    "basis_quote",
                    "evidence_segment_ids",
                    "source_question_text",
                    "priority",
                ):
                    if field in suggestion:
                        existing[field] = suggestion[field]
            # Once shown, keep the wording and source question stable. Model
            # refreshes may phrase the same gap differently; replacing the card
            # every few seconds makes it impossible for an interviewer to read.
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
    active_items = [item for item in history if item.get("status") == "active"]
    active_items.sort(
        key=lambda item: (
            0 if item.get("id") in current_ids else 1,
            0 if item.get("priority") == "high" else 1,
            str(item.get("created_at") or ""),
        )
    )
    for item in active_items[3:]:
        item["status"] = "deferred"
        item["resolved_at"] = now
    history = history[-30:]
    interview.suggestion_history = history
    analysis["suggestion_history"] = list(reversed(history))
    analysis["current_suggestion_ids"] = current_ids
    return analysis


def update_suggestion_status(
    interview: InterviewRound, suggestion_id: str, status: str
) -> dict[str, Any] | None:
    history = [dict(item) for item in (interview.suggestion_history or [])]
    selected = None
    for item in history:
        if item.get("id") == suggestion_id:
            item["status"] = status
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

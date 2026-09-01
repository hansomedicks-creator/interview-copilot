from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EvidenceItem, InterviewQuestionProgress, InterviewRound, Job, TranscriptSegment, new_id
from ..services.evaluation_scope import evaluation_scope_payload, round_evaluation_dimensions
from ..services.job_semantics import build_local_job_semantic_profile
from ..services.question_analysis import analyze_question_answers, assess_response_quality
from ..services.answer_logic import build_local_answer_logic_review
from ..services.utterance_quality import (
    best_substantive_quote,
    is_evidence_worthy_utterance,
    is_usable_evidence_record,
)


class MockIntelligenceProvider:
    """Deterministic provider for product validation without external data transfer."""

    name = "mock-rules-v0.1"
    _negative_markers = ("没有", "不会", "失败", "没能", "放弃", "无法")

    def analyze_job_definition(self, title: str, jd_text: str) -> dict[str, Any]:
        return build_local_job_semantic_profile(title, jd_text)

    def analyze_live(
        self,
        db: Session,
        interview: InterviewRound,
        job: Job,
        latest_segment: TranscriptSegment | None = None,
    ) -> dict[str, Any]:
        conversation_mode = interview.interview_mode == "conversation"
        competencies = [] if conversation_mode else round_evaluation_dimensions(db, interview, job)
        segments = db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.interview_round_id == interview.id,
                TranscriptSegment.is_final.is_(True),
            )
            .order_by(TranscriptSegment.start_ms)
        ).all()
        candidate_segments = [
            s
            for s in segments
            if s.speaker_role == "candidate" and is_evidence_worthy_utterance(s.effective_text)
        ]
        question_progress = db.scalars(
            select(InterviewQuestionProgress).where(
                InterviewQuestionProgress.interview_round_id == interview.id
            )
        ).all()
        question_analysis = analyze_question_answers(
            [] if conversation_mode else (interview.plan_payload or {}).get("questions", []),
            segments,
            question_progress,
        )
        evidence = db.scalars(
            select(EvidenceItem).where(EvidenceItem.interview_round_id == interview.id)
        ).all()
        evidence_by_competency: dict[str, list[EvidenceItem]] = defaultdict(list)
        for item in evidence:
            if item.human_status != "rejected" and is_usable_evidence_record(
                quote=item.quote,
                direction=item.direction,
                explanation=item.explanation,
                human_status=item.human_status,
            ):
                evidence_by_competency[item.competency_id].append(item)

        if latest_segment and latest_segment.speaker_role == "candidate":
            self._extract_draft_evidence(db, interview, latest_segment, competencies, evidence)
            db.flush()
            evidence = db.scalars(
                select(EvidenceItem).where(EvidenceItem.interview_round_id == interview.id)
            ).all()
            evidence_by_competency = defaultdict(list)
            for item in evidence:
                if item.human_status != "rejected" and is_usable_evidence_record(
                    quote=item.quote,
                    direction=item.direction,
                    explanation=item.explanation,
                    human_status=item.human_status,
                ):
                    evidence_by_competency[item.competency_id].append(item)

        coverage = []
        for competency in competencies:
            matching_segments = [
                segment
                for segment in candidate_segments
                if any(k in segment.effective_text for k in competency.get("keywords", []))
            ]
            items = evidence_by_competency[competency["id"]]
            if any(i.human_status in {"confirmed", "modified"} for i in items):
                status = "verified"
            elif items or matching_segments:
                status = "mentioned"
            else:
                status = "uncovered"
            coverage.append(
                {
                    "competency_id": competency["id"],
                    "name": competency["name"],
                    "status": status,
                    "evidence_count": len(items),
                }
            )

        # Uncovered dimensions belong to the prepared-question panel. A live
        # follow-up must react to something the candidate has actually said.
        suggestions = list(question_analysis["suggestions"])

        return {
            "provider": self.name,
            "mode": "mock",
            "analysis_mode": interview.interview_mode,
            "coverage": coverage,
            "question_coverage": question_analysis["states"],
            "question_coverage_summary": question_analysis["summary"],
            "active_question_id": question_analysis["active_question_id"],
            "suggestions": suggestions,
            "evidence": [self._evidence_payload(item) for item in evidence[-10:]],
            "transcript_segment_count": len(segments),
        }

    def _extract_draft_evidence(
        self,
        db: Session,
        interview: InterviewRound,
        segment: TranscriptSegment,
        competencies: list[dict],
        existing: list[EvidenceItem],
    ) -> None:
        existing_keys = {
            (item.competency_id, segment_id)
            for item in existing
            for segment_id in item.segment_ids
        }
        text = segment.effective_text
        if not is_evidence_worthy_utterance(text):
            return
        quote = best_substantive_quote(text, max_chars=180)
        if not quote:
            return
        for competency in competencies:
            matched = [k for k in competency.get("keywords", []) if k in text]
            if not matched or (competency["id"], segment.id) in existing_keys:
                continue
            direction = "negative" if any(marker in text for marker in self._negative_markers) else "support"
            strength = min(0.9, 0.45 + len(matched) * 0.12 + min(len(text), 80) / 400)
            db.add(
                EvidenceItem(
                    id=new_id("ev"),
                    interview_round_id=interview.id,
                    competency_id=competency["id"],
                    segment_ids=[segment.id],
                    quote=quote,
                    direction=direction,
                    strength=round(strength, 2),
                    explanation=f"Mock 规则命中关键词：{'、'.join(matched)}。需面试官核对语境。",
                    model_version=self.name,
                    human_status="pending",
                )
            )

    def draft_scorecard(
        self, db: Session, interview: InterviewRound, job: Job
    ) -> dict[str, Any]:
        conversation_mode = interview.interview_mode == "conversation"
        competencies = [] if conversation_mode else round_evaluation_dimensions(db, interview, job)
        segments = db.scalars(
            select(TranscriptSegment)
            .where(
                TranscriptSegment.interview_round_id == interview.id,
                TranscriptSegment.is_final.is_(True),
            )
            .order_by(TranscriptSegment.start_ms)
        ).all()
        question_progress = db.scalars(
            select(InterviewQuestionProgress).where(
                InterviewQuestionProgress.interview_round_id == interview.id
            )
        ).all()
        question_analysis = analyze_question_answers(
            [] if conversation_mode else (interview.plan_payload or {}).get("questions", []),
            segments,
            question_progress,
        )
        response_quality = assess_response_quality(list(segments))
        answer_logic_review = build_local_answer_logic_review(list(segments))
        if conversation_mode:
            candidate_segments = [item for item in segments if item.speaker_role == "candidate"]
            interviewer_segments = [item for item in segments if item.speaker_role == "interviewer"]
            shallow_states = [item for item in question_analysis["states"] if item["status"] == "shallow"]
            answered_states = [item for item in question_analysis["states"] if item["status"] != "unanswered"]
            evidenced_states = [item for item in answered_states if item["status"] == "evidenced"]
            evidence_ratio = len(evidenced_states) / max(1, len(answered_states))
            local_score = (
                round(
                    min(
                        5.0,
                        max(
                            1.0,
                            float(response_quality.get("score") or 1) * 0.65
                            + (1 + 4 * evidence_ratio) * 0.35,
                        ),
                    ),
                    1,
                )
                if candidate_segments
                else None
            )
            sufficient = len(candidate_segments) >= 3 and bool(answered_states)
            if not sufficient:
                ai_decision, ai_label = "insufficient_evidence", "证据不足，暂不建议推进或淘汰"
            elif local_score is not None and local_score >= 3.6:
                ai_decision, ai_label = "advance", "建议进入下一轮，继续核实关键事实"
            elif local_score is not None and local_score >= 2.7:
                ai_decision, ai_label = "supplementary_interview", "建议补充验证后再决定"
            else:
                ai_decision, ai_label = "hold", "建议保留讨论，并补充关键证据"
            observations = []
            if candidate_segments:
                observations.append(
                    f"共识别 {len(candidate_segments)} 段候选人回答，回答质量参考为“{response_quality['label']}”。"
                )
            if shallow_states:
                observations.append(f"有 {len(shallow_states)} 个实际提问的回答仍缺少可核验细节。")
            if not observations:
                observations.append("当前有效对话较少，暂不能形成稳定的对话分析。")
            notable = sorted(candidate_segments, key=lambda item: len(item.effective_text), reverse=True)[:3]
            return {
                "rubric_version": "conversation-review-v1.0",
                "ai_scores": [],
                "recommendation": {
                    "interview_mode": "conversation",
                    "decision": ai_decision,
                    "summary": "已按全部真实问答与岗位要求形成证据参考分；不要求面试官使用固定题。",
                    "policy": "自由对话评分只基于本轮可复核的岗位相关事实；不按固定题完成度扣分，不自动变更候选人阶段。",
                    "stage_change": "not_performed",
                    "ai_recommendation": {
                        "decision": ai_decision,
                        "label": ai_label,
                        "overall_score": local_score,
                        "confidence": response_quality.get("confidence", 0),
                        "rationale": "综合候选人在面试官实际问题下呈现的岗位相关事实、操作细节、判断依据、责任边界和回答深度；固定题是否提问不参与扣分。",
                        "human_confirmation_required": True,
                        "candidate_stage_changed": False,
                        "planned_question_dependency": False,
                        "evidence_segment_ids": list(response_quality.get("evidence_segment_ids", [])),
                    },
                    "response_quality": response_quality,
                    "answer_logic_review": answer_logic_review,
                    "dialogue_analysis": {
                        "summary": "本轮按面试官实际提问分析，不依赖题库完成度。",
                        "interviewer_question_count": len(interviewer_segments),
                        "candidate_answer_count": len(candidate_segments),
                        "substantive_answer_count": question_analysis["summary"]["evidenced"],
                        "shallow_answer_count": question_analysis["summary"]["shallow"],
                        "observations": observations,
                        "risks": (["部分回答较浅，需要结合原问题进一步核实。"] if shallow_states else []),
                        "positive_evidence": (["候选人已在真实问答中提供可继续核验的岗位相关事实。"] if evidenced_states else []),
                        "notable_excerpts": [
                            {"segment_id": item.id, "quote": item.effective_text[:180]}
                            for item in notable
                        ],
                        "boundary": "仅评价本轮回答呈现出的结构与事实，不推断人格、智力或潜力。",
                    },
                    "evaluation_scope": {
                        "mode": "conversation",
                        "round_label": {"business": "业务面", "hr": "HR 面", "ceo": "CEO 面"}.get(interview.round_type, interview.round_type),
                        "interviewer_names": list(interview.interviewer_names or []),
                        "dimensions": [],
                        "transcript_scope": "本轮全部真实问答",
                        "planned_question_dependency": False,
                    },
                    "question_evidence_summary": question_analysis["summary"],
                    "jd_assessments": [],
                },
                "next_round_questions": [
                    {
                        "competency_id": "dialogue_follow_up",
                        "source_type": "ad_hoc_gap",
                        "source_question_id": item.get("question_id"),
                        "source_question_text": item.get("source_question_text"),
                        "priority": item.get("priority", "normal"),
                        "reason": item.get("reason", "回答仍需核实"),
                        "question": item.get("question", "请补充一个可核实的具体细节。"),
                    }
                    for item in question_analysis["suggestions"][:3]
                ],
            }
        evidence = db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.interview_round_id == interview.id,
                EvidenceItem.human_status != "rejected",
            )
        ).all()
        by_competency: dict[str, list[EvidenceItem]] = defaultdict(list)
        for item in evidence:
            if is_usable_evidence_record(
                quote=item.quote,
                direction=item.direction,
                explanation=item.explanation,
                human_status=item.human_status,
            ):
                by_competency[item.competency_id].append(item)

        scores = []
        missing = []
        for competency in competencies:
            items = by_competency[competency["id"]]
            if not items:
                missing.append(competency)
                scores.append(
                    {
                        "competency_id": competency["id"],
                        "competency_name": competency["name"],
                        "score": None,
                        "assessment": "not_assessed",
                        "evidence_ids": [],
                        "confirmed_evidence_ids": [],
                        "confidence": 0,
                    }
                )
                continue
            weighted = sum((1 if i.direction == "support" else -1) * i.strength for i in items)
            score = max(1, min(5, round(3 + weighted / max(len(items), 1))))
            confirmed_evidence_ids = [
                i.id for i in items if i.human_status in {"confirmed", "modified"}
            ]
            confirmed = len(confirmed_evidence_ids)
            scores.append(
                {
                    "competency_id": competency["id"],
                    "competency_name": competency["name"],
                    "score": score,
                    "assessment": "evidence_available",
                    "evidence_ids": [i.id for i in items],
                    "confirmed_evidence_ids": confirmed_evidence_ids,
                    "confidence": round(min(0.9, 0.35 + 0.15 * len(items) + 0.2 * bool(confirmed)), 2),
                    "needs_human_confirmation": confirmed == 0,
                }
            )

        tracked_states = [
            item
            for item in question_analysis["states"]
            if item["required"] or item["source"] in {
                "resume_jd_match",
                "resume_personalized",
                "prior_round",
                "interviewer_ad_hoc",
            }
        ]
        jd_assessments = []
        for item in tracked_states:
            if item["source"] != "resume_jd_match":
                continue
            status_text = {
                "evidenced": "已有可复核回答，等待人工确认",
                "shallow": "回答信息不足，暂不能判断是否符合",
                "unanswered": "本轮未验证，不能作为负面结论",
            }[item["status"]]
            jd_assessments.append({
                **item,
                "assessment": status_text,
                "needs_human_confirmation": item["status"] == "evidenced",
            })

        suggestion_by_question = {
            item.get("question_id"): item for item in question_analysis["suggestions"]
        }
        next_round_questions = []
        for item in jd_assessments:
            if item["status"] == "evidenced":
                continue
            suggestion = suggestion_by_question.get(item["question_id"], {})
            next_round_questions.append({
                "competency_id": "jd_verification",
                "source_type": "jd_gap",
                "source_question_id": item["question_id"],
                "prior_answer_status": item["status"],
                "evidence_segment_ids": item["evidence_segment_ids"],
                "priority": "high" if item["status"] == "shallow" else "normal",
                "reason": (
                    f"本轮回答较浅，仍缺少{'、'.join(item['missing_dimensions'][:2])}"
                    if item["status"] == "shallow" and item["missing_dimensions"]
                    else "本轮尚未验证该 JD 重点，不能据此形成负面判断"
                ),
                "question": suggestion.get("question", item["question"]),
            })

        active_suggestion_questions = {
            str(item.get("question_id"))
            for item in (interview.suggestion_history or [])
            if item.get("status") == "active" and item.get("question_id")
        }
        for item in tracked_states:
            if (
                item["source"] != "interviewer_ad_hoc"
                or item["status"] != "shallow"
                or item["question_id"] not in active_suggestion_questions
                or len(next_round_questions) >= 5
            ):
                continue
            suggestion = suggestion_by_question.get(item["question_id"], {})
            next_round_questions.append({
                "competency_id": "interviewer_ad_hoc",
                "source_type": "ad_hoc_gap",
                "source_question_id": item["question_id"],
                "prior_answer_status": "shallow",
                "evidence_segment_ids": item["evidence_segment_ids"],
                "priority": "high",
                "reason": f"临场问题的回答仍缺少{'、'.join(item['missing_dimensions'][:2]) or '可核验细节'}",
                "question": suggestion.get("question", "请继续核实这段回答中最影响岗位判断的事实。"),
            })

        for item in missing:
            if len(next_round_questions) >= 5:
                break
            next_round_questions.append({
                "competency_id": item["id"],
                "source_type": "competency_gap",
                "prior_answer_status": "unanswered",
                "evidence_segment_ids": [],
                "priority": "normal",
                "reason": "本轮无可引用证据",
                "question": item.get("question", f"请补充{item['name']}的具体经历。"),
            })

        assessed = len(competencies) - len(missing)
        unresolved_jd = sum(item["status"] != "evidenced" for item in jd_assessments)
        enough = assessed >= max(2, len(competencies) // 2 + len(competencies) % 2) and unresolved_jd == 0
        evidenced_count = sum(item["status"] == "evidenced" for item in tracked_states)
        shallow_count = sum(item["status"] == "shallow" for item in tracked_states)
        unanswered_count = sum(item["status"] == "unanswered" for item in tracked_states)
        required_questions = list((interview.plan_payload or {}).get("required_questions", []))
        required_ids = {str(item.get("id")) for item in required_questions if item.get("id")}
        asked_ids = {item.question_id for item in question_progress if item.asked}
        required_asked = len(required_ids.intersection(asked_ids))
        required_coverage = required_asked / len(required_ids) if required_ids else 1.0
        tracked_answered = evidenced_count + shallow_count
        answer_coverage = tracked_answered / len(tracked_states) if tracked_states else 0.0
        interview_completeness_score = round(
            min(5.0, 1.0 + required_coverage * 2.0 + answer_coverage + min(1.0, len(evidence) / 2)),
            1,
        )
        scored = [item for item in scores if item.get("score") is not None]
        overall_score = (
            round(sum(item["score"] for item in scored) / len(scored), 1)
            if scored
            else None
        )
        confirmed_score_count = sum(bool(item.get("confirmed_evidence_ids")) for item in scored)
        if not enough or overall_score is None:
            ai_decision = "supplementary_interview"
            ai_label = "补充证据后再判断"
            rationale = "当前问题覆盖或岗位证据仍不完整，不能把未回答直接当作不符合。"
        elif overall_score >= 3.5:
            ai_decision = "advance"
            ai_label = "建议进入下一轮"
            rationale = "已覆盖能力项的证据评分达到进入下一轮的参考线，仍需人工核对证据语境。"
        elif overall_score < 2.5 and confirmed_score_count:
            ai_decision = "reject"
            ai_label = "暂不建议进入下一轮"
            rationale = "已确认的岗位相关证据整体偏弱；该建议不能自动改变候选人阶段。"
        else:
            ai_decision = "hold"
            ai_label = "保留讨论"
            rationale = "现有证据强弱并存，建议结合岗位硬要求与下一轮补充问题讨论。"
        ai_recommendation = {
            "decision": ai_decision,
            "label": ai_label,
            "overall_score": overall_score,
            "scored_competencies": len(scored),
            "confirmed_competencies": confirmed_score_count,
            "confidence": round(
                min(0.9, 0.25 + len(scored) * 0.1 + confirmed_score_count * 0.12), 2
            ),
            "rationale": rationale,
            "human_confirmation_required": True,
            "candidate_stage_changed": False,
            "interview_completeness_score": interview_completeness_score,
            "required_questions_asked": required_asked,
            "required_questions_total": len(required_ids),
            "required_question_coverage": round(required_coverage, 2),
            "process_warning": (
                "本轮统一必问题未完整覆盖；这是面试过程缺口，不能作为候选人的负面证据。"
                if required_asked < len(required_ids)
                else None
            ),
        }
        return {
            "rubric_version": "five-level-v0.4",
            "ai_scores": scores,
            "recommendation": {
                "decision": "human_review_required" if enough else "insufficient_evidence",
                "summary": (
                    "已有部分可追溯证据，请面试官核验后作出判断。"
                    if enough
                    else f"当前仍有 {shallow_count} 道回答较浅、{unanswered_count} 道尚未验证，不建议仅凭本轮形成录用或淘汰结论。"
                ),
                "policy": "AI 不自动作出录用、淘汰或阶段变更决定",
                "stage_change": "not_performed",
                "ai_recommendation": ai_recommendation,
                "response_quality": response_quality,
                "answer_logic_review": answer_logic_review,
                "evaluation_scope": evaluation_scope_payload(interview, competencies),
                "question_evidence_summary": {
                    "tracked_total": len(tracked_states),
                    "evidenced": evidenced_count,
                    "shallow": shallow_count,
                    "unanswered": unanswered_count,
                },
                "jd_assessments": jd_assessments,
            },
            "next_round_questions": next_round_questions,
        }

    @staticmethod
    def _evidence_payload(item: EvidenceItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "competency_id": item.competency_id,
            "segment_ids": item.segment_ids,
            "quote": item.quote,
            "direction": item.direction,
            "strength": item.strength,
            "explanation": item.explanation,
            "human_status": item.human_status,
        }

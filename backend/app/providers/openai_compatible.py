from __future__ import annotations

from collections.abc import Iterable
from difflib import SequenceMatcher
import json
import re
from threading import Lock
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    Application,
    Candidate,
    EvidenceItem,
    InterviewRound,
    Job,
    TranscriptSegment,
    new_id,
    utc_now,
)
from ..services.company_profile import active_company_profile
from ..services.answer_logic import ANSWER_LOGIC_BOUNDARY, quotes_for_segments
from ..services.evaluation_scope import round_evaluation_dimensions
from ..services.job_semantics import (
    build_local_job_semantic_profile,
    job_semantic_schema,
    normalize_job_semantic_profile,
)
from ..services.utterance_quality import (
    best_substantive_quote,
    describes_absence_instead_of_behavior,
    is_evidence_worthy_utterance,
    is_substantive_utterance,
    is_usable_evidence_record,
    substantive_character_count,
    trim_leading_fillers,
)
from .mock import MockIntelligenceProvider


LIVE_EVIDENCE_GAPS = {
    "personal_action",
    "ownership_boundary",
    "mechanism",
    "decision_basis",
    "result",
    "metric",
    "constraint",
    "reflection",
    "behavior_evidence",
    "risk_clarification",
    "consistency",
    "other",
}
FORBIDDEN_DECISION_TERMS = ("建议录用", "建议淘汰", "应该录用", "应该淘汰", "不予录用", "阶段变更")


class IntelligenceProviderError(RuntimeError):
    """Sanitized provider failure that is safe to expose as a degraded status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OpenAICompatibleProvider:
    """Evidence-constrained LLM adapter with deterministic fallback.

    The remote model may propose evidence and follow-up questions, but every quote
    and identifier is validated against local interview data before it is saved.
    Numeric scores and hiring decisions remain under the existing human workflow.
    """

    def __init__(
        self,
        settings: Settings,
        fallback: MockIntelligenceProvider | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not settings.llm_configured:
            raise ValueError("LLM settings are incomplete")
        self.settings = settings
        self.fallback = fallback or MockIntelligenceProvider()
        self.client = client or httpx.Client(timeout=settings.llm_timeout_seconds)
        self.name = f"openai-compatible:{settings.llm_model}"
        self._live_failure_counts: dict[str, int] = {}
        self._live_answer_sizes: dict[str, int] = {}
        self._live_state_lock = Lock()

    def analyze_live(
        self,
        db: Session,
        interview: InterviewRound,
        job: Job,
        latest_segment: TranscriptSegment | None = None,
    ) -> dict[str, Any]:
        # In production, deterministic rules provide coverage and a safe fallback,
        # but they must not persist keyword-only evidence before semantic review.
        baseline = self.fallback.analyze_live(db, interview, job)
        if not latest_segment or latest_segment.speaker_role != "candidate":
            # Keep subtitles responsive without publishing a template question
            # before semantic analysis has understood the answer.
            baseline["suggestions"] = []
            baseline["availability"] = "semantic_analysis_pending"
            return self._with_status(baseline, "ready")

        segments = self._segments(db, interview.id)
        answer_context = _current_candidate_answer_context(segments, baseline)
        if answer_context["character_count"] < 10:
            baseline["suggestions"] = []
            baseline["availability"] = "semantic_analysis_pending"
            return self._with_status(baseline, "ready")
        if not self._should_enrich_live(
            interview.id,
            str(baseline.get("active_question_id") or "conversation"),
            int(answer_context["character_count"]),
        ):
            # Between semantic refreshes, keep a stable local shallow-answer
            # prompt instead of clearing the right panel on every ASR fragment.
            baseline["availability"] = (
                "local_followup_ready"
                if baseline.get("suggestions")
                else "semantic_analysis_pending"
            )
            return self._with_status(baseline, "ready")
        application = db.get(Application, interview.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        conversation_mode = interview.interview_mode == "conversation"
        competencies = [] if conversation_mode else round_evaluation_dimensions(db, interview, job)
        questions = [] if conversation_mode else list((interview.plan_payload or {}).get("questions", []))
        question_context = _live_question_context(questions, baseline)
        active_profile = active_company_profile(db)
        prompt_payload = {
            "job_title": job.title,
            "round_type": interview.round_type,
            "analysis_method": (
                "理解面试官实际问题和候选人回答，寻找事实与回答深度缺口，不匹配能力维度。"
                if conversation_mode
                else "理解回答语义后，对照可观察行为标准寻找证据缺口；关键词只能用于导航，不能单独作为证据。"
            ),
            "competencies": [_live_competency_payload(item) for item in competencies],
            "questions": question_context,
            "company_policy": {
                "profile_version": active_profile.version_label if active_profile else None,
                "red_lines": list((active_profile.profile_payload or {}).get("red_lines", [])) if active_profile else [],
                "anti_bias_boundary": (active_profile.profile_payload or {}).get("anti_bias_boundary", []) if active_profile else [],
            },
            "transcript_correction_context": {
                "candidate_name": candidate.display_name if candidate else "",
                "resume_reference": (candidate.resume_text[:2200] if candidate else ""),
                "jd_reference": job.jd_text[:2200],
                "policy": "只修正上下文能够高置信确认的同音字、专有名词和明显断句错误；不得润色或改写原意。",
            },
            "transcript": [
                {
                    "segment_id": item.id,
                    "speaker_role": item.speaker_role,
                    "text": item.effective_text,
                    "text_raw": item.text_raw,
                }
                for item in segments
            ],
            "latest_segment_id": latest_segment.id,
            "current_answer_context": answer_context,
        }
        try:
            output = self._chat_json(
                instructions=(
                    "你是人工面试官的实时思考伙伴，不是 STAR 问题生成器。必须先理解面试官究竟在问什么、候选人的回答解决了什么、暴露了什么关键认知。"
                    + ("当前是自由对话分析模式：只跟随面试官实际提出的问题，不得匹配、评分或补造任何能力维度。" if conversation_mode else "")
                    + "只有追问能显著改变面试官对候选人的认知时才返回一条建议；没有高价值追问时必须返回空数组。"
                    "优先寻找回答内部矛盾、关键机制、本人判断与团队方案的边界、取舍依据、失败原因、认知上限，以及岗位本质与候选人经历之间的落差。"
                    "‘结果、数字、本人行动’不是每道题都必须补齐。职业规划、兴趣爱好、寒暄、确认信息、候选人反问等回答，禁止套用项目结果或量化指标追问。"
                    "嗯、哦、好的、然后呢等口头语或确认词不是回答、不是证据，也不得作为 basis_quote。"
                    "先识别实际问题意图：动机看选择依据和持续行为，技术看机制与排查路径，亲力亲为看责任边界和操作细节，判断力看备选方案与取舍；只有成果主张或 KPI 相关问题才核实结果或指标。"
                    "禁止生成‘这件事最后具体变成了什么结果’‘你本人具体做了哪一步’‘用什么数字判断有效’及其同义改写，除非当前问题本身就在核实项目交付，且该信息会实质影响判断。"
                    "如果面试官已经提出新问题，必须跟随最新问题，不得强迫对话返回旧的浅答问题；旧缺口只能留作稍后可选提醒。"
                    "面试官临场提出的问题与题库问题同等有效，必须按实际对话单独理解，不得错误归入上一道预设题。"
                    "实时 ASR 可能把一句回答切成多个短片段。必须结合 current_answer_context 理解完整回答，不能因为单个片段短就判定没有回答。"
                    "如果候选人声称工具已经上线、自动运行或可供他人使用，且该主张影响岗位判断，应优先从权限与数据安全、稳定性、持久化、并发、监控或故障恢复中选择一个最关键的生产化边界当场核实；不要罗列检查清单。"
                    "每条建议必须绑定输入中的 question_id、competency_id 和 candidate 片段，basis_quote 必须逐字复制连续原文。"
                    "追问必须直接承接 basis_quote 的具体含义，不能只根据能力缺口生成一条与回答无关的通用问题。"
                    "只分析给定逐字稿，不得补充未出现的事实。"
                    "同时检查最近逐字稿中的明显语音识别错误。只有能被相邻上下文、简历或 JD 高置信确认时才能修正；"
                    "修正必须保留原句含义和口语风格，不得总结、润色、补全候选人未说出的内容。没有可靠修正时返回空数组。"
                    "证据 quote 必须逐字复制某个 candidate 片段中包含事实、判断、行动、理由或明确立场的连续原文，segment_id 和 competency_id 必须来自输入。"
                    "信息缺失、回答含糊或无法判断只能写入建议理由，不能作为 support、negative 或 neutral 证据；只有候选人明确表现出的行为才可形成证据。"
                    "建议只能帮助继续核实，不能给出录用、淘汰、阶段变更或人格判断。"
                ),
                payload=prompt_payload,
                schema_name="interview_live_assistance",
                schema=_live_schema(),
            )
            transcript_corrections = self._apply_validated_transcript_corrections(
                segments, output.get("transcript_corrections", [])
            )
            db.flush()
            self._persist_validated_evidence(db, interview, competencies, segments, output.get("evidence", []))
            refreshed = self.fallback.analyze_live(db, interview, job)
            refreshed["suggestions"] = self._validated_suggestions(
                output.get("suggestions", []),
                [],
                competencies,
                question_context,
                segments,
                answer_context,
            )
            refreshed["transcript_corrections"] = transcript_corrections
            self._reset_live_failures(interview.id)
            return self._with_status(refreshed, "active")
        except IntelligenceProviderError as error:
            degraded = self.fallback.analyze_live(db, interview, job, latest_segment)
            failures = self._record_live_failure(interview.id)
            return self._degraded(
                degraded,
                error,
                status="recovering" if failures < 3 else "degraded",
            )

    def _should_enrich_live(
        self,
        interview_id: str,
        question_id: str,
        character_count: int,
    ) -> bool:
        """Debounce semantic work by logical answer growth, not ASR fragment count."""
        key = f"{interview_id}:{question_id}"
        with self._live_state_lock:
            previous = self._live_answer_sizes.get(key)
            if previous is not None and character_count - previous < 18:
                return False
            self._live_answer_sizes[key] = character_count
            return True

    def _apply_validated_transcript_corrections(
        self,
        segments: list[TranscriptSegment],
        proposed: Iterable[Any],
    ) -> list[dict[str, Any]]:
        """Persist only conservative ASR repairs while retaining the raw text."""
        by_id = {item.id: item for item in segments}
        output: list[dict[str, Any]] = []
        for item in proposed:
            if not isinstance(item, dict):
                continue
            segment = by_id.get(str(item.get("segment_id", "")))
            corrected = str(item.get("corrected_text", "")).strip()
            try:
                confidence = float(item.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            if not segment or confidence < 0.86 or not corrected:
                continue
            source = segment.effective_text.strip()
            if corrected == source or not source:
                continue
            length_ratio = len(corrected) / max(1, len(source))
            similarity = SequenceMatcher(None, source, corrected).ratio()
            if not 0.65 <= length_ratio <= 1.35 or similarity < 0.55:
                continue
            segment.text_corrected = corrected
            segment.corrected_by = f"{self.name}:context-correction"
            segment.corrected_at = utc_now()
            output.append(
                {
                    "segment_id": segment.id,
                    "corrected_text": corrected,
                    "confidence": round(confidence, 2),
                    "reason": str(item.get("reason", "上下文语音识别修正"))[:120],
                }
            )
        return output

    def analyze_job_definition(self, title: str, jd_text: str) -> dict[str, Any]:
        fallback = build_local_job_semantic_profile(title, jd_text)
        try:
            output = self._chat_json(
                instructions=(
                    "你是招聘岗位定义分析助手。请理解完整 JD 的岗位使命、业务结果、真实工作场景和成功证据，"
                    "不要把分词、短语命中或原文中的任意名词直接当成能力。"
                    "生成 7 个面试验证维度：业务面 3 个、HR 面 2 个、CEO 面 2 个；每个维度必须引用 JD 中连续的 source_excerpt。"
                    "问题必须要求候选人提供具体情境、本人行动、结果或取舍证据，而不是询问是否掌握某个关键词。"
                    "性别、年龄、婚育、籍贯、特定学校、外貌或身体条件不得成为维度、问题或录用标准；即使 JD 原文出现也必须忽略。"
                    "不得凭空添加 JD 未写明的年限、数量、百分比或‘至少/至多’等门槛；不得用抽象的文化契合、价值观匹配或性格匹配代替可观察工作行为。"
                    "不得虚构职责、汇报关系、指标或技术要求。"
                ),
                payload={"job_title": title, "jd_text": jd_text},
                schema_name="job_semantic_profile",
                schema=job_semantic_schema(),
                timeout_seconds=max(self.settings.llm_timeout_seconds, 45),
                max_tokens=3200,
            )
            return normalize_job_semantic_profile(
                output,
                title=title,
                jd_text=jd_text,
                provider=self.name,
            )
        except IntelligenceProviderError as error:
            fallback["model_assistance"] = {
                "status": "fallback",
                "error_code": error.code,
            }
            return fallback

    def refine_interview_plan(
        self,
        db: Session,
        interview: InterviewRound,
        job: Job,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace template CV questions with model-designed, resume-grounded probes."""
        application = db.get(Application, interview.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        if not candidate or candidate.source == "demo" or interview.interview_mode == "conversation":
            return self._replace_resume_plan_questions(plan, [], status="not_applicable")

        resume_reference = _clean_resume_reference(candidate.resume_text)[:7600]
        dimensions = [
            item
            for item in (job.semantic_profile or {}).get("interview_dimensions", [])
            if item.get("round_type") == interview.round_type
        ]
        dimension_payload = [
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "job_context": str(item.get("job_context") or item.get("source_excerpt") or "")[:360],
                "evidence_target": str(item.get("evidence_target", ""))[:240],
            }
            for item in dimensions
        ]
        try:
            output = self._chat_json(
                instructions=(
                    "你是资深招聘负责人和岗位专家。请同时理解完整 JD、候选人简历和当前面试轮次，设计 0 到 2 道真正值得问的问题。"
                    "先判断这个岗位最本质的工作矛盾，再从简历中选择最能验证该矛盾的真实经历；不得因为出现同一个技术名词就认为经历相关。"
                    "问题要区分‘会复述概念’与‘真正理解并做过关键判断’，优先追问设计选择、机制、失败原因、取舍、认知边界或经历与岗位之间的关键落差。"
                    "对于管培生和应届生，不假设成熟工作经验；可以深入课程、个人项目、比赛或实习，但问题仍需检验其是否能把技术用于真实问题。"
                    "每道题只问一个核心问题，使用自然口语，候选人听一遍就能明白。"
                    "禁止使用‘你实际负责哪一部分’‘最后有什么结果’‘为什么愿意投入’‘讲一个具体案例’等通用模板，禁止机械要求 STAR、数字或结果。"
                    "resume_quote 必须逐字复制简历中的一段连续原文；无法找到高价值切入点时返回空数组，不能硬凑问题。"
                ),
                payload={
                    "candidate_stage": plan.get("preparation_context", {}).get("candidate_stage"),
                    "job_title": job.title,
                    "round_type": interview.round_type,
                    "jd": job.jd_text[:3800],
                    "resume": resume_reference,
                    "round_dimensions": dimension_payload,
                },
                schema_name="resume_grounded_interview_questions",
                schema=_resume_question_schema(),
                timeout_seconds=max(self.settings.llm_timeout_seconds, 20),
                max_tokens=1400,
                model=self._planning_model(),
            )
        except IntelligenceProviderError as error:
            return self._replace_resume_plan_questions(
                plan, [], status="degraded", error_code=error.code
            )

        dimensions_by_id = {str(item.get("id")): item for item in dimensions}
        forbidden = (
            "你实际负责哪一部分",
            "最后有什么结果",
            "为什么愿意投入",
            "讲一个具体案例",
            "这件事最后具体变成",
        )
        questions: list[dict[str, Any]] = []
        for index, item in enumerate(output.get("questions", [])[:2]):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("resume_quote", "")).strip()
            question = str(item.get("question", "")).strip()
            follow_up = str(item.get("follow_up", "")).strip()
            dimension_id = str(item.get("dimension_id", ""))
            dimension = dimensions_by_id.get(dimension_id)
            if (
                not dimension
                or len(quote) < 8
                or quote not in resume_reference
                or not 10 <= len(question) <= 220
                or any(value in question for value in forbidden)
            ):
                continue
            questions.append(
                {
                    "id": f"q-{interview.round_type}-semantic-resume-{index + 1}",
                    "competency_id": f"jd_semantic.{dimension_id}",
                    "competency_name": f"简历深挖 · {dimension.get('name') or '岗位关键判断'}",
                    "question": question,
                    "follow_up": follow_up[:240] or "根据候选人的回答，只追问最关键的判断依据。",
                    "required": False,
                    "source": "resume_jd_match",
                    "generation_mode": "llm_semantic",
                    "rationale": str(item.get("why_this_matters", ""))[:300],
                    "source_evidence": f"简历原文：{quote}",
                    "keywords": [],
                    "jd_analysis_mode": (job.semantic_profile or {}).get("analysis_mode"),
                }
            )
        return self._replace_resume_plan_questions(plan, questions, status="active")

    @staticmethod
    def _replace_resume_plan_questions(
        plan: dict[str, Any],
        questions: list[dict[str, Any]],
        *,
        status: str,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        output = dict(plan)
        existing_resume_questions = [
            item
            for item in output.get("questions", [])
            if item.get("source") == "resume_jd_match"
        ]
        retained = [
            item for item in output.get("questions", [])
            if item.get("source") != "resume_jd_match"
        ]
        # The local planner has already created resume-grounded questions.
        # A semantic result may replace them only when at least one validated
        # question survives. Empty/invalid model output must never erase a
        # useful candidate-specific preparation plan.
        selected_questions = questions or existing_resume_questions
        combined = [*retained, *selected_questions]
        output["questions"] = combined
        output["required_questions"] = [item for item in combined if item.get("required")]
        output["optional_questions"] = [item for item in combined if not item.get("required")]
        mix = dict(output.get("question_mix", {}))
        mix["resume_jd_match"] = len(selected_questions)
        output["question_mix"] = mix
        if questions:
            effective_status = "active"
        elif existing_resume_questions:
            effective_status = "fallback_preserved"
        elif status == "degraded":
            effective_status = "degraded"
        else:
            effective_status = "no_grounded_match"
        output["semantic_question_assistance"] = {
            "status": effective_status,
            "provider": "llm_semantic" if questions else "local_resume_grounding" if existing_resume_questions else None,
            "error_code": error_code,
            "model_question_count": len(questions),
            "fallback_question_count": len(existing_resume_questions) if not questions else 0,
        }
        output["personalization_version"] = "resume-semantic-deep-dive-v1.2"
        return output

    def draft_scorecard(self, db: Session, interview: InterviewRound, job: Job) -> dict[str, Any]:
        baseline = self.fallback.draft_scorecard(db, interview, job)
        if interview.interview_mode == "conversation":
            dialogue_segments = self._segments(db, interview.id, limit=None)
            if not dialogue_segments:
                baseline["model_assistance"] = self._status("ready")
                return baseline
            try:
                judgment = self._assess_free_dialogue(
                    db, interview, job, dialogue_segments
                )
            except IntelligenceProviderError as error:
                recommendation = baseline["recommendation"]
                recommendation["decision"] = "insufficient_evidence"
                recommendation["summary"] = "真实语义模型未完成本轮岗位证据评分；请恢复模型服务后重新生成。"
                recommendation["ai_recommendation"] = {
                    "decision": "insufficient_evidence",
                    "label": "AI 语义评分暂不可用",
                    "overall_score": None,
                    "confidence": 0,
                    "rationale": "为避免把本地关键词和回答长度误当成岗位判断，本次不输出替代分数。",
                    "human_confirmation_required": True,
                    "candidate_stage_changed": False,
                    "planned_question_dependency": False,
                    "evidence_segment_ids": [],
                }
                baseline["model_assistance"] = self._status("degraded", error.code)
                return baseline
            if judgment is None:
                baseline["model_assistance"] = self._status("degraded", "free_dialogue_review_unavailable")
                return baseline
            recommendation = baseline["recommendation"]
            recommendation["decision"] = judgment["decision"]
            recommendation["summary"] = judgment["rationale"]
            recommendation["ai_recommendation"] = {
                "decision": judgment["decision"],
                "label": judgment["label"],
                "overall_score": judgment["overall_score"],
                "confidence": judgment["confidence"],
                "rationale": judgment["rationale"],
                "human_confirmation_required": True,
                "candidate_stage_changed": False,
                "planned_question_dependency": False,
                "evidence_segment_ids": judgment["evidence_segment_ids"],
            }
            recommendation["dialogue_analysis"]["summary"] = judgment["rationale"]
            recommendation["dialogue_analysis"]["observations"] = judgment["positive_evidence"]
            recommendation["dialogue_analysis"]["positive_evidence"] = judgment["positive_evidence"]
            recommendation["dialogue_analysis"]["risks"] = judgment["risks"]
            recommendation["conversation_assessment"] = judgment["batch_status"]
            if judgment["next_round_questions"]:
                baseline["next_round_questions"] = judgment["next_round_questions"]
            self._attach_answer_logic_review(
                baseline, interview, job, dialogue_segments
            )
            baseline["model_assistance"] = self._status(
                "active" if judgment["batch_status"]["failed_batches"] == 0 else "partial"
            )
            return baseline
        all_segments = self._segments(db, interview.id, limit=None)
        segments = all_segments[-24:]
        evidence = [
            item
            for item in db.scalars(
                select(EvidenceItem).where(
                    EvidenceItem.interview_round_id == interview.id,
                    EvidenceItem.human_status != "rejected",
                )
            ).all()
            if is_usable_evidence_record(
                quote=item.quote,
                direction=item.direction,
                explanation=item.explanation,
                human_status=item.human_status,
            )
        ]
        if not all_segments:
            baseline["model_assistance"] = self._status("ready")
            return baseline

        competencies = round_evaluation_dimensions(db, interview, job)
        assessments, batch_status = self._assess_full_conversation(
            interview,
            job,
            competencies,
            all_segments,
        )
        if assessments:
            self._apply_conversation_assessments(
                db,
                interview,
                competencies,
                all_segments,
                assessments,
                baseline,
            )
            evidence = [
                item
                for item in db.scalars(
                    select(EvidenceItem).where(
                        EvidenceItem.interview_round_id == interview.id,
                        EvidenceItem.human_status != "rejected",
                    )
                ).all()
                if is_usable_evidence_record(
                    quote=item.quote,
                    direction=item.direction,
                    explanation=item.explanation,
                    human_status=item.human_status,
                )
            ]
        baseline["recommendation"]["conversation_assessment"] = {
            **batch_status,
            "transcript_segment_count": len(all_segments),
            "candidate_segment_count": sum(
                item.speaker_role == "candidate" for item in all_segments
            ),
            "planned_question_dependency": False,
            "policy": "结束后按本轮全部问答重新分析；预设题、临场问题和自然追问具有同等证据资格。",
        }
        prompt_payload = {
            "job_title": job.title,
            "round_type": interview.round_type,
            "competencies": [{"id": item["id"], "name": item["name"]} for item in competencies],
            "confirmed_or_pending_evidence": [
                {
                    "evidence_id": item.id,
                    "competency_id": item.competency_id,
                    "quote": item.quote,
                    "human_status": item.human_status,
                }
                for item in evidence
            ],
            "transcript": [
                {"segment_id": item.id, "speaker_role": item.speaker_role, "text": item.effective_text}
                for item in segments
            ],
            "local_response_quality_reference": baseline["recommendation"].get("response_quality"),
        }
        try:
            output = self._chat_json(
                instructions=(
                    "你是人工面试评价的证据整理助手。只依据输入材料概括，不得猜测。"
                    "面试官是否使用预设题不影响证据资格；所有临场问题与自然追问都必须按上下文理解。"
                    "可以对候选人回答中可观察的事实与操作细节、判断依据、责任边界、边界与修正、表达清晰度给出1至5分的回答质量参考分，"
                    "结果和量化指标只在原问题或候选人的成果主张需要核实时使用，不得把 STAR 完整度当作通用回答质量。"
                    "口头语、确认词和候选人反问不得作为 response_quality 或能力证据；信息缺失只能写入 limitations，不能伪装成负面证据。"
                    "但不得把它表述为智力、人格或潜力，不得据此直接生成录用或淘汰结论；summary 必须明确仍需人工确认。"
                    "response_quality 的 evidence_segment_ids 只能引用候选人片段，且至少一条；没有候选人片段时不要虚构。"
                    "追问用于补足证据，引用 evidence_ids 时只能使用输入中的 ID。"
                ),
                payload=prompt_payload,
                schema_name="interview_scorecard_assistance",
                schema=_scorecard_schema(),
            )
            summary = str(output.get("summary", "")).strip()
            if 10 <= len(summary) <= 500:
                baseline["recommendation"]["summary"] = summary
            validated_response_quality = self._validated_response_quality(
                output.get("response_quality"), segments
            )
            if validated_response_quality is not None:
                baseline["recommendation"]["response_quality"] = validated_response_quality
            baseline["next_round_questions"] = self._validated_next_questions(
                output.get("next_round_questions", []),
                competencies,
                {item.id for item in evidence},
                baseline.get("next_round_questions", []),
            )
            self._attach_answer_logic_review(
                baseline, interview, job, all_segments
            )
            baseline["model_assistance"] = self._status(
                "active" if batch_status["failed_batches"] == 0 else "partial"
            )
            return baseline
        except IntelligenceProviderError as error:
            baseline["model_assistance"] = self._status(
                "partial" if assessments else "degraded", error.code
            )
            return baseline

    def _attach_answer_logic_review(
        self,
        baseline: dict[str, Any],
        interview: InterviewRound,
        job: Job,
        segments: list[TranscriptSegment],
    ) -> None:
        local_review = baseline.get("recommendation", {}).get("answer_logic_review")
        candidate_count = sum(
            item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
            for item in segments
        )
        if candidate_count < 2:
            return
        try:
            output = self._chat_json(
                instructions=(
                    "你是面试回答逻辑与可信度核验助手。无论面试官是否使用固定题，都要理解真实问题语境，"
                    "检查候选人回答中的因果连贯性、时间线一致性、本人贡献与团队贡献边界、成果口径和跨回答表述一致性。"
                    "只能引用 speaker_role=candidate 的片段 ID；面试官的话只能用于理解语境。"
                    "不得根据语速、停顿、口音、紧张、表情或措辞习惯推断欺骗，不得输出‘撒谎、说谎、造假、欺骗’等定性。"
                    "只有两段候选人原话对同一事实出现直接冲突时，才能标记 factual_conflict、timeline_conflict 或 ownership_shift，"
                    "且必须同时引用至少两个不同片段。面试官改变假设条件、候选人主动修正或补充细节，不应自动视为矛盾。"
                    "单个成果主张缺少核验材料时只能标记 claim_needs_verification，不能判断其为假。"
                    "回答没有涉及某项内容属于 unknown，不是风险。没有真实异常时 consistency_flags 必须为空。"
                    "logic_score 只评价本轮回答文本的逻辑可追溯程度，不得直接用于录用或淘汰决定。"
                ),
                payload={
                    "job_title_context_only": job.title,
                    "round_type": interview.round_type,
                    "transcript": [
                        {
                            "segment_id": item.id,
                            "speaker_role": item.speaker_role,
                            "text": item.effective_text[:600],
                        }
                        for item in segments
                    ],
                },
                schema_name="answer_logic_and_consistency_review",
                schema=_answer_logic_schema(),
                timeout_seconds=max(self.settings.llm_timeout_seconds, 35),
                max_tokens=2200,
                model=self._planning_model(),
            )
        except IntelligenceProviderError as error:
            if isinstance(local_review, dict):
                local_review["status"] = "semantic_unavailable"
                local_review["error_code"] = error.code
                local_review["summary"] = "语义一致性核验暂未完成；不会使用本地关键词或语音表现替代判断。"
            return
        validated = self._validated_answer_logic(output, segments)
        if validated is not None:
            baseline["recommendation"]["answer_logic_review"] = validated

    @staticmethod
    def _validated_answer_logic(
        proposed: Any,
        segments: list[TranscriptSegment],
    ) -> dict[str, Any] | None:
        if not isinstance(proposed, dict):
            return None
        candidate_segments = {
            item.id: item
            for item in segments
            if item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
        }
        if len(candidate_segments) < 2 or not proposed.get("sufficient_evidence"):
            return None
        try:
            logic_score = int(proposed.get("logic_score"))
            proposed_confidence = float(proposed.get("confidence", 0))
        except (TypeError, ValueError):
            return None
        if not 1 <= logic_score <= 5:
            return None
        allowed_dimensions = {
            "causal_coherence": "因果连贯性",
            "timeline_consistency": "时间线一致性",
            "ownership_consistency": "责任边界一致性",
            "claim_calibration": "成果口径可核验性",
            "cross_answer_consistency": "跨回答一致性",
        }
        allowed_statuses = {"coherent", "needs_verification", "unknown"}
        dimensions = []
        for item in proposed.get("dimensions", [])[:5]:
            if not isinstance(item, dict):
                continue
            dimension_id = str(item.get("id", ""))
            status = str(item.get("status", ""))
            if dimension_id not in allowed_dimensions or status not in allowed_statuses:
                continue
            referenced = list(
                dict.fromkeys(
                    str(value)
                    for value in item.get("segment_ids", [])
                    if str(value) in candidate_segments
                )
            )[:4]
            dimensions.append(
                {
                    "id": dimension_id,
                    "name": allowed_dimensions[dimension_id],
                    "status": status,
                    "explanation": str(item.get("explanation", ""))[:300],
                    "segment_ids": referenced,
                    "quotes": quotes_for_segments(referenced, candidate_segments),
                }
            )
        conflict_types = {"factual_conflict", "timeline_conflict", "ownership_shift"}
        allowed_flag_types = conflict_types | {
            "causal_gap",
            "claim_needs_verification",
        }
        forbidden_labels = ("撒谎", "说谎", "造假", "欺骗", "骗子")
        flags = []
        for item in proposed.get("consistency_flags", [])[:5]:
            if not isinstance(item, dict):
                continue
            flag_type = str(item.get("flag_type", ""))
            description = str(item.get("description", "")).strip()
            referenced = list(
                dict.fromkeys(
                    str(value)
                    for value in item.get("segment_ids", [])
                    if str(value) in candidate_segments
                )
            )[:4]
            if (
                flag_type not in allowed_flag_types
                or not description
                or any(label in description for label in forbidden_labels)
                or not referenced
                or (flag_type in conflict_types and len(referenced) < 2)
            ):
                continue
            question = str(item.get("verification_question", "")).strip()
            if len(question) < 6:
                continue
            flags.append(
                {
                    "flag_type": flag_type,
                    "severity": item.get("severity") if item.get("severity") in {"high", "medium", "low"} else "medium",
                    "description": description[:300],
                    "segment_ids": referenced,
                    "quotes": quotes_for_segments(referenced, candidate_segments),
                    "verification_question": question[:300],
                    "decision_impact": "仅提示复核，不直接计入录用或淘汰结论",
                }
            )
        verification_questions = list(
            dict.fromkeys(
                str(value).strip()[:300]
                for value in proposed.get("verification_questions", [])
                if len(str(value).strip()) >= 6
            )
        )[:5]
        verification_questions = list(
            dict.fromkeys(
                [*verification_questions, *(item["verification_question"] for item in flags)]
            )
        )[:5]
        evidence_segment_ids = list(
            dict.fromkeys(
                segment_id
                for item in dimensions
                for segment_id in item["segment_ids"]
            )
        )
        evidence_segment_ids = list(
            dict.fromkeys(
                [
                    *evidence_segment_ids,
                    *(segment_id for item in flags for segment_id in item["segment_ids"]),
                ]
            )
        )[:12]
        confidence = round(
            min(0.9, max(0.2, proposed_confidence), 0.35 + 0.08 * len(evidence_segment_ids)),
            2,
        )
        return {
            "status": "model_assessed",
            "sufficient_evidence": True,
            "logic_score": logic_score,
            "confidence": confidence,
            "label": str(proposed.get("label") or "回答逻辑语义核验")[:80],
            "summary": str(proposed.get("summary") or "已完成回答逻辑与跨回答一致性核验。")[:500],
            "dimensions": dimensions,
            "consistency_flags": flags,
            "verification_questions": verification_questions,
            "evidence_segment_ids": evidence_segment_ids,
            "boundary": ANSWER_LOGIC_BOUNDARY,
        }

    def _chat_json(
        self,
        *,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        selected_model = model or self.settings.llm_model
        request_payload = {
            "model": selected_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{instructions}\n\n"
                        "输出要求：只返回一个有效 JSON 对象，不要使用 Markdown 代码块，"
                        "不要附加解释文字；字段和值必须严格符合下面的 JSON Schema。\n"
                        f"JSON Schema：{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
                    ),
                },
                {
                    "role": "user",
                    "content": _bounded_json(payload, self.settings.llm_max_context_chars),
                },
            ],
            "temperature": 0.1,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        if (
            "deepseek.com" in self.settings.llm_base_url.lower()
            or str(selected_model).lower().startswith("deepseek")
        ):
            # DeepSeek V4 enables thinking by default. These requests require
            # short, schema-constrained output rather than a long reasoning
            # trace, so non-thinking mode is both faster and less failure-prone.
            request_payload["thinking"] = {"type": "disabled"}
        if max_tokens is not None:
            request_payload["max_tokens"] = max_tokens
        response = self._post(request_payload, timeout_seconds=timeout_seconds)
        if response.status_code == 400:
            request_payload["response_format"] = {"type": "json_object"}
            response = self._post(request_payload, timeout_seconds=timeout_seconds)
        if response.status_code == 402:
            raise IntelligenceProviderError("insufficient_balance", "模型账户余额不足")
        if response.status_code in {401, 403}:
            raise IntelligenceProviderError("authentication_error", "模型鉴权失败")
        if response.status_code == 429:
            raise IntelligenceProviderError("rate_limited", "模型请求过于频繁")
        if response.status_code >= 400:
            raise IntelligenceProviderError("upstream_error", "模型服务暂时不可用")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            parsed = json.loads(_strip_code_fence(str(content)))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise IntelligenceProviderError("invalid_response", "模型返回内容无法验证") from error
        if not isinstance(parsed, dict):
            raise IntelligenceProviderError("invalid_response", "模型返回内容无法验证")
        return parsed

    def _planning_model(self) -> str | None:
        if self.settings.llm_planning_model:
            return self.settings.llm_planning_model
        if (
            "deepseek.com" in self.settings.llm_base_url.lower()
            and self.settings.llm_model == "deepseek-v4-flash"
        ):
            return "deepseek-v4-pro"
        return self.settings.llm_model

    def _post(
        self, payload: dict[str, Any], *, timeout_seconds: float | None = None
    ) -> httpx.Response:
        try:
            return self.client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.llm_api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout_seconds or self.settings.llm_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as error:
            raise IntelligenceProviderError("connection_error", "无法连接模型服务") from error

    def _segments(
        self,
        db: Session,
        interview_id: str,
        *,
        limit: int | None = 24,
    ) -> list[TranscriptSegment]:
        statement = (
            select(TranscriptSegment)
            .where(
                TranscriptSegment.interview_round_id == interview_id,
                TranscriptSegment.is_final.is_(True),
            )
            .order_by(TranscriptSegment.start_ms.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = db.scalars(statement).all()
        return list(reversed(rows))

    def _assess_full_conversation(
        self,
        interview: InterviewRound,
        job: Job,
        competencies: list[dict[str, Any]],
        segments: list[TranscriptSegment],
    ) -> tuple[list[dict[str, Any]], dict[str, int | str]]:
        batches = _conversation_batches(
            segments,
            max_chars=max(
                2400,
                min(7600, self.settings.llm_max_context_chars - 3800),
            ),
        )
        assessments: list[dict[str, Any]] = []
        failed = 0
        for index, batch in enumerate(batches):
            try:
                output = self._chat_json(
                    instructions=(
                        "你是分轮面试的全量对话证据评估助手。当前评价维度已经按业务面、HR 面或 CEO 面区分。"
                        "必须分析本批次中的全部真实问答；预设题、面试官临场问题和自然追问具有同等资格，不得因为没有按题库提问就忽略候选人回答。"
                        "面试官原话只用于理解问题语境，不能作为候选人的能力证据。每项评分必须引用 candidate 片段 ID。"
                        "只输出本批次有可观察证据的维度；没有证据的维度不要强行评分。1 分表示明确风险，3 分表示基本达到，5 分表示复杂约束下稳定表现。"
                        "1 分必须来自候选人明确表现出的风险行为或错误判断；未回答、回答短、口头语多、未提及某项内容只能形成 limitations，不能构成低分证据。"
                        "嗯、哦、好的、候选人反问和纯确认信息不得引用为能力证据。优先引用能体现事实、机制、判断依据、责任边界、取舍或修正的片段。"
                        "不得基于姓名、年龄、性别、婚育、学校或公司光环评分，也不得推断人格、智力或潜力。"
                    ),
                    payload={
                        "job_title": job.title,
                        "round_type": interview.round_type,
                        "interviewer_names": list(interview.interviewer_names or []),
                        "batch": {"index": index + 1, "total": len(batches)},
                        "competencies": [
                            _live_competency_payload(item) for item in competencies
                        ],
                        "transcript": [
                            {
                                "segment_id": item.id,
                                "speaker_role": item.speaker_role,
                                "text": item.effective_text,
                            }
                            for item in batch
                        ],
                    },
                    schema_name="full_conversation_competency_assessment",
                    schema=_conversation_assessment_schema(),
                    timeout_seconds=max(self.settings.llm_timeout_seconds, 35),
                    max_tokens=2600,
                )
                assessments.extend(output.get("competency_assessments", []))
            except IntelligenceProviderError:
                failed += 1
        return assessments, {
            "status": (
                "complete"
                if batches and failed == 0
                else "partial"
                if assessments
                else "unavailable"
            ),
            "total_batches": len(batches),
            "completed_batches": len(batches) - failed,
            "failed_batches": failed,
        }

    def _assess_free_dialogue(
        self,
        db: Session,
        interview: InterviewRound,
        job: Job,
        segments: list[TranscriptSegment],
    ) -> dict[str, Any] | None:
        application = db.get(Application, interview.application_id)
        candidate = db.get(Candidate, application.candidate_id) if application else None
        batches = _conversation_batches(
            segments,
            max_chars=max(2600, min(5200, self.settings.llm_max_context_chars - 5200)),
        )
        results: list[dict[str, Any]] = []
        failed = 0
        last_error: IntelligenceProviderError | None = None
        all_candidate_ids = {
            item.id
            for item in segments
            if item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
        }
        for index, batch in enumerate(batches):
            batch_candidate_ids = {
                item.id
                for item in batch
                if item.speaker_role == "candidate"
                and is_evidence_worthy_utterance(item.effective_text)
            }
            try:
                output = self._chat_json(
                    instructions=(
                        "你是自由对话面试的岗位证据评估助手。面试官可以完全不使用固定题；"
                        "你必须理解本批次真实问题为什么被提出，以及候选人的回答是否提供了与 JD 相关、可复核的事实。"
                        "score 是本批次岗位相关证据参考分：1分为出现明确岗位风险，3分为基本证据成立但仍有缺口，"
                        "5分为在复杂约束下提供了稳定、具体且可核验的成果。没有足够候选人证据时 sufficient_evidence=false。"
                        "只能引用 candidate 片段；面试官介绍公司、岗位或福利不能成为候选人的得分证据。"
                        "候选人的嗯、哦、确认词和反问也不能成为岗位证据。回答缺失只能降低置信度，不能被解释成负面能力。"
                        "不得使用性别、年龄、婚育、家庭、籍贯、学校或公司光环评分，不得推断人格、智力或潜力。"
                        "可以建议进入下一轮、补充面试、保留讨论或不建议进入下一轮，但不得自动改变候选人阶段。"
                    ),
                    payload={
                        "candidate_name_reference_only": candidate.display_name if candidate else "",
                        "resume_reference": candidate.resume_text[:1800] if candidate else "",
                        "job_title": job.title,
                        "jd_reference": job.jd_text[:2600],
                        "configured_round_type": interview.round_type,
                        "actual_interviewers": list(interview.interviewer_names or []),
                        "batch": {"index": index + 1, "total": len(batches)},
                        "transcript": [
                            {
                                "segment_id": item.id,
                                "speaker_role": item.speaker_role,
                                "text": item.effective_text,
                            }
                            for item in batch
                        ],
                    },
                    schema_name="free_dialogue_job_evidence_batch",
                    schema=_free_dialogue_batch_schema(),
                    timeout_seconds=max(self.settings.llm_timeout_seconds, 35),
                    max_tokens=1800,
                )
            except IntelligenceProviderError as error:
                failed += 1
                last_error = error
                continue
            referenced = list(
                dict.fromkeys(
                    str(value)
                    for value in output.get("evidence_segment_ids", [])
                    if str(value) in batch_candidate_ids
                )
            )[:8]
            try:
                score = float(output.get("score"))
                confidence = float(output.get("confidence", 0))
            except (TypeError, ValueError):
                failed += 1
                continue
            if not output.get("sufficient_evidence") or not referenced or not 1 <= score <= 5:
                continue
            results.append(
                {
                    "score": score,
                    "confidence": max(0.2, min(0.95, confidence)),
                    "rationale": str(output.get("rationale", "")).strip()[:500],
                    "positive_evidence": [str(item)[:240] for item in output.get("positive_evidence", []) if str(item).strip()][:4],
                    "risks": [str(item)[:240] for item in output.get("risks", []) if str(item).strip()][:4],
                    "evidence_segment_ids": referenced,
                    "next_round_questions": list(output.get("next_round_questions", []))[:3],
                }
            )
        if not results:
            if last_error is not None:
                raise last_error
            return None
        weights = [item["confidence"] * max(1, len(item["evidence_segment_ids"])) for item in results]
        overall_score = round(
            sum(item["score"] * weight for item, weight in zip(results, weights, strict=True))
            / max(0.01, sum(weights)),
            1,
        )
        confidence = round(
            min(
                0.94,
                sum(item["confidence"] for item in results) / len(results)
                * (len(results) / max(1, len(batches))),
            ),
            2,
        )
        evidence_segment_ids = list(
            dict.fromkeys(
                segment_id
                for item in results
                for segment_id in item["evidence_segment_ids"]
                if segment_id in all_candidate_ids
            )
        )[:12]
        if overall_score >= 3.7:
            decision, label = "advance", "建议进入下一轮，继续核实关键事实"
        elif overall_score >= 2.8:
            decision, label = "supplementary_interview", "建议补充验证后再决定"
        elif overall_score >= 2.2 or confidence < 0.7:
            decision, label = "hold", "建议保留讨论，并补充关键证据"
        else:
            decision, label = "reject", "不建议进入下一轮，需人工复核证据"
        positive_evidence = list(
            dict.fromkeys(text for item in results for text in item["positive_evidence"])
        )[:6]
        risks = list(dict.fromkeys(text for item in results for text in item["risks"]))[:6]
        rationales = list(
            dict.fromkeys(item["rationale"] for item in results if item["rationale"])
        )
        rationale = "；".join(rationales[:3])[:700] or "已依据自由对话中的岗位相关证据形成参考判断。"
        next_round_questions: list[dict[str, Any]] = []
        for item in (question for result in results for question in result["next_round_questions"]):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if len(question) < 6 or not reason:
                continue
            if question in {existing["question"] for existing in next_round_questions}:
                continue
            next_round_questions.append(
                {
                    "competency_id": "free_dialogue_job_evidence",
                    "source_type": "ad_hoc_gap",
                    "source_question_text": str(item.get("source_question_text", ""))[:240] or None,
                    "priority": item.get("priority") if item.get("priority") in {"high", "normal", "low"} else "normal",
                    "reason": reason[:240],
                    "question": question[:300],
                }
            )
            if len(next_round_questions) >= 5:
                break
        return {
            "decision": decision,
            "label": label,
            "overall_score": overall_score,
            "confidence": confidence,
            "rationale": rationale,
            "positive_evidence": positive_evidence,
            "risks": risks,
            "evidence_segment_ids": evidence_segment_ids,
            "next_round_questions": next_round_questions,
            "batch_status": {
                "status": "complete" if failed == 0 else "partial",
                "total_batches": len(batches),
                "completed_batches": len(batches) - failed,
                "failed_batches": failed,
                "transcript_segment_count": len(segments),
                "candidate_segment_count": sum(item.speaker_role == "candidate" for item in segments),
                "planned_question_dependency": False,
            },
        }

    def _apply_conversation_assessments(
        self,
        db: Session,
        interview: InterviewRound,
        competencies: list[dict[str, Any]],
        segments: list[TranscriptSegment],
        proposed: Iterable[Any],
        baseline: dict[str, Any],
    ) -> None:
        competency_map = {str(item["id"]): item for item in competencies}
        candidate_segments = {
            item.id: item
            for item in segments
            if item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in proposed:
            if not isinstance(item, dict):
                continue
            competency_id = str(item.get("competency_id", ""))
            if competency_id not in competency_map:
                continue
            referenced = list(
                dict.fromkeys(
                    str(value)
                    for value in item.get("evidence_segment_ids", [])
                    if str(value) in candidate_segments
                )
            )[:6]
            try:
                score = float(item.get("score"))
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                continue
            rationale = str(item.get("rationale", "")).strip()
            limitations = str(item.get("limitations", "")).strip()
            direction = item.get("direction")
            if (
                not referenced
                or not 1 <= score <= 5
                or not rationale
                or direction not in {"support", "negative"}
                or describes_absence_instead_of_behavior(rationale, limitations)
            ):
                continue
            grouped.setdefault(competency_id, []).append(
                {
                    "score": score,
                    "confidence": max(0.2, min(0.95, confidence)),
                    "direction": direction,
                    "rationale": rationale[:400],
                    "evidence_segment_ids": referenced,
                    "limitations": limitations[:240],
                }
            )

        existing = list(
            db.scalars(
                select(EvidenceItem).where(
                    EvidenceItem.interview_round_id == interview.id
                )
            ).all()
        )
        existing_keys = {
            (item.competency_id, segment_id)
            for item in existing
            for segment_id in item.segment_ids
        }
        evidence_by_competency: dict[str, list[EvidenceItem]] = {}
        for item in existing:
            if item.human_status != "rejected" and is_usable_evidence_record(
                quote=item.quote,
                direction=item.direction,
                explanation=item.explanation,
                human_status=item.human_status,
            ):
                evidence_by_competency.setdefault(item.competency_id, []).append(item)

        merged_scores: dict[str, dict[str, Any]] = {}
        for competency_id, items in grouped.items():
            weight = sum(item["confidence"] for item in items)
            score = round(
                sum(item["score"] * item["confidence"] for item in items)
                / max(weight, 0.01),
                1,
            )
            referenced = list(
                dict.fromkeys(
                    segment_id
                    for item in items
                    for segment_id in item["evidence_segment_ids"]
                )
            )[:8]
            for segment_id in referenced:
                if (competency_id, segment_id) in existing_keys:
                    continue
                source = candidate_segments[segment_id]
                quote = best_substantive_quote(source.effective_text, max_chars=220)
                if not quote:
                    continue
                direction = next(
                    item["direction"]
                    for item in items
                    if segment_id in item["evidence_segment_ids"]
                )
                evidence_item = EvidenceItem(
                    id=new_id("ev"),
                    interview_round_id=interview.id,
                    competency_id=competency_id,
                    segment_ids=[segment_id],
                    quote=quote,
                    direction=direction,
                    strength=round(min(0.9, 0.45 + 0.08 * len(items)), 2),
                    explanation=(
                        "结束后基于本轮全部问答形成的语义证据；不依赖是否使用预设题。"
                        f" {items[0]['rationale']}"
                    )[:500],
                    model_version=self.name,
                    human_status="pending",
                )
                db.add(evidence_item)
                evidence_by_competency.setdefault(competency_id, []).append(evidence_item)
                existing_keys.add((competency_id, segment_id))
            merged_scores[competency_id] = {
                "score": score,
                "confidence": round(
                    min(0.9, sum(item["confidence"] for item in items) / len(items)),
                    2,
                ),
                "rationale": "；".join(
                    dict.fromkeys(item["rationale"] for item in items)
                )[:500],
                "limitations": "；".join(
                    dict.fromkeys(
                        item["limitations"] for item in items if item["limitations"]
                    )
                )[:300],
            }
        db.flush()

        for item in baseline.get("ai_scores", []):
            competency_id = str(item.get("competency_id", ""))
            merged = merged_scores.get(competency_id)
            if not merged:
                continue
            related = evidence_by_competency.get(competency_id, [])
            confirmed_ids = [
                evidence.id
                for evidence in related
                if evidence.human_status in {"confirmed", "modified"}
            ]
            item.update(
                {
                    "score": merged["score"],
                    "assessment": "full_conversation_semantic",
                    "evidence_ids": [evidence.id for evidence in related],
                    "confirmed_evidence_ids": confirmed_ids,
                    "confidence": merged["confidence"],
                    "rationale": merged["rationale"],
                    "limitations": merged["limitations"],
                    "needs_human_confirmation": not bool(confirmed_ids),
                }
            )

        scored = [
            item for item in baseline.get("ai_scores", []) if item.get("score") is not None
        ]
        if not scored:
            return
        overall = round(
            sum(float(item["score"]) for item in scored) / len(scored), 1
        )
        minimum_dimensions = min(
            len(competencies), max(2, (len(competencies) + 2) // 3)
        )
        enough = len(scored) >= minimum_dimensions
        recommendation = baseline["recommendation"]
        ai = recommendation["ai_recommendation"]
        if not enough:
            decision, label = "supplementary_interview", "补充证据后再判断"
        elif overall >= 3.5:
            decision, label = "advance", "建议进入下一轮"
        elif overall < 2.5:
            decision, label = "reject", "暂不建议进入下一轮"
        else:
            decision, label = "hold", "保留讨论"
        ai.update(
            {
                "decision": decision,
                "label": label,
                "overall_score": overall,
                "scored_competencies": len(scored),
                "confidence": round(
                    min(
                        0.88,
                        sum(float(item.get("confidence", 0)) for item in scored)
                        / len(scored),
                    ),
                    2,
                ),
                "rationale": (
                    f"已按{baseline['recommendation']['evaluation_scope']['round_label']}职责分析本轮全部真实问答，"
                    f"共在 {len(scored)} 项评价维度发现可引用证据；该建议仍需人工核对。"
                ),
                "assessment_basis": "full_round_conversation",
                "planned_question_dependency": False,
            }
        )
        recommendation["decision"] = (
            "human_review_required" if enough else "insufficient_evidence"
        )

    def _persist_validated_evidence(
        self,
        db: Session,
        interview: InterviewRound,
        competencies: list[dict[str, Any]],
        segments: list[TranscriptSegment],
        proposed: Iterable[Any],
    ) -> None:
        competency_ids = {item["id"] for item in competencies}
        candidate_segments = {
            item.id: item
            for item in segments
            if item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
        }
        existing = db.scalars(
            select(EvidenceItem).where(EvidenceItem.interview_round_id == interview.id)
        ).all()
        existing_keys = {(item.competency_id, segment_id) for item in existing for segment_id in item.segment_ids}
        for item in list(proposed)[:6]:
            if not isinstance(item, dict):
                continue
            segment_id = str(item.get("segment_id", ""))
            competency_id = str(item.get("competency_id", ""))
            quote = str(item.get("quote", "")).strip()
            source = candidate_segments.get(segment_id)
            evidence_quote = best_substantive_quote(quote, max_chars=180)
            if (
                not source
                or competency_id not in competency_ids
                or not evidence_quote
                or quote not in source.effective_text
                or (competency_id, segment_id) in existing_keys
                or item.get("direction") not in {"support", "negative"}
                or describes_absence_instead_of_behavior(str(item.get("explanation", "")))
            ):
                continue
            direction = str(item.get("direction"))
            try:
                strength = max(0.0, min(1.0, float(item.get("strength", 0.5))))
            except (TypeError, ValueError):
                strength = 0.5
            db.add(
                EvidenceItem(
                    id=new_id("ev"),
                    interview_round_id=interview.id,
                    competency_id=competency_id,
                    segment_ids=[segment_id],
                    quote=evidence_quote,
                    direction=direction,
                    strength=round(strength, 2),
                    explanation=str(item.get("explanation", "模型提议，需面试官核对语境。"))[:300],
                    model_version=self.name,
                    human_status="pending",
                )
            )
            existing_keys.add((competency_id, segment_id))
        db.flush()

    @staticmethod
    def _validated_response_quality(
        proposed: Any, segments: list[TranscriptSegment]
    ) -> dict[str, Any] | None:
        if not isinstance(proposed, dict):
            return None
        candidate_segments = {
            item.id: item
            for item in segments
            if item.speaker_role == "candidate"
            and is_evidence_worthy_utterance(item.effective_text)
        }
        referenced = [
            str(value)
            for value in proposed.get("evidence_segment_ids", [])
            if str(value) in candidate_segments
        ]
        try:
            score = int(proposed.get("score"))
        except (TypeError, ValueError):
            return None
        rationale = str(proposed.get("rationale", "")).strip()
        if not 1 <= score <= 5 or not referenced or not rationale:
            return None
        observed = [
            str(value)[:40]
            for value in proposed.get("observed_dimensions", [])
            if str(value).strip()
        ][:6]
        return {
            "score": score,
            "label": str(proposed.get("label") or "AI 语义回答质量评估")[:80],
            "confidence": round(min(0.9, 0.45 + 0.1 * len(referenced)), 2),
            "evidence_segment_ids": referenced,
            "evidence_quotes": [
                quote
                for item_id in referenced
                if (quote := best_substantive_quote(candidate_segments[item_id].effective_text, max_chars=160))
            ],
            "dimensions": {item: True for item in observed},
            "rationale": rationale[:400],
            "boundary": "只评估本轮回答呈现出的结构与证据，不推断智力、人格或潜力。",
            "model_assessed": True,
            "limitations": str(proposed.get("limitations", "仍需结合问题难度和说话人识别准确性人工复核。"))[:240],
        }

    @staticmethod
    def _validated_suggestions(
        proposed: Iterable[Any],
        fallback: list[dict[str, Any]],
        competencies: list[dict[str, Any]],
        questions: list[dict[str, Any]],
        segments: list[TranscriptSegment],
        answer_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        question_competencies = {
            str(item.get("id") or item.get("question_id")): str(item.get("competency_id", ""))
            for item in questions
            if (item.get("id") or item.get("question_id")) and item.get("competency_id")
        }
        question_texts = {
            str(item.get("id") or item.get("question_id")): str(item.get("question", ""))
            for item in questions
            if item.get("id") or item.get("question_id")
        }
        allowed_competencies = {str(item.get("id")) for item in competencies} | set(question_competencies.values())
        candidate_segments = {
            item.id: item
            for item in segments
            if item.speaker_role == "candidate"
            and substantive_character_count(trim_leading_fillers(item.effective_text)) >= 2
        }
        context_segment_ids = [
            str(value)
            for value in (answer_context or {}).get("source_segment_ids", [])
            if str(value) in candidate_segments
        ]
        context_text = str((answer_context or {}).get("text", ""))
        output: list[dict[str, Any]] = []
        generic_templates = (
            "最后具体",
            "什么结果",
            "你本人具体",
            "用什么数字",
            "最后是做成、没做成",
            "发生了什么可核实的变化",
        )
        for item in list(proposed)[:1]:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id", ""))
            competency_id = str(item.get("competency_id", ""))
            evidence_gap = str(item.get("evidence_gap", ""))
            basis_segment_id = str(item.get("basis_segment_id", ""))
            basis_quote = str(item.get("basis_quote", "")).strip()
            evidence_quote = best_substantive_quote(basis_quote, max_chars=180)
            question = str(item.get("question", "")).strip()
            reason = str(item.get("reason", "")).strip()
            source_segment = candidate_segments.get(basis_segment_id)
            quote_is_traceable = bool(
                source_segment
                and (
                    basis_quote in source_segment.effective_text
                    or (
                        basis_segment_id in context_segment_ids
                        and basis_quote in context_text
                    )
                )
            )
            if (
                question_id not in question_competencies
                or competency_id not in allowed_competencies
                or question_competencies[question_id] != competency_id
                or evidence_gap not in LIVE_EVIDENCE_GAPS
                or not source_segment
                or not evidence_quote
                or not quote_is_traceable
                or not question
                or not reason
                or not _gap_matches_context(
                    evidence_gap,
                    question_texts.get(question_id, ""),
                    evidence_quote,
                )
                or any(template in question for template in generic_templates)
                or any(term in f"{reason}{question}" for term in FORBIDDEN_DECISION_TERMS)
            ):
                continue
            normalized_question = _normalize_suggestion(question)
            if normalized_question in {_normalize_suggestion(existing["question"]) for existing in output}:
                continue
            output.append(
                {
                    "question": (
                        question[:300]
                        if evidence_quote in question
                        else f"你刚才提到“{evidence_quote[:72]}”。{question}"[:300]
                    ),
                    "reason": reason[:200],
                    "priority": item.get("priority") if item.get("priority") in {"high", "normal", "low"} else "normal",
                    "source": "llm_semantic_evidence_gap",
                    "question_id": question_id,
                    "competency_id": competency_id,
                    "evidence_gap": evidence_gap,
                    "evidence_segment_ids": (
                        context_segment_ids
                        if basis_quote in context_text and context_segment_ids
                        else [basis_segment_id]
                    ),
                    "basis_quote": evidence_quote,
                    "source_question_text": question_texts.get(question_id, ""),
                }
            )
        return output

    @staticmethod
    def _validated_next_questions(
        proposed: Iterable[Any],
        competencies: list[dict[str, Any]],
        evidence_ids: set[str],
        fallback: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed_competencies = {item["id"] for item in competencies} | {"jd_verification", "prior_round_followup"}
        output: list[dict[str, Any]] = []
        for item in list(proposed)[:5]:
            if not isinstance(item, dict):
                continue
            competency_id = str(item.get("competency_id", ""))
            question = str(item.get("question", "")).strip()
            reason = str(item.get("reason", "")).strip()
            referenced = [str(value) for value in item.get("evidence_ids", []) if str(value) in evidence_ids]
            if competency_id not in allowed_competencies or not question or not reason:
                continue
            output.append(
                {
                    "competency_id": competency_id,
                    "source_type": "llm_follow_up",
                    "priority": item.get("priority") if item.get("priority") in {"high", "normal", "low"} else "normal",
                    "reason": reason[:300],
                    "question": question[:300],
                    "evidence_ids": referenced,
                }
            )
        return output or fallback

    def _with_status(self, payload: dict[str, Any], status: str) -> dict[str, Any]:
        payload["provider"] = self.name
        payload["mode"] = "production"
        payload["model_assistance"] = self._status(status)
        return payload

    def _degraded(
        self,
        payload: dict[str, Any],
        error: IntelligenceProviderError,
        *,
        status: str = "degraded",
    ) -> dict[str, Any]:
        payload["provider"] = self.name
        payload["mode"] = "degraded" if status == "degraded" else "fallback"
        payload["model_assistance"] = self._status(status, error.code)
        return payload

    def _record_live_failure(self, interview_id: str) -> int:
        with self._live_state_lock:
            count = self._live_failure_counts.get(interview_id, 0) + 1
            self._live_failure_counts[interview_id] = count
            return count

    def _reset_live_failures(self, interview_id: str) -> None:
        with self._live_state_lock:
            self._live_failure_counts.pop(interview_id, None)

    def _status(self, status: str, error_code: str | None = None) -> dict[str, Any]:
        return {
            "status": status,
            "provider": self.name,
            "fallback_provider": self.fallback.name,
            "error_code": error_code,
            "automatic_decision": False,
        }


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _live_competency_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Expose observable standards to the model without authorizing a live score."""

    return {
        "id": item["id"],
        "name": item["name"],
        "definition": item.get("description", ""),
        "positive_evidence": list(item.get("positive_evidence", [])),
        "risk_signals": list(item.get("risk_signals", [])),
        "score_anchors_reference_only": dict(item.get("score_anchors", {})),
        "evidence_requirements": list(item.get("evidence_requirements", [])),
        "suggested_follow_up": item.get("follow_up", ""),
        "keywords_navigation_only": list(item.get("keywords", [])),
        "source": item.get("source", "round_catalog"),
    }


def _current_candidate_answer_context(
    segments: list[TranscriptSegment], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Join streaming fragments for the current logical answer.

    Source segment IDs are retained so every live prompt can still be traced
    back to the raw transcript even when the display text spans ASR fragments.
    """
    active_id = str(baseline.get("active_question_id") or "")
    active_state = next(
        (
            item
            for item in baseline.get("question_coverage", [])
            if str(item.get("question_id") or "") == active_id
        ),
        None,
    )
    linked_ids = {
        str(value) for value in (active_state or {}).get("evidence_segment_ids", [])
    }
    if linked_ids:
        selected = [
            item
            for item in segments
            if item.id in linked_ids and item.speaker_role == "candidate"
        ]
    else:
        selected: list[TranscriptSegment] = []
        for item in reversed(segments):
            if item.speaker_role == "interviewer" and selected:
                break
            if item.speaker_role == "candidate":
                selected.append(item)
        selected.reverse()
    selected = selected[-18:]
    values = [
        value
        for item in selected
        if (value := trim_leading_fillers(item.effective_text))
        and substantive_character_count(value) >= 2
    ]
    text = " ".join(values)
    return {
        "question_id": active_id or None,
        "text": text,
        "source_segment_ids": [item.id for item in selected],
        "character_count": substantive_character_count(text),
        "policy": "这是同一回答的分析视图；引用仍须回到 source_segment_ids 中的原始逐字稿。",
    }


def _live_question_context(
    questions: list[dict[str, Any]], baseline: dict[str, Any]
) -> list[dict[str, Any]]:
    state_by_id = {
        str(item.get("question_id")): item
        for item in baseline.get("question_coverage", [])
        if isinstance(item, dict) and item.get("question_id")
    }
    active_id = str(baseline.get("active_question_id") or "")

    def rank(item: dict[str, Any]) -> tuple[int, int]:
        question_id = str(item.get("id", ""))
        state = state_by_id.get(question_id, {})
        if question_id == active_id:
            return (0, 0)
        if state.get("status") == "shallow":
            return (1, 0)
        if item.get("required"):
            return (2, 0)
        if item.get("source") == "resume_jd_match":
            return (3, 0)
        return (4, 0)

    output = []
    for item in sorted(questions, key=rank)[:12]:
        question_id = str(item.get("id", ""))
        state = state_by_id.get(question_id, {})
        output.append(
            {
                "question_id": question_id,
                "competency_id": str(item.get("competency_id", "")),
                "question": item.get("question", ""),
                "planned_follow_up": item.get("follow_up", ""),
                "source": item.get("source", ""),
                "required": bool(item.get("required")),
                "is_current": question_id == active_id,
                "answer_status": state.get("status", "unanswered"),
                "linked_candidate_segment_ids": list(state.get("evidence_segment_ids", [])),
                "rule_detected_missing_dimensions": list(state.get("missing_dimensions", [])),
            }
        )
    included_ids = {item["question_id"] for item in output}
    for question_id, state in state_by_id.items():
        if question_id in included_ids or not question_id.startswith("adhoc:"):
            continue
        output.insert(
            0,
            {
                "question_id": question_id,
                "competency_id": str(state.get("competency_id") or "interviewer_ad_hoc"),
                "question": state.get("question", ""),
                "planned_follow_up": "请补充本人行动、结果或一个能被核实的细节。",
                "source": "interviewer_ad_hoc",
                "required": False,
                "is_current": question_id == active_id,
                "answer_status": state.get("status", "unanswered"),
                "linked_candidate_segment_ids": list(state.get("evidence_segment_ids", [])),
                "rule_detected_missing_dimensions": list(state.get("missing_dimensions", [])),
            },
        )
    return output[:12]


def _normalize_suggestion(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _gap_matches_context(evidence_gap: str, source_question: str, basis_quote: str) -> bool:
    """Result and metric gaps are conditional checks, never universal requirements."""
    question_intent = re.sub(r"“[^”]{0,600}”", "", source_question)
    context = f"{question_intent}{basis_quote}"
    if evidence_gap == "result":
        return any(
            marker in context
            for marker in ("结果", "成果", "完成", "达成", "上线", "交付", "产出", "效果", "提升", "降低", "增长", "解决")
        )
    if evidence_gap == "metric":
        return any(
            marker in context
            for marker in ("指标", "数据", "多少", "量化", "提升", "降低", "增长", "转化率", "效率", "成本", "周期", "KPI")
        )
    return True


def _bounded_json(payload: dict[str, Any], max_chars: int) -> str:
    """Trim older transcript entries while always sending valid JSON."""

    bounded = dict(payload)
    transcript = [dict(item) for item in payload.get("transcript", []) if isinstance(item, dict)]
    bounded["transcript"] = transcript
    serialized = json.dumps(bounded, ensure_ascii=False)
    while len(serialized) > max_chars and len(transcript) > 1:
        transcript.pop(0)
        serialized = json.dumps(bounded, ensure_ascii=False)
    if len(serialized) > max_chars and transcript:
        fixed_size = len(serialized) - len(str(transcript[-1].get("text", "")))
        available = max(200, max_chars - fixed_size - 40)
        transcript[-1]["text"] = str(transcript[-1].get("text", ""))[-available:]
        serialized = json.dumps(bounded, ensure_ascii=False)
    return serialized


def _clean_resume_reference(value: str) -> str:
    return "".join(
        character
        for character in value
        if character in "\n\t" or (character.isprintable() and character != "\x00")
    )


def _resume_question_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "questions": {
                "type": "array",
                "maxItems": 2,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "dimension_id": {"type": "string"},
                        "resume_quote": {"type": "string"},
                        "question": {"type": "string"},
                        "follow_up": {"type": "string"},
                        "why_this_matters": {"type": "string"},
                    },
                    "required": [
                        "dimension_id",
                        "resume_quote",
                        "question",
                        "follow_up",
                        "why_this_matters",
                    ],
                },
            }
        },
        "required": ["questions"],
    }


def _live_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "suggestions": {
                "type": "array",
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "question_id": {"type": "string"},
                        "competency_id": {"type": "string"},
                        "evidence_gap": {
                            "type": "string",
                            "enum": sorted(LIVE_EVIDENCE_GAPS),
                        },
                        "basis_segment_id": {"type": "string"},
                        "basis_quote": {"type": "string"},
                        "reason": {"type": "string"},
                        "question": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                    },
                    "required": [
                        "question_id",
                        "competency_id",
                        "evidence_gap",
                        "basis_segment_id",
                        "basis_quote",
                        "reason",
                        "question",
                        "priority",
                    ],
                },
            },
            "evidence": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "segment_id": {"type": "string"},
                        "competency_id": {"type": "string"},
                        "quote": {"type": "string"},
                        "direction": {"type": "string", "enum": ["support", "negative", "neutral"]},
                        "strength": {"type": "number", "minimum": 0, "maximum": 1},
                        "explanation": {"type": "string"},
                    },
                    "required": ["segment_id", "competency_id", "quote", "direction", "strength", "explanation"],
                },
            },
            "transcript_corrections": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "segment_id": {"type": "string"},
                        "corrected_text": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["segment_id", "corrected_text", "confidence", "reason"],
                },
            },
        },
        "required": ["suggestions", "evidence", "transcript_corrections"],
    }


def _conversation_batches(
    segments: list[TranscriptSegment],
    *,
    max_chars: int,
) -> list[list[TranscriptSegment]]:
    """Keep question-answer turns together while covering every final segment."""
    batches: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    current_chars = 0
    for segment in segments:
        segment_chars = len(segment.effective_text) + 80
        starts_new_turn = segment.speaker_role == "interviewer"
        if current and current_chars + segment_chars > max_chars and starts_new_turn:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(segment)
        current_chars += segment_chars
        if current_chars >= max_chars and segment.speaker_role == "candidate":
            batches.append(current)
            current = []
            current_chars = 0
    if current:
        batches.append(current)
    return batches


def _conversation_assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "competency_assessments": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "competency_id": {"type": "string"},
                        "score": {"type": "number", "minimum": 1, "maximum": 5},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "direction": {
                            "type": "string",
                            "enum": ["support", "negative", "neutral"],
                        },
                        "rationale": {"type": "string"},
                        "evidence_segment_ids": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 6,
                            "items": {"type": "string"},
                        },
                        "limitations": {"type": "string"},
                    },
                    "required": [
                        "competency_id",
                        "score",
                        "confidence",
                        "direction",
                        "rationale",
                        "evidence_segment_ids",
                        "limitations",
                    ],
                },
            }
        },
        "required": ["competency_assessments"],
    }


def _scorecard_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "response_quality": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "integer", "minimum": 1, "maximum": 5},
                    "label": {"type": "string"},
                    "rationale": {"type": "string"},
                    "observed_dimensions": {
                        "type": "array",
                        "maxItems": 6,
                        "items": {"type": "string"},
                    },
                    "evidence_segment_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {"type": "string"},
                    },
                    "limitations": {"type": "string"},
                },
                "required": ["score", "label", "rationale", "observed_dimensions", "evidence_segment_ids", "limitations"],
            },
            "next_round_questions": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "competency_id": {"type": "string"},
                        "reason": {"type": "string"},
                        "question": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["competency_id", "reason", "question", "priority", "evidence_ids"],
                },
            },
        },
        "required": ["summary", "response_quality", "next_round_questions"],
    }


def _answer_logic_schema() -> dict[str, Any]:
    dimension = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {
                "type": "string",
                "enum": [
                    "causal_coherence",
                    "timeline_consistency",
                    "ownership_consistency",
                    "claim_calibration",
                    "cross_answer_consistency",
                ],
            },
            "status": {
                "type": "string",
                "enum": ["coherent", "needs_verification", "unknown"],
            },
            "explanation": {"type": "string"},
            "segment_ids": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
        },
        "required": ["id", "status", "explanation", "segment_ids"],
    }
    flag = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "flag_type": {
                "type": "string",
                "enum": [
                    "factual_conflict",
                    "timeline_conflict",
                    "ownership_shift",
                    "causal_gap",
                    "claim_needs_verification",
                ],
            },
            "severity": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "description": {"type": "string"},
            "segment_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "verification_question": {"type": "string"},
        },
        "required": [
            "flag_type",
            "severity",
            "description",
            "segment_ids",
            "verification_question",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sufficient_evidence": {"type": "boolean"},
            "logic_score": {"type": "integer", "minimum": 1, "maximum": 5},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "label": {"type": "string"},
            "summary": {"type": "string"},
            "dimensions": {
                "type": "array",
                "maxItems": 5,
                "items": dimension,
            },
            "consistency_flags": {
                "type": "array",
                "maxItems": 5,
                "items": flag,
            },
            "verification_questions": {
                "type": "array",
                "maxItems": 5,
                "items": {"type": "string"},
            },
        },
        "required": [
            "sufficient_evidence",
            "logic_score",
            "confidence",
            "label",
            "summary",
            "dimensions",
            "consistency_flags",
            "verification_questions",
        ],
    }


def _free_dialogue_batch_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "sufficient_evidence": {"type": "boolean"},
            "score": {"type": "number", "minimum": 1, "maximum": 5},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
            "positive_evidence": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "risks": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "evidence_segment_ids": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string"},
            },
            "next_round_questions": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source_question_text": {"type": "string"},
                        "reason": {"type": "string"},
                        "question": {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                    },
                    "required": ["source_question_text", "reason", "question", "priority"],
                },
            },
        },
        "required": [
            "sufficient_evidence",
            "score",
            "confidence",
            "rationale",
            "positive_evidence",
            "risks",
            "evidence_segment_ids",
            "next_round_questions",
        ],
    }

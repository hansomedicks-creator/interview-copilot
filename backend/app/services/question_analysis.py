from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..models import InterviewQuestionProgress, TranscriptSegment
from .utterance_quality import (
    best_substantive_quote,
    is_evidence_worthy_utterance,
    substantive_character_count,
    trim_leading_fillers,
)


ACTION_MARKERS = ("我", "主导", "推动", "分析", "制定", "执行", "协调", "判断", "负责", "沟通", "设计")
RESULT_MARKERS = ("最终", "结果", "完成", "达成", "提升", "降低", "增长", "上线", "交付", "改善")
CONSTRAINT_MARKERS = ("约束", "阻力", "困难", "难", "适应", "资源", "冲突", "压力", "风险", "成本", "时间", "限制")
REFLECTION_MARKERS = ("复盘", "原因", "改进", "后来", "调整", "修正", "失败", "不足", "判断错", "重新")
CAUSAL_MARKERS = ("因为", "所以", "原因", "导致", "为了", "考虑到", "判断", "取舍", "优先", "相比")
STRUCTURE_MARKERS = ("首先", "然后", "接着", "最后", "第一", "第二", "一方面", "另一方面")
OWNERSHIP_MARKERS = ("我负责", "我做", "我写", "我改", "我排查", "我设计", "我决定", "我亲自", "由我")
MECHANISM_MARKERS = ("步骤", "流程", "排查", "检查", "日志", "接口", "配置", "调用", "实现", "原理")
DECISION_MARKERS = ("依据", "判断", "选择", "决定", "权衡", "取舍", "优先", "考虑", "因为", "所以")
DELIVERY_QUESTION_MARKERS = ("结果", "成果", "完成", "达成", "上线", "交付", "产出", "效果", "是否有效")
METRIC_QUESTION_MARKERS = ("指标", "数据", "多少", "量化", "提升", "降低", "增长", "转化率", "效率", "成本", "周期", "KPI")
OWNERSHIP_QUESTION_MARKERS = ("亲自", "本人", "你负责", "自己做", "团队", "下属", "带人", "责任边界")
MECHANISM_QUESTION_MARKERS = ("怎么做", "如何", "步骤", "流程", "机制", "原理", "排查", "实现", "设计")
DECISION_QUESTION_MARKERS = ("为什么", "依据", "判断", "选择", "决定", "权衡", "取舍", "优先")
GENERIC_TERMS = {
    "岗位", "经历", "具体", "一次", "什么", "如何", "说明", "请讲", "这个", "你的", "你会", "当时",
}

TURN_TYPES = {"interview_question", "small_talk", "transition", "confirmation", "instruction", "candidate_question_response", "administrative"}


def interviewer_turns(segments: list[TranscriptSegment], overrides: dict | None = None) -> list[dict]:
    """Classify logical turns, preserving fragments and an auditable rationale."""
    turns: list[dict] = []
    previous_role = None
    for segment in segments:
        if segment.speaker_role == "interviewer":
            if previous_role != "interviewer":
                turns.append({"segment_ids": [], "text": "", "previous_candidate_text": previous_text if previous_role == "candidate" else ""})
            turns[-1]["segment_ids"].append(segment.id)
            turns[-1]["text"] += (" " if turns[-1]["text"] else "") + segment.effective_text
        previous_role = segment.speaker_role
        previous_text = segment.effective_text
    for turn in turns:
        text = turn["text"]
        supplied = (overrides or {}).get(turn["segment_ids"][0], {})
        if supplied.get("text") == text and supplied.get("turn_type") in TURN_TYPES and supplied.get("confidence", 0) >= .7:
            turn.update({key: supplied[key] for key in ("turn_type", "confidence", "reason")})
            turn["source"] = "semantic"
            continue
        # Conservative offline fallback: a question mark alone is not evidence
        # that this turn requests job-related behavior or reasoning.
        kind = "transition"
        asks = re.search(r"为什么|怎么|如何|什么|哪|能否|是否|请讲|讲讲|介绍.*经历|说说|负责", text)
        work = re.search(r"岗位|项目|工作|方案|工具|技术|团队|客户|负责|判断|选择|离开|离职|学校|课程|学习|实习|经验|经历|困难|问题|故障|设计|系统|业务|决定|使用|实现|部署|例子|处理|动机|职业", text)
        if asks and work:
            kind = "interview_question"
        elif re.search(r"到岗|入职时间|薪资|薪酬|面试时间|预约|排期", text):
            kind = "administrative"
        elif re.search(r"自我介绍|慢慢说|简单一点|请坐|喝.*水", text):
            kind = "instruction"
        elif re.search(r"天气|挺热|吃饭|过来.*方便|路上|堵车", text):
            kind = "small_talk"
        elif re.search(r"对吧|是吗|没错吧|毕业|现在.*在", text):
            kind = "confirmation"
        elif re.search(r"贵公司|你们.*[吗？?]|想问|请问", turn["previous_candidate_text"]):
            kind = "candidate_question_response"
        turn.update(turn_type=kind, confidence=.6, source="local_conservative", reason="结合提问动作与岗位经历语境判断；不明确的发言暂不触发追问")
    return turns


def analyze_question_answers(
    questions: list[dict[str, Any]],
    segments: list[TranscriptSegment],
    progress: list[InterviewQuestionProgress],
    turn_intents: dict | None = None,
) -> dict[str, Any]:
    """Link candidate answers to planned questions without turning inference into a human fact."""
    question_by_id = {item.get("id"): item for item in questions if item.get("id")}
    terms_by_id = {question_id: _question_terms(question) for question_id, question in question_by_id.items()}
    linked: dict[str, list[TranscriptSegment]] = defaultdict(list)
    active_question_id: str | None = None
    active_source = "none"
    last_speaker_role: str | None = None
    turns = interviewer_turns(segments, turn_intents)
    intent_by_segment = {sid: turn for turn in turns for sid in turn["segment_ids"]}
    ignore_response = False

    manual_progress = sorted(
        [item for item in progress if item.asked],
        key=lambda item: item.asked_at,
    )
    for item in manual_progress:
        for segment_id in item.evidence_segment_ids or []:
            segment = next((candidate for candidate in segments if candidate.id == segment_id), None)
            if segment and segment.speaker_role == "candidate":
                linked[item.question_id].append(segment)

    for segment in segments:
        if segment.speaker_role == "interviewer":
            intent = intent_by_segment[segment.id]
            if intent["turn_type"] != "interview_question":
                active_question_id = None
                active_source = "none"
                ignore_response = True
                last_speaker_role = "interviewer"
                continue
            ignore_response = False
            matched = _best_question_match(segment.effective_text, question_by_id, terms_by_id, interviewer=True)
            if matched:
                active_question_id = matched
                active_source = "interviewer_match"
            elif (
                last_speaker_role == "interviewer"
                and active_question_id
                and active_question_id.startswith("adhoc:")
            ):
                # Real-time ASR often emits one interviewer question as several
                # final fragments. Until the candidate starts answering, they
                # are one logical question—not dozens of unanswered questions.
                current = question_by_id[active_question_id]
                current["question"] = " ".join(
                    value
                    for value in (
                        str(current.get("question", "")).strip(),
                        trim_leading_fillers(segment.effective_text),
                    )
                    if value
                )
            else:
                # An interviewer may ask a completely valid question outside
                # the prepared list. Treat it as a new conversation turn so
                # the candidate's answer is never assigned to an old question.
                ad_hoc_id = f"adhoc:{segment.id}"
                question_by_id[ad_hoc_id] = {
                    "id": ad_hoc_id,
                    "competency_id": "interviewer_ad_hoc",
                    "competency_name": "面试官临场问题",
                    "question": segment.effective_text,
                    "follow_up": "只在回答暴露出影响判断的关键缺口时继续追问。",
                    "source": "interviewer_ad_hoc",
                    "required": False,
                    "keywords": [],
                }
                terms_by_id[ad_hoc_id] = []
                active_question_id = ad_hoc_id
                active_source = "interviewer_ad_hoc"
            last_speaker_role = "interviewer"
            continue
        if segment.speaker_role != "candidate":
            continue
        if ignore_response:
            last_speaker_role = "candidate"
            continue

        question_id = active_question_id
        link_source = active_source
        if not question_id:
            manual = _latest_manual_question(manual_progress, segment)
            if manual:
                question_id = manual.question_id
                link_source = "manual_asked"
        if not question_id:
            question_id = _best_question_match(segment.effective_text, question_by_id, terms_by_id, interviewer=False)
            link_source = "answer_keyword" if question_id else "none"
        if question_id and question_id in question_by_id:
            if all(existing.id != segment.id for existing in linked[question_id]):
                linked[question_id].append(segment)
            active_question_id = question_id
            active_source = link_source
        last_speaker_role = "candidate"

    states = []
    for question_id, question in question_by_id.items():
        # Streaming ASR commonly splits one answer into several short final
        # fragments. Judge the logical answer as a whole; otherwise a useful
        # answer such as “用了 / 内网穿透 / 和免费域名” is incorrectly treated
        # as three empty utterances and no live follow-up can be generated.
        raw_answer_segments = linked.get(question_id, [])
        answer_segments = [
            item
            for item in raw_answer_segments
            if substantive_character_count(trim_leading_fillers(item.effective_text)) >= 2
        ]
        text = " ".join(
            value
            for item in answer_segments
            if (value := trim_leading_fillers(item.effective_text))
        )
        signals = _answer_signals(text)
        if not answer_segments or not is_evidence_worthy_utterance(text, min_chars=4):
            status = "unanswered"
        elif _has_traceable_depth(question, text, signals):
            status = "evidenced"
        else:
            status = "shallow"
        states.append(
            {
                "question_id": question_id,
                "competency_id": question.get("competency_id", "interviewer_ad_hoc"),
                "competency_name": question.get("competency_name", "本轮问题"),
                "question": question.get("question", ""),
                "source": question.get("source", "round_standard"),
                "required": bool(question.get("required")),
                "status": status,
                # Preserve progressive candidate updates while explicitly
                # recording that ASR may split one logical answer into fragments.
                "answer_turn_count": len(answer_segments),
                "answer_segment_count": len(answer_segments),
                "answer_character_count": len(re.sub(r"\s+", "", text)),
                "evidence_segment_ids": [item.id for item in answer_segments],
                "answer_excerpt": text[:120],
                "answer_text": text,
                "basis_segment_id": answer_segments[-1].id if answer_segments else None,
                "basis_quote": _answer_anchor(answer_segments),
                "missing_dimensions": _missing_dimensions(question, signals) if answer_segments else [],
            }
        )

    suggestions = _question_gap_suggestions(states, active_question_id)
    return {
        "states": states,
        "suggestions": suggestions,
        "active_question_id": active_question_id,
        "interviewer_turns": turns,
        "summary": {
            "total": len(states),
            "unanswered": sum(item["status"] == "unanswered" for item in states),
            "shallow": sum(item["status"] == "shallow" for item in states),
            "evidenced": sum(item["status"] == "evidenced" for item in states),
        },
    }


def _latest_manual_question(
    progress: list[InterviewQuestionProgress], segment: TranscriptSegment
) -> InterviewQuestionProgress | None:
    if not progress or segment.created_at is None:
        return None
    segment_time = segment.created_at.timestamp()
    eligible = [item for item in progress if item.asked_at.timestamp() <= segment_time]
    return eligible[-1] if eligible else None


def _question_terms(question: dict[str, Any]) -> list[str]:
    terms = [str(item).strip() for item in question.get("keywords", []) if str(item).strip()]
    competency_name = str(question.get("competency_name", ""))
    if "·" in competency_name:
        terms.append(competency_name.rsplit("·", 1)[1].strip())
    evidence = str(question.get("source_evidence", ""))
    if "：" in evidence:
        terms.append(evidence.rsplit("：", 1)[1].strip())
    return list(dict.fromkeys(term for term in terms if term not in GENERIC_TERMS and len(term) >= 2))


def _best_question_match(
    text: str,
    questions: dict[str, dict[str, Any]],
    terms_by_id: dict[str, list[str]],
    *,
    interviewer: bool,
) -> str | None:
    normalized = _normalize(text)
    if not normalized:
        return None
    text_bigrams = _bigrams(normalized)
    best_id = None
    best_score = 0.0
    for question_id, question in questions.items():
        if interviewer and question.get("source") == "interviewer_ad_hoc":
            # Two naturally phrased interviewer questions often share most of
            # their wording. Never collapse a new real question into an older
            # ad-hoc turn merely because both contain "讲讲具体场景".
            continue
        terms = terms_by_id.get(question_id, [])
        term_hits = sum(term in text for term in terms)
        question_bigrams = _bigrams(_normalize(str(question.get("question", ""))))
        overlap = len(text_bigrams & question_bigrams) / max(1, len(text_bigrams | question_bigrams))
        # When the interviewer is speaking, the planned question's full wording
        # is the strongest signal. Keyword-heavy standard competencies must not
        # steal an exact JD question match. Candidate answers use terms only as
        # a last-resort link when no active/manual question exists.
        score = (
            overlap + min(term_hits, 3) * 0.05
            if interviewer
            else term_hits * 1.5 + overlap
        )
        # A loose match wrongly assigns ordinary interviewer questions to a
        # prepared template. Prefer a separate ad-hoc turn unless the wording
        # is clearly the same question.
        threshold = 0.32 if interviewer else 1.45
        if score >= threshold and score > best_score:
            best_id = question_id
            best_score = score
    return best_id


def _normalize(text: str) -> str:
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9%]", "", text)
    for phrase in ("请讲一次", "请讲一个", "请说明", "可以", "结合", "具体", "这个", "你的", "你会", "什么", "如何"):
        compact = compact.replace(phrase, "")
    return compact


def _bigrams(text: str) -> set[str]:
    return {text[index : index + 2] for index in range(max(0, len(text) - 1))}


def _answer_signals(text: str) -> dict[str, bool]:
    return {
        "action": any(marker in text for marker in ACTION_MARKERS),
        "ownership": any(marker in text for marker in OWNERSHIP_MARKERS),
        "mechanism": any(marker in text for marker in MECHANISM_MARKERS),
        "decision_basis": any(marker in text for marker in DECISION_MARKERS),
        "result": any(marker in text for marker in RESULT_MARKERS),
        "metric": bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|％|万|天|周|月|年|个|倍|百分点)", text)),
        "constraint": any(marker in text for marker in CONSTRAINT_MARKERS),
        "reflection": any(marker in text for marker in REFLECTION_MARKERS),
        "causal": any(marker in text for marker in CAUSAL_MARKERS),
        "structure": any(marker in text for marker in STRUCTURE_MARKERS),
    }


def assess_response_quality(segments: list[TranscriptSegment]) -> dict[str, Any]:
    """Keep paired observations available; offline rules must not invent a score."""
    review = analyze_question_answers([], segments, [])
    pairs = [item for item in review["states"] if item["evidence_segment_ids"]]
    ids = list(dict.fromkeys(sid for pair in pairs for sid in pair["evidence_segment_ids"]))
    return {
        "score": None,
        "label": "等待问答语义评价" if pairs else "没有可评估的真实问答",
        "confidence": 0.0,
        "evidence_segment_ids": ids,
        "evidence_quotes": [pair["answer_excerpt"] for pair in pairs],
        "question_answer_pairs": [{"question": pair["question"], "answer": pair["answer_text"],
                                  "answer_segment_ids": pair["evidence_segment_ids"]} for pair in pairs],
        "dimensions": {},
        "rationale": "需要结合面试官实际问题判断回答是否对题；本地规则不按长度或关键词给分。",
        "boundary": "仅评价本轮回答表现，不推断智力、人格或长期潜力。",
    }


def _has_traceable_depth(
    question: dict[str, Any], text: str, signals: dict[str, bool]
) -> bool:
    character_count = len(re.sub(r"\s+", "", text))
    if character_count < 4:
        return False
    # A bare action placeholder is not a description of what happened. This
    # does not reject concise, concrete answers (e.g. "我负责接口和部署").
    if re.fullmatch(r"[我会就先们\s]*(?:跟客户)?(?:处理|解释|沟通|做|弄|协调)(?:了)?(?:一下|一下子|一点)[。！!\s]*", text):
        return False
    required = _required_dimensions(question)
    if required:
        present = sum(signals.get(signal_key, False) for _, signal_key in required)
        return present == len(required)
    observable = sum(
        signals.get(key, False)
        for key in ("action", "ownership", "mechanism", "decision_basis", "constraint", "reflection", "causal")
    )
    return observable >= 2


def _missing_dimensions(question: dict[str, Any], signals: dict[str, bool]) -> list[str]:
    return [label for label, signal_key in _required_dimensions(question) if not signals.get(signal_key, False)]


def _required_dimensions(question: dict[str, Any]) -> list[tuple[str, str]]:
    """Select only the evidence that the actual question makes decision-relevant."""
    text = str(question.get("question", ""))
    # A resume quote may itself mention growth, delivery or metrics. That does
    # not mean the interviewer is currently asking the candidate to prove them.
    text = re.sub(r"“[^”]{0,600}”", "", text)
    required: list[tuple[str, str]] = []
    candidates = (
        (OWNERSHIP_QUESTION_MARKERS, ("责任边界", "ownership")),
        (MECHANISM_QUESTION_MARKERS, ("实现机制", "mechanism")),
        (DECISION_QUESTION_MARKERS, ("判断依据", "decision_basis")),
        (DELIVERY_QUESTION_MARKERS, ("结果验证", "result")),
        (METRIC_QUESTION_MARKERS, ("衡量依据", "metric")),
        (("约束", "阻力", "冲突", "压力", "风险", "资源不足", "最难", "适应", "排班"), ("约束或阻力", "constraint")),
        (("失败", "复盘", "错误", "修正", "重来"), ("复盘与修正", "reflection")),
    )
    for markers, dimension in candidates:
        if any(marker in text for marker in markers) and dimension not in required:
            required.append(dimension)
    return required


def _answer_anchor(answer_segments: list[TranscriptSegment]) -> str:
    """Return an exact, recent quote that a follow-up can visibly point to."""
    for segment in reversed(answer_segments):
        quote = best_substantive_quote(segment.effective_text, max_chars=72)
        if quote:
            return quote
    combined = " ".join(
        value
        for item in answer_segments
        if (value := trim_leading_fillers(item.effective_text))
    )
    return combined[-72:] if is_evidence_worthy_utterance(combined, min_chars=4) else ""


def _question_gap_suggestions(states: list[dict[str, Any]], active_question_id: str | None) -> list[dict[str, Any]]:
    # The left panel owns unanswered prepared questions. The right panel is a
    # true follow-up area: it may only react to the candidate's current answer.
    item = next(
        (
            state
            for state in states
            if state["question_id"] == active_question_id
            and state["status"] == "shallow"
            and state.get("basis_quote")
        ),
        None,
    )
    if not item:
        return []
    if re.search(r"(?:…|\.\.\.|然后|首先|接着|因为|所以|还有|先看了日志)[。\s]*$", item.get("answer_text", "")):
        return []

    missing = item["missing_dimensions"]
    reason = f"“{item['competency_name']}”的这段回答较浅"
    if missing:
        reason += f"，尚缺少{'、'.join(missing[:2])}"
    # ASR fragment count is not the number of failed attempts to answer.
    follow_up_stage = 0
    selected_gap = missing[follow_up_stage % len(missing)] if missing else "可核验细节"
    quote = item["basis_quote"]
    follow_ups = {
        "个人行动": (
            f"你刚才说“{quote}”。这里面你本人亲手做的是哪一步？",
            f"你提到“{quote}”。先不讲团队整体，只说你作出的一个具体决定是什么？",
            f"围绕“{quote}”，如果让当时的人核实，哪个动作能确认是你完成的？",
        ),
        "责任边界": (
            f"你提到“{quote}”。这部分是你亲自完成、共同完成，还是主要由团队成员完成？",
            f"围绕“{quote}”，如果没有团队协助，哪一部分你仍能独立完成？",
            f"你刚才说“{quote}”。请只说一个由你本人作出的关键判断。",
        ),
        "实现机制": (
            f"你提到“{quote}”。其中最关键的一步是怎么运作的？",
            f"围绕“{quote}”，如果这一步出错，你会先检查哪里？",
            f"你刚才说“{quote}”。为什么选择这种做法，而不是另一个常见方案？",
        ),
        "判断依据": (
            f"你提到“{quote}”。当时哪条信息真正改变了你的选择？",
            f"围绕“{quote}”，你排除了哪个方案？为什么？",
            f"你刚才说“{quote}”。如果关键前提改变，你的判断会怎么变？",
        ),
        "结果验证": (
            f"你刚才说“{quote}”。哪一个后续变化最能说明这次处理起了作用？",
            f"你提到“{quote}”。结束时，哪些预期实现了，哪些没有？",
            f"围绕“{quote}”，如果当时的目标没有完全达成，真正卡在哪里？",
        ),
        "衡量依据": (
            f"你刚才说“{quote}”。当时有什么记录或反馈支持这个判断？",
            f"你提到“{quote}”。如果没有精确数字，你当时依据什么判断方向是对的？",
            f"围绕“{quote}”，哪一份交付物或哪类反馈最能验证你的说法？",
        ),
        "约束或阻力": (
            f"你刚才说“{quote}”。当时真正卡住你的是什么？",
            f"你提到“{quote}”。哪个环节最容易失败，你做了什么应对？",
            f"围绕“{quote}”，如果资源少一半，你会保留哪一步？",
        ),
        "复盘与修正": (
            f"你刚才说“{quote}”。后来你发现哪个判断需要调整？",
            f"你提到“{quote}”。如果重来一次，你最先会改哪一步？",
            f"围绕“{quote}”，之后遇到类似情况时，你真正改变了什么做法？",
        ),
        "可核验细节": (
            f"你刚才说“{quote}”。其中哪一点最能帮助我们理解你的判断？",
            f"你提到“{quote}”。能选一个关键细节说明你为什么这样理解吗？",
            f"围绕“{quote}”，还有哪条事实会改变我们对这件事的判断？",
        ),
    }
    variants = follow_ups[selected_gap]
    question = variants[min(follow_up_stage, len(variants) - 1)]
    output = []
    priority = "high"
    for item in [item]:
        output.append(
            {
                "reason": reason,
                "question": question,
                "source_question_text": item["question"],
                "priority": priority,
                "question_id": item["question_id"],
                "source": "question_gap",
                "answer_status": "shallow",
                "evidence_gap": {
                    "个人行动": "personal_action",
                    "责任边界": "ownership_boundary",
                    "实现机制": "mechanism",
                    "判断依据": "decision_basis",
                    "结果验证": "result",
                    "衡量依据": "metric",
                    "约束或阻力": "constraint",
                    "复盘与修正": "reflection",
                    "可核验细节": "other",
                }.get(selected_gap, "other"),
                "follow_up_stage": follow_up_stage,
                "evidence_segment_ids": item["evidence_segment_ids"],
                "basis_quote": quote,
            }
        )
    return output

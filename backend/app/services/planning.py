from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, Candidate, CandidateProfile, EvidenceItem, InterviewRound, Job, Scorecard
from .catalog import competencies_for
from .company_profile import company_required_questions
from .job_semantics import build_local_job_semantic_profile
from .resume_recognition import recognize_resume


def preparation_context(db: Session, interview: InterviewRound) -> dict[str, Any]:
    application = db.get(Application, interview.application_id)
    candidate = db.get(Candidate, application.candidate_id) if application else None
    job = db.get(Job, application.job_id) if application else None
    profile = db.scalar(select(CandidateProfile).where(CandidateProfile.candidate_id == candidate.id)) if candidate else None
    recognized = profile.structured_data if profile else (recognize_resume(candidate.resume_text, f"{candidate.display_name}.txt")["fields"] if candidate else {})
    labels = {
        "current_company": "当前/最近公司", "current_title": "当前/最近职位",
        "years_experience": "工作年限", "highest_education": "最高学历", "location": "所在城市",
    }
    facts = []
    for key, label in labels.items():
        value = recognized.get(key)
        if value is not None and value != "":
            facts.append({"key": key, "label": label, "value": f"{value}年" if key == "years_experience" else str(value), "source": "candidate_profile" if profile else "resume_text"})
    semantic_profile = (
        (job.semantic_profile or {})
        if job
        else {}
    )
    if job and not semantic_profile:
        semantic_profile = build_local_job_semantic_profile(job.title, job.jd_text)
    dimensions = list(semantic_profile.get("interview_dimensions", []))
    focus_terms = [
        str(item.get("name"))
        for item in dimensions
        if item.get("name") and item.get("round_type") == interview.round_type
    ]
    return {
        "candidate_facts": facts,
        "job_focus_terms": focus_terms,
        "personalization_status": "ready" if facts or focus_terms else "limited",
        "boundary": "仅用于生成核实性问题，不代表候选人已具备或不具备相关能力",
        "candidate_name": candidate.display_name if candidate else "候选人",
        "job_title": job.title if job else "目标岗位",
        "role_level": _role_level(job.title if job else "", job.jd_text if job else ""),
        "job_role_mission": semantic_profile.get("role_mission"),
        "job_business_outcomes": list(semantic_profile.get("business_outcomes", [])),
        "job_work_scenarios": list(semantic_profile.get("work_scenarios", [])),
        "jd_interview_dimensions": dimensions,
        "jd_analysis_mode": semantic_profile.get("analysis_mode", "local_structured_fallback"),
        "jd_analysis_provider": semantic_profile.get("provider", "local-structured-rules"),
        "excluded_non_job_factors": list(semantic_profile.get("excluded_non_job_factors", [])),
        "profile": recognized,
        "_resume_text": candidate.resume_text if candidate else "",
        "candidate_stage": _candidate_stage(candidate.resume_text if candidate else "", recognized),
    }


def personalized_questions(context: dict[str, Any], round_type: str) -> list[dict[str, Any]]:
    semantic_dimensions = [
        item
        for item in context.get("jd_interview_dimensions", [])
        if item.get("round_type") == round_type
    ]
    output: list[dict[str, Any]] = []
    matched_dimensions = _match_resume_experiences(
        context.get("_resume_text", ""), semantic_dimensions
    )
    for index, (dimension, resume_excerpt) in enumerate(matched_dimensions[:2]):
        dimension_id = str(dimension.get("id") or f"{round_type}-{index + 1}")
        dimension_name = str(dimension.get("name") or "岗位职责核实")
        evidence_target = str(dimension.get("evidence_target") or "具体情境、本人行动和可验证结果")
        spoken_question, spoken_follow_up = _spoken_resume_match_question(
            resume_excerpt,
            round_type=round_type,
            candidate_stage=context.get("candidate_stage", "experienced"),
        )
        output.append({
            "id": f"q-{round_type}-resume-match-{index + 1}",
            "competency_id": f"jd_semantic.{dimension_id}",
            "competency_name": f"简历相关经历 · {dimension_name}",
            "question": spoken_question,
            "follow_up": spoken_follow_up,
            "required": False,
            "source": "resume_jd_match",
            "rationale": f"JD 只用于选择重点，问题从候选人简历中的相近经历出发；核实：{evidence_target}",
            "source_evidence": f"简历原文：{resume_excerpt}",
            "keywords": _matching_terms(str(dimension.get("source_excerpt") or ""), resume_excerpt),
            "jd_analysis_mode": context.get("jd_analysis_mode"),
        })
    return output


def prior_round_context(db: Session, current: InterviewRound) -> list[dict[str, Any]]:
    """Expose traceable facts, not prior conclusions, to reduce anchoring."""
    conditions = [
        InterviewRound.application_id == current.application_id,
        InterviewRound.id != current.id,
        InterviewRound.status == "completed",
    ]
    if current.scheduled_at is not None:
        conditions.append(InterviewRound.scheduled_at < current.scheduled_at)
    prior_rounds = db.scalars(
        select(InterviewRound)
        .where(*conditions)
        .order_by(InterviewRound.scheduled_at, InterviewRound.created_at)
    ).all()
    output: list[dict[str, Any]] = []
    for prior in prior_rounds:
        confirmed = db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.interview_round_id == prior.id,
                EvidenceItem.human_status.in_(["confirmed", "modified"]),
            )
        ).all()
        scorecard = db.scalar(
            select(Scorecard).where(Scorecard.interview_round_id == prior.id)
        )
        output.append(
            {
                "source_round_id": prior.id,
                "source_round_type": prior.round_type,
                "confirmed_evidence": [
                    {
                        "competency_id": item.competency_id,
                        "quote": item.quote,
                        "evidence_id": item.id,
                    }
                    for item in confirmed
                ],
                "unverified_questions": (scorecard.next_round_questions if scorecard else []),
                "excluded_to_reduce_anchoring": ["numeric_scores", "recommendation"],
            }
        )
    return output


def build_plan(
    db: Session, interview: InterviewRound, job_competencies: list[dict]
) -> dict[str, Any]:
    competencies = competencies_for(interview.round_type, job_competencies)
    context = preparation_context(db, interview)
    standard_questions = []
    company_questions, company_profile_version = company_required_questions(
        db, interview.round_type
    )
    # A round has one fixed comparison question at most. If HR has published a
    # company-wide standard, use its highest-priority item; otherwise use the
    # first round competency. Everything else starts from this candidate's CV.
    company_questions = company_questions[:1]
    for question in company_questions:
        spoken = _spoken_standard_question(
            {
                "id": str(question.get("competency_id", "")).removeprefix("company."),
                "name": question.get("competency_name", "公司通用能力"),
                "question": question.get("question"),
                "follow_up": question.get("follow_up"),
            },
            candidate_stage=context.get("candidate_stage", "experienced"),
        )
        question["question"], question["follow_up"] = spoken
        question["required"] = True
    if not company_questions and competencies:
        item = competencies[0]
        spoken = _spoken_standard_question(
            item, candidate_stage=context.get("candidate_stage", "experienced")
        )
        standard_questions.append(
            {
                "id": f"q-{interview.round_type}-{item['id']}",
                "competency_id": item["id"],
                "competency_name": item["name"],
                "question": spoken[0],
                "follow_up": spoken[1],
                "keywords": item.get("keywords", []),
                "required": True,
                "source": "round_standard",
            }
        )
    personalized = personalized_questions(context, interview.round_type)
    prior_context = prior_round_context(db, interview)
    carryover = []
    for prior in prior_context:
        for index, item in enumerate(prior.get("unverified_questions", [])[:2]):
            question_text = item.get("question") if isinstance(item, dict) else str(item)
            if question_text:
                carryover.append({
                    "id": f"q-{interview.round_type}-prior-{prior['source_round_id']}-{index}",
                    "competency_id": "prior_round_followup", "competency_name": "前轮待验证",
                    "question": question_text, "follow_up": "请补充具体行动、结果和可验证细节。",
                    "required": False, "source": "prior_round", "rationale": "由前轮已记录的待验证问题带入",
                    "source_evidence": f"前轮：{prior['source_round_type']}",
                })
    questions = [*company_questions, *standard_questions, *personalized, *carryover]
    return {
        "version": "plan-v1.1",
        "interview_mode": interview.interview_mode,
        "mode_label": "结构化提问与评分" if interview.interview_mode == "structured" else "自由对话分析",
        "question_bank_version": f"{interview.round_type}-standard-v0.1",
        "personalization_version": "resume-evidence-first-v1.0",
        "round_type": interview.round_type,
        "company_profile_version": company_profile_version,
        "principles": [
            "AI 只提供可选建议，面试官控制提问顺序",
            "未提及不等于不具备",
            "JD 只决定核实重点，不直接生成脱离简历的经历题",
            "结论必须能回到逐字稿证据",
            "禁止基于性别、年龄、婚育等非岗位因素判断",
        ],
        "required_questions": [item for item in questions if item["required"]],
        "optional_questions": [item for item in questions if not item["required"]],
        "questions": questions,
        "question_mix": {
            "required": len([item for item in questions if item["required"]]),
            "resume_jd_match": len([item for item in questions if item["source"] == "resume_jd_match"]),
            "resume_personalized": len([item for item in questions if item["source"] == "resume_personalized"]),
            "prior_round": len([item for item in questions if item["source"] == "prior_round"]),
            **(
                {"company_standard": len(company_questions)}
                if company_questions
                else {}
            ),
        },
        "preparation_context": {
            key: value for key, value in context.items()
            if key != "profile" and not key.startswith("_")
        },
        "prior_round_context": prior_context,
    }


def _role_level(title: str, jd_text: str) -> str:
    basic = ("专员", "助理", "文员", "客服", "销售", "操作员", "仓管", "初级", "实习", "前台", "店员", "跟单")
    senior = ("总监", "负责人", "专家", "架构", "战略", "经营", "管理团队")
    if any(marker in title for marker in basic):
        return "basic"
    if any(marker in f"{title}{jd_text}" for marker in senior):
        return "senior"
    return "professional"


def _short_job_context(value: str) -> str:
    compact = " ".join(value.replace("\n", " ").split()).strip("；。 ")
    return compact if len(compact) <= 56 else f"{compact[:54]}…"


def _spoken_resume_match_question(
    resume_excerpt: str,
    *,
    round_type: str,
    candidate_stage: str,
) -> tuple[str, str]:
    excerpt = _short_job_context(resume_excerpt)
    if candidate_stage == "student":
        lead = f"你简历里写到“{excerpt}”。这段可以是课程、比赛、社团、实习或个人项目，"
    else:
        lead = f"你简历里写到“{excerpt}”。"
    if round_type == "hr":
        return (
            f"{lead}哪件具体的事让你更想继续做这类工作？",
            "同一段经历里，有没有哪件事让你觉得自己并不适合？",
        )
    if round_type == "ceo":
        return (
            f"{lead}如果重新做一次，你会先改哪一步？",
            "是什么事实让你现在会这样改？",
        )
    return (
        f"{lead}这里面哪个判断最难？你当时依据什么作了选择？",
        "如果当时判断错了，最先会出现什么信号？",
    )


def _spoken_standard_question(
    competency: dict[str, Any], *, candidate_stage: str = "experienced"
) -> tuple[str, str]:
    simple = {
        "domain_expertise": ("挑一件和这个岗位最像的工作讲讲，你当时具体做了什么？", "最后做成了吗？你自己完成的是哪一部分？"),
        "problem_solving": ("工作里遇到问题时，你通常怎么找到原因？讲一件最近的事。", "你先试了什么，后来为什么换了做法？"),
        "ownership": ("讲一件工作出了问题、最后需要你来处理的事。", "你做了什么补救，最后结果怎么样？"),
        "collaboration": ("讲一件你需要别人配合才能完成的工作。", "对方一开始不配合时，你是怎么沟通的？"),
        "motivation": ("你为什么想做这个岗位？你最看重哪部分工作？", "哪种实际情况会让你觉得这份工作不适合？"),
        "communication": ("讲一件你和同事意见不一样、最后还要一起把事做完的经历。", "你具体怎么说、怎么调整做法的？"),
        "values": ("讲一件赶结果和守规则发生冲突的事，你最后怎么选？", "你当时放弃了什么，又保住了什么？"),
        "stability": ("你最近几次换工作的主要原因分别是什么？", "这一次你最不希望再次遇到什么？"),
        "strategic_alignment": ("你觉得这个岗位最重要的结果是什么？", "如果时间只够做一件事，你会先做什么？"),
        "learning_agility": ("讲一件你后来发现自己判断错了的事。", "是什么让你改了想法，之后怎么做的？"),
        "leadership_potential": ("讲一件你不是负责人、但还是推动大家把事做成的经历。", "别人为什么愿意配合你？"),
        "risk_judgement": ("讲一件机会不错、但风险也很大的事，你怎么决定做不做？", "你给自己设了什么停止条件？"),
        "integrity": ("讲一件说出真实情况会让自己更麻烦、但你还是选择说清楚的事。", "你承担了什么后果，最后事情怎么处理的？"),
        "long_term_value": ("讲一件眼前结果和长期效果冲突的事，你怎么选？", "你用什么判断这个选择后来是对的？"),
    }
    if candidate_stage == "student":
        student = {
            "domain_expertise": ("从课程、比赛、社团、实习或个人项目里，挑一件和这个岗位最接近的事讲讲。", "其中哪一步最考验你的判断？为什么选择这种做法？"),
            "problem_solving": ("讲一件你在课程、项目或实习里遇到问题的事。", "你怎么找到原因，后来改了哪一步？"),
            "ownership": ("讲一件课程、项目或实习出了问题，而你没有等别人来处理的事。", "你本人做了什么补救，最后怎么样？"),
            "collaboration": ("讲一件你需要同学、队友或实习同事配合才能完成的事。", "对方想法不一样时，你具体怎么沟通？"),
            "learning_agility": ("讲一件你后来发现自己理解错了的事。", "什么信息让你改变了想法，之后怎么做？"),
            "leadership_potential": ("讲一件你不是负责人、但还是推动大家完成任务的事。", "你具体做了什么，别人为什么愿意配合？"),
        }
        if str(competency.get("id", "")).removeprefix("company.") in student:
            return student[str(competency.get("id", "")).removeprefix("company.")]
    return simple.get(
        str(competency.get("id")),
        (
            str(competency.get("question") or f"讲一件能说明你在{competency['name']}方面表现的真实事情。"),
            str(competency.get("follow_up") or "这件事里最难判断的是什么？你依据什么作了选择？"),
        ),
    )


def _candidate_stage(resume_text: str, profile: dict[str, Any]) -> str:
    explicit_student = ("在校", "应届", "毕业生")
    if any(marker in resume_text for marker in explicit_student):
        return "student"
    years = profile.get("years_experience")
    if isinstance(years, (int, float)) and years >= 1:
        return "experienced"
    if "至今" in resume_text and profile.get("current_company"):
        return "experienced"
    early_career_markers = ("实习", "课程项目", "毕业设计", "毕设", "社团", "学生会", "校园", "竞赛")
    return "student" if years == 0 or any(marker in resume_text for marker in early_career_markers) else "experienced"


_RESUME_SECTION_MARKERS = re.compile(
    r"工作经历|项目经历|实习经历|实践经历|校园经历|教育经历|课程项目|竞赛经历|社团经历"
)
_RESUME_NOISE = re.compile(
    r"^(?:姓名|电话|手机|邮箱|学历|求职意向|个人信息|基本信息|当前公司|目前公司|所在公司|当前职位|目前职位|应聘职位|职位)\s*[:：]"
)
_MATCH_CONCEPTS: dict[str, tuple[str, ...]] = {
    "ai": ("AI", "人工智能", "大模型", "模型", "算法", "机器学习", "提示词", "prompt", "智能体", "agent"),
    "data": ("数据", "分析", "指标", "报表", "SQL", "统计"),
    "growth": ("增长", "转化", "拉新", "留存", "流量", "用户运营"),
    "operations": ("运营", "活动", "内容", "新媒体", "社群"),
    "product": ("产品", "需求", "原型", "用户研究", "迭代"),
    "project": ("项目", "推进", "交付", "实施", "落地"),
    "customer": ("客户", "客服", "投诉", "服务", "客诉"),
    "sales": ("销售", "商务", "成交", "回款", "渠道"),
    "people": ("招聘", "人力", "HR", "培训", "绩效"),
    "engineering": ("开发", "编程", "Python", "Java", "前端", "后端", "研发", "代码"),
    "collaboration": ("协作", "跨部门", "沟通", "协调", "团队"),
}


def _resume_excerpts(resume_text: str) -> list[str]:
    excerpts: list[str] = []
    current_section = ""
    for raw_line in resume_text.splitlines():
        line = " ".join(raw_line.split()).strip(" ·•|-—；;。")
        if not line:
            continue
        if _RESUME_SECTION_MARKERS.fullmatch(line):
            current_section = line
            continue
        if _RESUME_NOISE.search(line) or re.fullmatch(r"[\d./年月日\s~至到—-]+", line):
            continue
        for part in re.split(r"[。；;]", line):
            part = part.strip(" ·•|-—，,")
            if 8 <= len(part) <= 180:
                excerpt = f"{current_section}：{part}" if current_section and current_section not in part else part
                if excerpt not in excerpts:
                    excerpts.append(excerpt)
    return excerpts


def _concept_hits(text: str) -> set[str]:
    lowered = text.lower()
    return {
        name
        for name, aliases in _MATCH_CONCEPTS.items()
        if any(alias.lower() in lowered for alias in aliases)
    }


def _meaningful_bigrams(text: str) -> set[str]:
    normalized = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text).lower()
    stop = {"负责", "工作", "岗位", "能力", "进行", "相关", "要求", "完成", "以及", "通过", "经验"}
    return {
        normalized[index:index + 2]
        for index in range(max(0, len(normalized) - 1))
        if normalized[index:index + 2] not in stop
    }


def _match_score(job_text: str, resume_excerpt: str) -> float:
    shared_concepts = _concept_hits(job_text) & _concept_hits(resume_excerpt)
    shared_bigrams = _meaningful_bigrams(job_text) & _meaningful_bigrams(resume_excerpt)
    return len(shared_concepts) * 2.0 + min(len(shared_bigrams), 6) * 0.35


def _match_resume_experiences(
    resume_text: str, dimensions: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], str]]:
    excerpts = _resume_excerpts(resume_text)
    candidates: list[tuple[float, dict[str, Any], str]] = []
    for dimension in dimensions:
        job_text = " ".join(
            str(dimension.get(key) or "")
            # Match only the actual JD excerpt. Generic rubric words such as
            # "项目行为" or "协作能力" would create false relevance.
            for key in ("job_context", "source_excerpt")
        )
        scored = sorted(
            ((_match_score(job_text, excerpt), excerpt) for excerpt in excerpts),
            reverse=True,
        )
        if scored and scored[0][0] >= 1.05:
            candidates.append((scored[0][0], dimension, scored[0][1]))
    output: list[tuple[dict[str, Any], str]] = []
    used_excerpts: set[str] = set()
    for _, dimension, excerpt in sorted(candidates, key=lambda item: item[0], reverse=True):
        if excerpt in used_excerpts:
            continue
        output.append((dimension, excerpt))
        used_excerpts.add(excerpt)
    return output


def _matching_terms(job_text: str, resume_excerpt: str) -> list[str]:
    terms: list[str] = []
    for name in _concept_hits(job_text) & _concept_hits(resume_excerpt):
        alias = next(
            (value for value in _MATCH_CONCEPTS[name] if value.lower() in resume_excerpt.lower()),
            name,
        )
        terms.append(alias)
    return terms[:6]

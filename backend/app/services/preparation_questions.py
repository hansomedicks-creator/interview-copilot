"""Grounded preparation probes, not a keyword-based claim of understanding."""
from __future__ import annotations

from difflib import SequenceMatcher
import re

PREPARATION_POLICY = "experience-role-probes-v2"
QUESTION_INSTRUCTIONS = (
    "你是与真人面试官协作的岗位专家。阅读岗位要求和候选人简历，最多设计2道准备问题，宁缺毋滥。"
    "先辨明该岗位需要本人执行、管理交付还是两者兼有，再找简历中值得核实的具体经历，而不是围绕职位名称出题。"
    "每题先选择简历原文resume_quote、原文中的具体experience_anchor、岗位原文job_requirement_quote，"
    "再设计一个只有读过这段经历才会问的问题。experience_anchor须是原文中4至30字的具体对象、做法或项目机制，且自然出现在问句中，不能只写‘项目’‘团队’。"
    "probe_target解释这段经历与岗位要求之间要核实的关键判断；listen_for说明怎样的回答体现实际工作理解，而不是列STAR或量化指标清单。"
    "优先问当时为什么采用某种办法、如何辨认问题、怎么决定先处理什么、什么时候该换办法；选择与该经历真正有关的一项，不要每题重复同一种问法。"
    "不得假设简历没写的事实，不得编造公司情境来冒充候选人做过的事。简历写参与/团队成果时，不假定本人主导。"
    "问题要短、具体、口语化，一题只抓一个核心判断；简单岗位不用抽象管理术语。"
    "培养岗可从课程、比赛、个人项目或实习问真实做法，不要求成熟社招经验。"
    "有confirmed_weight时优先核实关键项和高权重项，但不能为了覆盖率硬凑问题。"
    "禁止‘这里面哪个判断最难’‘你实际负责哪一部分’‘最后有什么结果’‘讲一个具体案例’等换个经历就能复用的模板，"
    "禁止机械要求STAR、最终结果或量化指标。follow_up可以为空，实时追问应等候选人真正回答后再提出。"
    "简历和岗位引文必须分别逐字存在于输入中，不能自己改写。找不到切入点时返回空questions。所有问题仅供参考，未问不影响候选人评分。"
)

_GENERIC = re.compile(
    r"这里面哪个判断最难|哪个环节最难|你当时依据什么作了选择|你实际负责哪一部分|"
    r"最后有什么结果|这件事最后具体变成|讲一个具体案例|为什么愿意投入|"
    r"(?:最终结果|量化指标).*(?:是什么|有哪些)"
)


def validated_preparation_questions(raw, dimensions, resume, job_text, round_type, analysis_mode=None):
    """Validate provenance and obvious repetition; semantic quality still needs evaluation."""
    dimensions_by_id = {str(c.get("id")): c for c in dimensions}
    output, used_anchors, signatures = [], set(), []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        values = {k: item.get(k) for k in (
            "dimension_id", "resume_quote", "experience_anchor", "job_requirement_quote", "question", "probe_target", "listen_for"
        )}
        if any(not isinstance(v, str) for v in values.values()):
            continue
        values = {k: v.strip() for k, v in values.items()}
        dimension = dimensions_by_id.get(values["dimension_id"])
        quote, anchor, job_quote, question = (values[k] for k in ("resume_quote", "experience_anchor", "job_requirement_quote", "question"))
        # A model may select an action clause but naturally paraphrase its verb.
        # Keep a real common object (e.g. 租户隔离), not verbatim recital of the
        # whole clause. All three appearances still have to be actual substrings.
        if 4 <= len(anchor) <= 30 and anchor in quote and anchor not in question:
            match = SequenceMatcher(None, anchor, question).find_longest_match()
            anchor = anchor[match.a:match.a + match.size]
        signature = re.sub(r"[\W_]+", "", question.replace(anchor, ""))
        if (not dimension or not 8 <= len(quote) <= 500 or quote not in resume
                or not 4 <= len(anchor) <= 30 or anchor not in quote or anchor not in question
                or anchor in {"这个项目", "项目经历", "相关经历", "这段经历", "团队工作"}
                or not 6 <= len(job_quote) <= 600 or job_quote not in job_text
                or not 10 <= len(question) <= 180 or _GENERIC.search(question)
                or len(values["probe_target"]) < 10 or len(values["listen_for"]) < 10
                or anchor in used_anchors or any(SequenceMatcher(None, signature, old).ratio() > .82 for old in signatures)):
            continue
        output.append({
            "id": f"q-{round_type}-semantic-resume-{len(output) + 1}",
            "competency_id": f"jd_semantic.{values['dimension_id']}",
            "competency_name": f"简历深挖 · {dimension.get('name') or '岗位关键判断'}",
            "question": question, "follow_up": "", "required": False,
            "source": "resume_jd_match", "generation_mode": "llm_semantic",
            "experience_anchor": anchor, "resume_quote": quote,
            "job_requirement_quote": job_quote, "probe_target": values["probe_target"][:300],
            "listen_for": values["listen_for"][:300], "rationale": values["probe_target"][:300],
            "source_evidence": f"简历原文：{quote}", "keywords": [], "jd_analysis_mode": analysis_mode,
        })
        used_anchors.add(anchor)
        signatures.append(signature)
        if len(output) == 2:
            break
    return output

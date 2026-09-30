"""Presentation contract for a candidate review, independent of question usage."""
SCORE_GUIDE = [
    {"range": "0 ≤ 分数 < 1", "label": "明显不符合", "meaning": "已有明确事实显示无法完成岗位核心工作；建议不通过，需人工确认。"},
    {"range": "1 ≤ 分数 < 2", "label": "低于岗位要求", "meaning": "已观察到关键能力或工作方式的明显差距。"},
    {"range": "2 ≤ 分数 < 3", "label": "部分符合", "meaning": "已展现相关基础，但实际履职仍需较多指导或关键能力尚未达到岗位要求；不是默认通过档。"},
    {"range": "3 ≤ 分数 < 4", "label": "达到岗位要求", "meaning": "具体事实支持独立承担主要职责，能说明本人操作、判断依据及异常处理；相关经历或表达流畅本身不够。"},
    {"range": "4 ≤ 分数 ≤ 5", "label": "较强匹配", "meaning": "关键职责有充分事实，能处理本岗位真实复杂性并解释取舍和适用边界；接近5分需多项相互印证的突出表现。"},
]

ASSESSMENT_POLICY = "job-readiness-strict-v1"
HIRING_CRITERIA = (
    "采用严格的岗位履职标准，主要面向社招。先从JD识别最重要的工作、交付责任和亲自执行/管理的要求，再评价实际对话。"
    "不得仅凭相关行业经历、年限、头衔、工具名或表达流畅给高分。团队成绩不能直接算成本人能力，管理经验不能替代执行型岗位的亲手操作；管理岗位也不能一律按执行岗评价。"
    "0至1分只用于已有明确事实表明无法承担核心工作；1至2分为已观察到明显差距；2至3分为相关基础但履职仍需较多指导；"
    "3至4分必须有独立完成主要职责、本人判断与处理问题的具体事实；4至5分还要解释关键机制、取舍和适用边界，接近5分需多项相互印证的突出表现。"
    "建议通过必须说明本轮负责核验的核心要求为何已达到，不能用一般优点抵消已观察到的核心不匹配。只凭自称熟悉、参与或带团队而责任边界未明，不能据此认定独立履职。"
    "未问到、证据缺失和ASR不确定保持未知、降低置信度，不因此压低能力分或认定不通过；若核心履职尚无法确认，可以具体分析已知事实并建议补充关键验证。"
    "岗位明确为实习、应届或培养岗时按该岗位入职要求，接受课程、个人项目中的实际能力证据，不要求资深社招经验；不要根据候选人的年龄或身份擅自降低岗位标准。"
)
HIRING_BAR = HIRING_CRITERIA + (
    "rationale用120至220字概括总体匹配、最重要的优势/差距与本轮倾向；job_fit_analysis按岗位主题综合分析，不能按时间、题号或对话批次复述。"
    "主题为核心工作匹配、亲自履职与判断、经验迁移与上手条件、关键差距与用人建议，择有信息的3至4项，每项80至160字。"
    "每项说明岗位需要什么、候选人实际展示什么、两者关系以及判断边界，引用相关候选人证据ID；没有证据的主题只标未知，不补写事实。"
)

FIT_TOPICS = {"core_work": "核心工作匹配", "execution": "亲自履职与判断",
              "transfer": "经验迁移与上手条件", "gaps": "关键差距与用人建议"}


def validated_fit_analysis(items, allowed_ids):
    """Only accept thematic observations grounded in the reviewed dialogue."""
    output = []
    seen = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        topic = item.get("topic")
        refs = item.get("evidence_segment_ids") or []
        analysis = str(item.get("analysis", "")).strip()
        if (not isinstance(topic, str) or topic not in FIT_TOPICS or topic in seen or not analysis or not isinstance(refs, list)
                or not refs or any(not isinstance(ref, str) or ref not in allowed_ids for ref in refs)):
            continue
        seen.add(topic)
        output.append({"topic": topic, "title": FIT_TOPICS[topic], "analysis": analysis[:800],
                       "evidence_segment_ids": list(dict.fromkeys(refs))[:12]})
    return output

ERROR_LABELS = {
    "timeout": "模型响应超时", "connection_error": "模型连接失败",
    "authentication_error": "模型鉴权失败", "insufficient_balance": "模型账户余额不足",
    "rate_limited": "模型请求限流", "invalid_response": "模型返回格式无法验证",
    "output_truncated": "模型输出未完成", "upstream_error": "模型服务错误",
}


def candidate_report(judgment, *, error_code=None):
    if judgment is None:
        return {"status": "unavailable", "summary": "AI 尚未完成对话分析，不代表候选人不符合岗位。",
                "details": [], "strengths": [], "risks": [], "unknowns": [],
                "error_code": error_code, "error_message": ERROR_LABELS.get(error_code, "当前没有可核验的模型评价"),
                "score_guide": SCORE_GUIDE, "planned_question_dependency": False}
    return {"status": "complete" if judgment["batch_status"]["status"] == "complete" else "partial",
            "summary": judgment["rationale"], "details": judgment.get("analysis_details", []),
            "job_fit_analysis": judgment.get("job_fit_analysis", []),
            "summary_status": judgment.get("summary_status", "observations_only"),
            "assessment_policy": ASSESSMENT_POLICY,
            "strengths": judgment["positive_evidence"], "risks": judgment["risks"], "unknowns": judgment["unknowns"],
            "score_guide": SCORE_GUIDE, "planned_question_dependency": False}

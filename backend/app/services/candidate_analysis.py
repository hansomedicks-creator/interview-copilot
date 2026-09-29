"""Presentation contract for a candidate review, independent of question usage."""
SCORE_GUIDE = [
    {"range": "0 ≤ 分数 < 1", "label": "明显不符合", "meaning": "已有明确事实显示无法完成岗位核心工作；建议不通过，需人工确认。"},
    {"range": "1 ≤ 分数 < 2", "label": "低于岗位要求", "meaning": "已观察到关键能力或工作方式的明显差距。"},
    {"range": "2 ≤ 分数 < 3", "label": "部分符合", "meaning": "有相关基础，但关键工作能否独立完成仍需核实。"},
    {"range": "3 ≤ 分数 < 4", "label": "基本符合", "meaning": "已有事实支持主要职责；可建议通过本轮，并注明下一轮重点。"},
    {"range": "4 ≤ 分数 ≤ 5", "label": "较强匹配", "meaning": "关键职责有较充分、具体且可追溯的表现。"},
]

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
            "strengths": judgment["positive_evidence"], "risks": judgment["risks"], "unknowns": judgment["unknowns"],
            "score_guide": SCORE_GUIDE, "planned_question_dependency": False}

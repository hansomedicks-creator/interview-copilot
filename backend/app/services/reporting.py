from __future__ import annotations

from html import escape
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    EvidenceItem,
    InterviewReportVersion,
    InterviewRound,
    new_id,
    utc_now,
)
from .final_review import build_final_review


DECISION_LABELS = {
    "advance": "建议进入下一轮",
    "supplementary_interview": "建议补充面试",
    "hold": "保留讨论",
    "reject": "不建议继续",
    "offer_approval": "进入录用审批",
}


def build_or_refresh_report_draft(
    db: Session,
    application: Application,
    created_by: str,
) -> InterviewReportVersion:
    review = build_final_review(db, application)
    snapshot = _build_snapshot(db, review)
    digest = _content_hash(snapshot)
    draft = db.scalar(
        select(InterviewReportVersion).where(
            InterviewReportVersion.application_id == application.id,
            InterviewReportVersion.status == "draft",
        )
    )
    if draft:
        draft.snapshot_payload = snapshot
        draft.content_hash = digest
        draft.created_by = created_by
        draft.created_at = utc_now()
        return draft

    existing = list(
        db.scalars(
            select(InterviewReportVersion).where(
                InterviewReportVersion.application_id == application.id
            )
        ).all()
    )
    version_number = max((item.version_number for item in existing), default=0) + 1
    report = InterviewReportVersion(
        id=new_id("report"),
        application_id=application.id,
        version_number=version_number,
        version_label=f"report-v{version_number}",
        status="draft",
        snapshot_payload=snapshot,
        content_hash=digest,
        created_by=created_by,
    )
    db.add(report)
    return report


def lock_report(
    db: Session,
    report: InterviewReportVersion,
    locked_by: str,
) -> InterviewReportVersion:
    if report.status != "draft":
        raise ValueError("only a draft report can be locked")
    snapshot = report.snapshot_payload or {}
    readiness = snapshot.get("readiness") or {}
    final_decision = (snapshot.get("identity") or {}).get("human_final_decision")
    if readiness.get("status") != "ready_for_hr_decision" and not final_decision:
        raise ValueError("complete the required human evaluations or record an HR final decision before locking")
    previous = db.scalars(
        select(InterviewReportVersion).where(
            InterviewReportVersion.application_id == report.application_id,
            InterviewReportVersion.status == "locked",
        )
    ).all()
    for item in previous:
        item.status = "superseded"
    report.status = "locked"
    report.locked_by = locked_by
    report.locked_at = utc_now()
    return report


def list_report_versions(db: Session, application_id: str) -> list[InterviewReportVersion]:
    return list(
        db.scalars(
            select(InterviewReportVersion)
            .where(InterviewReportVersion.application_id == application_id)
            .order_by(InterviewReportVersion.version_number.desc())
        ).all()
    )


def report_payload(report: InterviewReportVersion, audience: str) -> dict[str, Any]:
    if audience not in {"management", "hr_archive"}:
        raise ValueError("unsupported report audience")
    snapshot = report.snapshot_payload or {}
    content = dict(snapshot.get("management") or {})
    if audience == "hr_archive":
        content["hr_appendix"] = snapshot.get("hr_appendix") or {}
    return {
        **report_metadata(report),
        "audience": audience,
        "identity": snapshot.get("identity") or {},
        "readiness": snapshot.get("readiness") or {},
        "content": content,
        "governance": snapshot.get("governance") or {},
    }


def report_metadata(report: InterviewReportVersion) -> dict[str, Any]:
    return {
        "id": report.id,
        "application_id": report.application_id,
        "version_number": report.version_number,
        "version_label": report.version_label,
        "status": report.status,
        "content_hash": report.content_hash,
        "created_by": report.created_by,
        "created_at": report.created_at,
        "locked_by": report.locked_by,
        "locked_at": report.locked_at,
        "share_path": f"/?report={report.id}" if report.status == "locked" else None,
    }


def render_report_html(report: InterviewReportVersion, audience: str) -> str:
    payload = report_payload(report, audience)
    identity = payload["identity"]
    content = payload["content"]
    summary = content.get("executive_summary") or {}
    competencies = content.get("competencies") or []
    rounds = content.get("rounds") or []
    evidence = content.get("key_evidence") or []
    hr_appendix = content.get("hr_appendix") or {}
    audience_label = "管理层摘要" if audience == "management" else "HR 完整档案"

    competency_html = "".join(
        f"<div class='competency'><div><b>{escape(str(item.get('competency_name', '能力项')))}</b>"
        f"<small>{item.get('round_count', 0)} 轮人工确认 · {item.get('evidence_count', 0)} 条证据</small></div>"
        f"<strong>{escape(str(item.get('average_human_score', '-')))} / 5</strong></div>"
        for item in competencies
    ) or "<p class='muted'>暂无已确认能力评分。</p>"
    round_html = "".join(
        f"<article class='round'><header><b>{escape(str(item.get('round_label', '面试')))}</b>"
        f"<span>{escape(str(item.get('human_decision_label') or '待提交人工评价'))}</span></header>"
        f"<p>面试官：{escape('、'.join(item.get('interviewer_names') or []) or '待分配')}</p>"
        f"<p>{escape(str(item.get('human_notes') or '暂无评价说明'))}</p></article>"
        for item in rounds
    ) or "<p class='muted'>暂无面试轮次。</p>"
    evidence_html = "".join(
        f"<blockquote><p>“{escape(str(item.get('quote', '')))}”</p>"
        f"<footer>{escape(str(item.get('round_label', '')))} · {escape(str(item.get('competency_name', item.get('competency_id', '能力证据'))))}</footer></blockquote>"
        for item in evidence
    ) or "<p class='muted'>暂无人工确认的逐字稿证据。</p>"
    strengths_html = _html_list(summary.get("strengths") or [], "当前没有达到稳定结论的优势项。")
    risks_html = _html_list(summary.get("risks") or [], "当前没有形成明确的风险结论。")
    disagreements_html = _html_list(summary.get("disagreements") or [], "各轮评价暂未发现明显分歧。")
    appendix_html = ""
    if audience == "hr_archive":
        artifacts = "".join(
            f"<li><b>{escape(str(item.get('round_label', '面试')))}</b>："
            f"<a href='{escape(str(item.get('transcript_url', '#')))}'>逐字稿</a> · "
            f"{len(item.get('recordings') or [])} 份录音</li>"
            for item in hr_appendix.get("artifacts", [])
        ) or "<li>暂无逐字稿或录音产物。</li>"
        quality = "".join(
            f"<li><b>{escape(str(item.get('round_label', '面试')))}</b>：{escape(str(item.get('status', '待复盘')))}；"
            f"质量提示 {len((item.get('metrics') or {}).get('flags') or [])} 项</li>"
            for item in hr_appendix.get("interviewer_quality", [])
        ) or "<li>暂无面试质量复盘。</li>"
        logic_reviews = "".join(
            f"<li><b>{escape(str(item.get('round_label', '面试')))}</b>："
            f"{escape(str((item.get('review') or {}).get('label', '暂不可评')))}；"
            f"待核验表述 {len((item.get('review') or {}).get('consistency_flags') or [])} 项</li>"
            for item in hr_appendix.get("answer_logic_reviews", [])
        ) or "<li>暂无回答逻辑核验。</li>"
        appendix_html = f"""
        <section><h2>HR 内部附录</h2>
          <div class='two'><div><h3>档案链接</h3><ul>{artifacts}</ul></div>
          <div><h3>面试官质量复盘</h3><ul>{quality}</ul></div></div>
          <div><h3>回答逻辑与一致性核验</h3><ul>{logic_reviews}</ul></div>
          <p class='notice'>该附录仅供 HR 流程治理，不应出现在候选人反馈或管理层摘要中。</p>
        </section>"""

    return f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{escape(identity.get('candidate_name', '候选人'))} · 岗位面试报告</title>
<style>
@page {{ size: A4; margin: 16mm; }}
* {{ box-sizing: border-box; }} body {{ margin:0; color:#17201d; font-family:'Microsoft YaHei','PingFang SC',sans-serif; background:#eef2ee; line-height:1.65; }}
.toolbar {{ position:sticky; top:0; display:flex; justify-content:flex-end; padding:12px 24px; background:#17201d; }} button {{ border:0; border-radius:8px; padding:10px 18px; color:white; background:#176b52; font-weight:700; cursor:pointer; }}
main {{ width:min(900px,calc(100% - 32px)); margin:24px auto; padding:42px 48px; background:white; box-shadow:0 18px 50px rgba(20,40,30,.12); }}
.cover {{ padding-bottom:26px; border-bottom:3px solid #176b52; }} .eyebrow {{ color:#176b52; font-size:12px; font-weight:800; letter-spacing:.12em; }} h1 {{ margin:8px 0 4px; font-size:34px; }} h2 {{ margin:0 0 14px; font-size:21px; }} h3 {{ margin:0 0 8px; font-size:16px; }} .meta,.muted,small {{ color:#65716b; }}
.decision {{ margin-top:20px; padding:18px; border-radius:12px; background:#eef7f2; }} .decision strong {{ display:block; margin-bottom:6px; color:#176b52; font-size:22px; }}
section {{ padding:24px 0; border-bottom:1px solid #dfe5df; break-inside:avoid; }} .three,.two {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }} .two {{ grid-template-columns:1fr 1fr; }} .box {{ padding:15px; border-radius:10px; background:#f7f8f5; }} ul {{ margin:6px 0; padding-left:20px; }}
.competency {{ display:flex; justify-content:space-between; gap:12px; padding:11px 0; border-bottom:1px solid #e4e8e3; }} .competency small {{ display:block; }} .competency strong {{ color:#176b52; font-size:18px; }}
.rounds {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }} .round {{ padding:15px; border:1px solid #dfe5df; border-radius:10px; break-inside:avoid; }} .round header {{ display:flex; justify-content:space-between; gap:8px; }} .round header span {{ color:#176b52; font-size:12px; font-weight:700; }} .round p {{ margin:8px 0 0; font-size:13px; }}
blockquote {{ margin:10px 0; padding:13px 16px; border-left:4px solid #85ad9c; background:#f7f8f5; break-inside:avoid; }} blockquote p {{ margin:0; }} blockquote footer {{ margin-top:7px; color:#65716b; font-size:12px; }}
.notice {{ padding:12px 14px; border-radius:8px; color:#735d34; background:#fff4d8; }} .hash {{ margin-top:24px; color:#7b8580; font-size:10px; word-break:break-all; }}
@media (max-width:700px) {{ main {{ padding:28px 22px; }} .three,.two,.rounds {{ grid-template-columns:1fr; }} }}
@media print {{ body {{ background:white; }} .toolbar {{ display:none; }} main {{ width:100%; margin:0; padding:0; box-shadow:none; }} a {{ color:#17201d; text-decoration:none; }} }}
</style></head><body><div class='toolbar'><button onclick='window.print()'>打印 / 保存为 PDF</button></div><main>
<div class='cover'><div class='eyebrow'>INTERVIEW COPILOT · {escape(audience_label)}</div><h1>{escape(identity.get('candidate_name', '候选人'))}</h1>
<div class='meta'>{escape(identity.get('job_title', '目标岗位'))} · {escape(payload['version_label'])} · {escape(payload['status'])}</div>
<div class='decision'><span>流程结论</span><strong>{escape(str(summary.get('conclusion_label', '等待 HR 确认')))}</strong><p>{escape(str(summary.get('ai_guidance', 'AI 仅提供材料完整度提示，最终决定由人作出。')))}</p></div></div>
<section><h2>管理层摘要</h2><div class='three'><div class='box'><h3>关键优势</h3>{strengths_html}</div><div class='box'><h3>主要风险</h3>{risks_html}</div><div class='box'><h3>评价分歧</h3>{disagreements_html}</div></div></section>
<section><h2>能力项评分</h2>{competency_html}</section>
<section><h2>各轮人工评价</h2><div class='rounds'>{round_html}</div></section>
<section><h2>关键证据摘录</h2>{evidence_html}</section>
{appendix_html}
<p class='hash'>报告摘要：{escape(payload['content_hash'])}<br>生成者：{escape(payload['created_by'])}；锁定者：{escape(str(payload.get('locked_by') or '尚未锁定'))}</p>
</main></body></html>"""


def _build_snapshot(db: Session, review: dict[str, Any]) -> dict[str, Any]:
    round_by_id = {item["id"]: item for item in review["rounds"]}
    evidence_rows: list[dict[str, Any]] = []
    for round_id, round_payload in round_by_id.items():
        evidence = db.scalars(
            select(EvidenceItem).where(
                EvidenceItem.interview_round_id == round_id,
                EvidenceItem.human_status.in_({"confirmed", "modified"}),
            )
        ).all()
        for item in evidence:
            evidence_rows.append({
                "id": item.id,
                "round_id": round_id,
                "round_label": round_payload["round_label"],
                "competency_id": item.competency_id,
                "competency_name": _competency_name(review, item.competency_id),
                "quote": item.quote,
                "direction": item.direction,
                "strength": item.strength,
            })
    evidence_rows.sort(key=lambda item: item["strength"], reverse=True)

    round_summaries = []
    decisions = []
    for item in review["rounds"]:
        scorecard = item.get("scorecard") or {}
        human_decision = scorecard.get("human_decision") or {}
        if human_decision.get("decision"):
            decisions.append((item["round_label"], human_decision["decision"]))
        round_summaries.append({
            "id": item["id"],
            "round_type": item["round_type"],
            "round_label": item["round_label"],
            "status": item["status"],
            "scheduled_at": item["scheduled_at"].isoformat() if item.get("scheduled_at") else None,
            "interviewer_names": item["interviewer_names"],
            "human_decision": human_decision.get("decision"),
            "human_decision_label": DECISION_LABELS.get(human_decision.get("decision")),
            "human_notes": human_decision.get("summary_notes"),
            "final_scores": scorecard.get("final_scores") or [],
        })

    strengths = [
        f"{item['competency_name']}：人工均分 {item['average_human_score']} / 5，来自 {item['round_count']} 轮确认。"
        for item in review["competency_summary"]
        if item["average_human_score"] >= 4
    ]
    risks = [
        f"{item['competency_name']}：人工均分 {item['average_human_score']} / 5，建议复核证据与岗位要求。"
        for item in review["competency_summary"]
        if item["average_human_score"] <= 2.5
    ]
    if review["outstanding_questions"]:
        risks.extend(
            f"待验证：{item['question']}"
            for item in review["outstanding_questions"][:3]
        )
    disagreements = _find_disagreements(review, decisions)
    human_final = review.get("human_final_decision")
    if human_final:
        conclusion = DECISION_LABELS.get(human_final, human_final)
        guidance = "HR 人工流程决定已记录；本报告只呈现作出该决定时的证据快照。"
    elif review["readiness"]["status"] == "ready_for_hr_decision":
        conclusion = "等待 HR 最终确认"
        guidance = "该岗位配置的面试和人工评价材料已齐，可进入 HR 终审；AI 不给出自动录用或淘汰结论。"
    else:
        conclusion = "材料尚未完备"
        guidance = "请先完成缺失的面试或人工评价，再形成最终流程结论。"

    management = {
        "executive_summary": {
            "conclusion_label": conclusion,
            "ai_guidance": guidance,
            "strengths": strengths[:5],
            "risks": risks[:6],
            "disagreements": disagreements[:5],
        },
        "competencies": review["competency_summary"],
        "rounds": round_summaries,
        "key_evidence": evidence_rows[:10],
        "outstanding_questions": review["outstanding_questions"][:8],
    }
    return {
        "generated_at": utc_now().isoformat(),
        "identity": {
            "application_id": review["application_id"],
            "candidate_id": review["candidate"]["id"],
            "candidate_name": review["candidate"]["display_name"],
            "job_id": review["job"]["id"],
            "job_title": review["job"]["title"],
            "source_job_code": review["job"].get("source_job_code"),
            "current_stage": review["current_stage"],
            "human_final_decision": review.get("human_final_decision"),
            "final_decision_details": review.get("final_decision_details"),
        },
        "readiness": review["readiness"],
        "management": management,
        "hr_appendix": {
            "artifacts": [
                {
                    "round_id": item["id"],
                    "round_label": item["round_label"],
                    "transcript_url": item["transcript_url"],
                    "recordings": item["recordings"],
                }
                for item in review["rounds"]
            ],
            "interviewer_quality": [
                {
                    "round_id": item["id"],
                    "round_label": item["round_label"],
                    "status": item["interviewer_quality"]["status"],
                    "metrics": item["interviewer_quality"]["metrics"],
                }
                for item in review["rounds"]
            ],
            "answer_logic_reviews": [
                {
                    "round_id": item["id"],
                    "round_label": item["round_label"],
                    "review": (item.get("scorecard") or {}).get("answer_logic_review", {}),
                }
                for item in review["rounds"]
                if (item.get("scorecard") or {}).get("answer_logic_review")
            ],
            "pending_knowledge_approvals": review["readiness"]["pending_knowledge_approvals"],
            "stage_change": review.get("final_decision_details"),
        },
        "governance": {
            "management_excludes": ["面试官质量评价", "知识审批细节", "内部流程辅导信号"],
            "policy": "AI 只整理证据、完整度与分歧，不自动作出录用或淘汰决定。",
            "locked_snapshot": "锁定后内容不可修改；重新生成会创建下一版本并保留历史。",
        },
    }


def _find_disagreements(
    review: dict[str, Any], decisions: list[tuple[str, str]]
) -> list[str]:
    output = []
    unique_decisions = {item[1] for item in decisions}
    if len(unique_decisions) > 1:
        output.append(
            "轮次结论不一致：" + "；".join(
                f"{round_label}{DECISION_LABELS.get(decision, decision)}"
                for round_label, decision in decisions
            )
        )
    for item in review["competency_summary"]:
        scores = [entry["score"] for entry in item.get("round_scores", [])]
        if len(scores) >= 2 and max(scores) - min(scores) >= 2:
            output.append(
                f"{item['competency_name']}跨轮评分差异较大（{min(scores)} - {max(scores)} 分），建议回看引用证据。"
            )
    return output


def _competency_name(review: dict[str, Any], competency_id: str) -> str:
    for item in review["competency_summary"]:
        if item["competency_id"] == competency_id:
            return item["competency_name"]
    return competency_id


def _content_hash(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _html_list(items: list[str], empty_text: str) -> str:
    if not items:
        return f"<p class='muted'>{escape(empty_text)}</p>"
    return "<ul>" + "".join(f"<li>{escape(str(item))}</li>" for item in items) + "</ul>"

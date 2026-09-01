from types import SimpleNamespace

from app.services.evidence_presentation import build_evidence_digest


def evidence(
    evidence_id: str,
    quote: str,
    *,
    segment_ids: list[str],
    direction: str = "support",
    status: str = "pending",
    strength: float = 0.7,
    competency_id: str = "execution",
):
    return SimpleNamespace(
        id=evidence_id,
        competency_id=competency_id,
        segment_ids=segment_ids,
        quote=quote,
        direction=direction,
        strength=strength,
        explanation="回答描述了候选人本人的具体做法。",
        human_status=status,
    )


def test_digest_merges_same_fact_and_keeps_confirmed_item_as_primary():
    items = [
        evidence(
            "ev_pending",
            "我先查看异常日志，再修改配置并完成了现场恢复。",
            segment_ids=["seg_1"],
            strength=0.86,
        ),
        evidence(
            "ev_confirmed",
            "我查看异常日志后修改配置，完成现场恢复。",
            segment_ids=["seg_1"],
            status="confirmed",
            strength=0.71,
        ),
    ]
    digest = build_evidence_digest(
        items,
        [{"id": "execution", "name": "亲力亲为", "description": "本人能够下场完成关键工作。"}],
        [{
            "question_id": "q_1",
            "question": "当时哪一步是你自己完成的？",
            "source": "interviewer_ad_hoc",
            "status": "evidenced",
            "evidence_segment_ids": ["seg_1"],
        }],
    )

    assert digest["summary"]["raw_count"] == 2
    assert digest["summary"]["cluster_count"] == 1
    assert len(digest["key_evidence"]) == 1
    merged = digest["key_evidence"][0]
    assert merged["primary_evidence_id"] == "ev_confirmed"
    assert merged["related_count"] == 2
    assert merged["source_question_text"] == "当时哪一步是你自己完成的？"
    assert merged["competency_name"] == "亲力亲为"
    assert "岗位判断依据" in merged["why_it_matters"]


def test_unknown_is_not_rendered_as_negative_evidence():
    digest = build_evidence_digest(
        [],
        [],
        [
            {
                "question_id": "q_shallow",
                "competency_id": "judgement",
                "competency_name": "判断能力",
                "question": "你为什么选择这个处理方式？",
                "source": "resume_jd_match",
                "required": False,
                "status": "shallow",
                "basis_quote": "我当时就是这样处理的。",
                "missing_dimensions": ["判断依据"],
            },
            {
                "question_id": "q_unanswered",
                "competency_id": "ownership",
                "competency_name": "本人贡献",
                "question": "其中哪部分是你本人完成的？",
                "source": "resume_personalized",
                "required": False,
                "status": "unanswered",
                "missing_dimensions": [],
            },
        ],
    )

    assert digest["risks"] == []
    assert digest["summary"]["unknown"] == 2
    assert all(item["decision_impact"] == "暂不支持正向或反向结论" for item in digest["unknowns"])


def test_rejected_and_legacy_absence_records_do_not_reappear_in_digest():
    rejected = evidence("ev_rejected", "我亲自完成了客户现场交付。", segment_ids=["seg_1"], status="rejected")
    absence = evidence("ev_absence", "候选人未说明具体做法。", segment_ids=["seg_2"])
    absence.explanation = "未提供证据，无法判断。"

    digest = build_evidence_digest([rejected, absence], [], [])

    assert digest["summary"]["raw_count"] == 2
    assert digest["summary"]["eligible_count"] == 0
    assert digest["key_evidence"] == []
    assert digest["risks"] == []

from __future__ import annotations

import json
import math
from io import BytesIO
import struct
import wave
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zipfile import ZipFile

from fastapi.testclient import TestClient
import httpx

from app.main import create_app
from app.config import Settings
from app.models import (
    Application,
    AudioRecording,
    Candidate,
    EvidenceItem,
    HistoricalHiringSample,
    InterviewRound,
    InterviewReportVersion,
    Job,
    Scorecard,
    TranscriptSegment,
    new_id,
    utc_now,
)
from sqlalchemy import create_engine, select, text
from app.database import Database
from app.providers.feishu_notifications import FeishuNotificationSender
from app.providers.openai_compatible import OpenAICompatibleProvider


def make_client(tmp_path, knowledge_vault_dir=None) -> TestClient:
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        settings=Settings(
            environment="test",
            recording_dir=tmp_path / "recordings",
            knowledge_vault_dir=knowledge_vault_dir,
        ),
    )
    return TestClient(app)


def bootstrap(client: TestClient) -> dict:
    response = client.post("/api/v1/demo/bootstrap")
    assert response.status_code == 201
    return response.json()


def acknowledge_and_start(client: TestClient, interview_id: str) -> None:
    response = client.post(
        f"/api/v1/interviews/{interview_id}/notice",
        json={"acknowledged_by": "tester", "candidate_was_notified": True},
    )
    assert response.status_code == 200
    response = client.post(f"/api/v1/interviews/{interview_id}/start")
    assert response.status_code == 200


def test_hr_can_create_an_atomic_three_round_interview_task(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "真实候选人",
            "resume_text": "负责过增长项目，并用数据完成业务复盘。",
            "job_title": "业务运营经理",
            "source_job_code": "OPS-001",
            "jd_text": "负责业务增长、项目推进与跨团队协作。",
            "retention_days": 120,
            "screening_payload": {"source": "boss-recruiting-agent", "tier": "recommended"},
            "rounds": [
                {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": (start + timedelta(days=0)).isoformat(), "meeting_source": "offline"},
                {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=1)).isoformat(), "meeting_source": "feishu"},
                {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat(), "meeting_source": "offline"},
            ],
        }
        created = client.post("/api/v1/interview-tasks", json=payload)
        assert created.status_code == 201
        data = created.json()
        assert data["candidate"]["display_name"] == "真实候选人"
        assert data["job"]["title"] == "业务运营经理"
        assert [item["round_type"] for item in data["rounds"]] == ["business", "hr", "ceo"]
        assert all(item["plan_version"] == "plan-v1.1" for item in data["rounds"])
        assert all(len(item["plan_payload"]["required_questions"]) == 1 for item in data["rounds"])
        assert [item["plan_payload"]["question_mix"]["resume_jd_match"] for item in data["rounds"]] == [1, 1, 1]
        assert all(
            len({question["id"] for question in item["plan_payload"]["questions"]})
            == len(item["plan_payload"]["questions"])
            for item in data["rounds"]
        )
        assert data["active_interview_id"] == data["rounds"][0]["id"]

        second_payload = {
            **payload,
            "candidate_name": "同岗位第二位候选人",
            "resume_text": "另一份已经通过筛选的简历。",
            "rounds": [
                {**item, "scheduled_at": (start + timedelta(days=10 + index)).isoformat()}
                for index, item in enumerate(payload["rounds"])
            ],
        }
        second = client.post("/api/v1/interview-tasks", json=second_payload)
        assert second.status_code == 201
        assert second.json()["job"]["id"] == data["job"]["id"]


def test_logout_returns_empty_204_response(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204
        assert response.content == b""


def test_interview_task_accepts_position_specific_round_order(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "候选人",
            "resume_text": "简历正文",
            "job_title": "岗位",
            "jd_text": "岗位要求",
            "rounds": [
                {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": start.isoformat()},
                {"round_type": "business", "interviewer_names": ["业务"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
            ],
        }
        response = client.post("/api/v1/interview-tasks", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert [item["round_type"] for item in data["rounds"]] == ["hr", "business", "ceo"]
        assert data["routing"]["round_order"] == ["hr", "business", "ceo"]
        assert data["routing"]["round_count"] == 3
        assert data["active_interview_id"] == data["rounds"][0]["id"]
        review = client.get(f"/api/v1/admin/applications/{data['task_id']}/final-review").json()
        assert review["current_stage"] == "hr_interview"
        assert review["readiness"]["configured_round_order"] == ["hr", "business", "ceo"]


def test_interview_task_accepts_one_or_two_selected_rounds_and_rejects_duplicates(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        base = {
            "candidate_name": "灵活流程候选人",
            "resume_text": "简历正文",
            "job_title": "灵活流程岗位",
            "jd_text": "岗位要求",
        }
        one_round = client.post(
            "/api/v1/interview-tasks",
            json={
                **base,
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务"], "scheduled_at": start.isoformat()}
                ],
            },
        )
        assert one_round.status_code == 201
        assert one_round.json()["routing"]["round_order"] == ["business"]

        two_rounds = client.post(
            "/api/v1/interview-tasks",
            json={
                **base,
                "candidate_name": "两轮候选人",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=3)).isoformat()},
                ],
            },
        )
        assert two_rounds.status_code == 201
        assert two_rounds.json()["routing"]["round_order"] == ["business", "ceo"]

        duplicate = client.post(
            "/api/v1/interview-tasks",
            json={
                **base,
                "candidate_name": "重复流程候选人",
                "rounds": [
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=4)).isoformat()},
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=5)).isoformat()},
                ],
            },
        )
        assert duplicate.status_code == 422


def test_plain_text_document_can_be_imported(tmp_path):
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/document-text",
            headers={"x-filename": "resume.txt", "content-type": "application/octet-stream"},
            content="候选人有五年业务运营经验。".encode("utf-8"),
        )
        assert response.status_code == 200
        assert response.json()["text"] == "候选人有五年业务运营经验。"


def test_hr_can_create_a_new_job_from_jd_before_any_candidate_exists(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/admin/jobs",
            json={
                "title": "商业化运营经理",
                "source_job_code": "OPS-NEW-001",
                "status": "active",
                "jd_text": "负责商业化策略、客户成功和跨团队项目交付；建立经营指标并持续复盘增长结果。",
            },
        )
        assert created.status_code == 201
        result = created.json()
        job = result["job"]
        assert job["title"] == "商业化运营经理"
        assert job["status"] == "active"
        assert job["application_count"] == 0
        assert job["jd_character_count"] >= 20
        assert job["semantic_profile"]["version"] == "jd-semantic-v0.1"
        assert job["semantic_profile"]["analysis_mode"] == "local_structured_fallback"
        assert len(job["semantic_profile"]["interview_dimensions"]) == 7
        assert job["profile"]["state"] == "draft"
        assert result["talent_profile_draft"]["source_mode"] == "jd_baseline"
        assert result["talent_profile_draft"]["status"] == "draft"

        listed = client.get("/api/v1/admin/jobs")
        assert listed.status_code == 200
        assert next(item for item in listed.json() if item["id"] == job["id"])["jd_text"].startswith("负责商业化策略")
        center = client.get(f"/api/v1/admin/jobs/{job['id']}/talent-profile").json()
        assert center["active_version"] is None
        assert center["draft_version"]["version_label"] == "profile-v1"

        duplicate = client.post(
            "/api/v1/admin/jobs",
            json={
                "title": "另一岗位名称",
                "source_job_code": "OPS-NEW-001",
                "status": "active",
                "jd_text": "这是一份长度足够但岗位编号重复的岗位说明，不应创建第二份岗位数据。",
            },
        )
        assert duplicate.status_code == 409


def test_jd_revision_refreshes_only_future_interviews_and_can_be_activated(tmp_path):
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/v1/admin/jobs",
            json={
                "title": "经营分析经理",
                "source_job_code": "BA-2026-01",
                "status": "active",
                "jd_text": "负责业务增长与数据分析，推动跨部门项目交付，并对关键转化指标持续复盘。",
            },
        ).json()
        job = created["job"]
        draft = created["talent_profile_draft"]
        assert client.post(
            f"/api/v1/admin/jobs/{job['id']}/talent-profile/versions/{draft['id']}/activate",
            json={"confirmed_by_hr": True},
        ).status_code == 200

        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        task = client.post(
            "/api/v1/interview-tasks",
            json={
                "job_id": job["id"],
                "candidate_name": "新岗位候选人",
                "resume_text": "有经营分析、项目交付和跨部门合作经历。",
                "job_title": job["title"],
                "source_job_code": job["source_job_code"],
                "jd_text": job["jd_text"],
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
                ],
            },
        )
        assert task.status_code == 201
        task_data = task.json()
        business_id = task_data["active_interview_id"]
        future_id = next(item["id"] for item in task_data["rounds"] if item["round_type"] == "hr")
        before_business = client.get(f"/api/v1/interviews/{business_id}").json()["interview"]["plan_payload"]
        assert "业务增长" in before_business["preparation_context"]["job_role_mission"]
        acknowledge_and_start(client, business_id)

        updated = client.put(
            f"/api/v1/admin/jobs/{job['id']}",
            json={
                "title": job["title"],
                "source_job_code": job["source_job_code"],
                "status": "active",
                "jd_text": "负责成本控制、风险管理和团队管理，建立经营预警机制并推动组织级改进。",
            },
        )
        assert updated.status_code == 200
        revision = updated.json()
        assert revision["refreshed_interviews"] == 2
        assert revision["frozen_in_progress"] == 1
        assert revision["talent_profile_draft"]["source_mode"] == "jd_revision"

        frozen = client.get(f"/api/v1/interviews/{business_id}").json()["interview"]["plan_payload"]
        future = client.get(f"/api/v1/interviews/{future_id}").json()["interview"]["plan_payload"]
        assert "业务增长" in frozen["preparation_context"]["job_role_mission"]
        assert "成本控制" in future["preparation_context"]["job_role_mission"]

        revision_draft = revision["talent_profile_draft"]
        activated = client.post(
            f"/api/v1/admin/jobs/{job['id']}/talent-profile/versions/{revision_draft['id']}/activate",
            json={"confirmed_by_hr": True},
        )
        assert activated.status_code == 200
        assert activated.json()["source_mode"] == "jd_revision"


def test_hr_can_batch_recognize_and_import_interview_candidates(tmp_path):
    with make_client(tmp_path) as client:
        batch = client.post(
            "/api/v1/admin/resume-imports",
            json={"job_title": "业务运营经理", "jd_text": "负责业务增长与跨团队协作"},
        )
        assert batch.status_code == 201
        batch_id = batch.json()["id"]

        resume = """姓名：张三
手机号：13800138000
邮箱：zhangsan@example.com
工作年限：5年
学历：本科
当前公司：示例科技
当前职位：业务运营
现居地：上海
负责增长项目与跨团队协作。"""
        uploaded = client.post(
            f"/api/v1/admin/resume-imports/{batch_id}/items",
            headers={"x-filename": quote("张三_简历.txt"), "content-type": "application/octet-stream"},
            content=resume.encode("utf-8"),
        )
        assert uploaded.status_code == 201
        item = uploaded.json()
        assert item["recognized"]["fields"]["name"] == "张三"
        assert item["recognized"]["fields"]["phone"] == "13800138000"
        assert item["recognized"]["decision"] is None
        assert "不进行筛选" in item["recognized"]["boundary"]

        committed = client.post(
            f"/api/v1/admin/resume-imports/{batch_id}/commit",
            json={"item_ids": [item["id"]], "retention_days": 120},
        )
        assert committed.status_code == 200
        assert committed.json()["created"][0]["name"] == "张三"
        assert committed.json()["next_step"] == "schedule_interviews"

        tasks = client.get("/api/v1/admin/interview-tasks").json()
        imported = next(task for task in tasks if task["candidate"]["display_name"] == "张三")
        assert imported["current_stage"] == "interview_to_schedule"
        assert imported["rounds"] == []

        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        schedule_payload = {
            "application_id": imported["task_id"],
            "candidate_name": "张三",
            "resume_text": "简历已批量导入",
            "job_title": "业务运营经理",
            "jd_text": "负责业务增长与跨团队协作",
            "rounds": [
                {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
            ],
        }
        scheduled = client.post("/api/v1/interview-tasks", json=schedule_payload)
        assert scheduled.status_code == 201
        assert scheduled.json()["task_id"] == imported["task_id"]
        assert [item["round_type"] for item in scheduled.json()["rounds"]] == ["business", "hr", "ceo"]
        business_detail = client.get(f"/api/v1/interviews/{scheduled.json()['rounds'][0]['id']}").json()
        plan = business_detail["interview"]["plan_payload"]
        assert plan["version"] == "plan-v1.1"
        assert plan["preparation_context"]["personalization_status"] == "ready"
        assert "业务增长" in plan["preparation_context"]["job_role_mission"]
        personalized = [question for question in plan["questions"] if question["source"] == "resume_jd_match"]
        assert {question["source"] for question in personalized} == {"resume_jd_match"}
        assert all(question["required"] is False for question in personalized)
        assert all(question["source_evidence"].startswith("简历原文：") for question in personalized)
        assert plan["question_mix"] == {
            "required": 1,
            "resume_jd_match": 1,
            "resume_personalized": 0,
            "prior_round": 0,
        }


def test_duplicate_resume_is_only_a_reminder_and_can_still_be_imported(tmp_path):
    with make_client(tmp_path) as client:
        first_batch = client.post("/api/v1/admin/resume-imports", json={"job_title": "岗位A"}).json()
        content = "姓名：李四\n手机：13900139000\n学历：本科".encode("utf-8")
        first_item = client.post(
            f"/api/v1/admin/resume-imports/{first_batch['id']}/items",
            headers={"x-filename": quote("李四.txt")}, content=content,
        ).json()
        assert client.post(
            f"/api/v1/admin/resume-imports/{first_batch['id']}/commit",
            json={"item_ids": [first_item["id"]]},
        ).status_code == 200

        second_batch = client.post("/api/v1/admin/resume-imports", json={"job_title": "岗位B"}).json()
        second_item = client.post(
            f"/api/v1/admin/resume-imports/{second_batch['id']}/items",
            headers={"x-filename": quote("李四新简历.txt")}, content=(content + b"\nupdated"),
        )
        assert second_item.status_code == 201
        data = second_item.json()
        assert data["duplicate_candidate_id"]
        assert any("重复建档" in warning for warning in data["recognized"]["warnings"])
        assert client.post(
            f"/api/v1/admin/resume-imports/{second_batch['id']}/commit",
            json={"item_ids": [data["id"]]},
        ).status_code == 200


def test_production_api_requires_signed_login_session(tmp_path):
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'production.db'}",
        settings=Settings(
            environment="production",
            session_secret="test-production-secret-with-enough-entropy",
            recording_dir=tmp_path / "recordings",
        ),
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/status").status_code == 200
        assert client.get("/api/v1/me").status_code == 401
        assert client.get("/api/v1/admin/interview-tasks").status_code == 401
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 404


def test_development_roles_filter_assigned_today_interviews(tmp_path):
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'development.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"})
        assert login.status_code == 200
        start = datetime.now().replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "权限测试候选人",
            "resume_text": "有业务运营经验",
            "job_title": "运营经理",
            "jd_text": "负责增长与协作",
            "rounds": [
                {"round_type": "business", "interviewer_names": ["王经理"], "interviewer_open_ids": ["dev-business"], "scheduled_at": start.isoformat()},
                {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (start + timedelta(hours=1)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (start + timedelta(hours=2)).isoformat()},
            ],
        }
        assert client.post("/api/v1/interview-tasks", json=payload).status_code == 201
        client.post("/api/v1/auth/logout")
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-business"}).status_code == 200
        agenda = client.get("/api/v1/me/interviews/today")
        assert agenda.status_code == 200
        assert [item["round_type"] for item in agenda.json()] == ["business"]
        actions = client.get("/api/v1/me/action-center")
        assert actions.status_code == 200
        assert [item["round_type"] for item in actions.json()["items"]] == ["business"]
        assert client.get("/api/v1/admin/interview-tasks").status_code == 403
        assert client.get("/api/v1/admin/action-center").status_code == 403


def test_hr_today_workspace_only_lists_rounds_assigned_to_that_hr(tmp_path):
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'hr-assignment.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        start = datetime.now().replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "HR 入口测试候选人",
            "resume_text": "有完整工作经历",
            "job_title": "运营经理",
            "jd_text": "负责增长与协作",
            "rounds": [
                {"round_type": "business", "interviewer_names": ["王经理"], "interviewer_open_ids": ["dev-business"], "scheduled_at": start.isoformat()},
                {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (start + timedelta(minutes=1)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (start + timedelta(minutes=2)).isoformat()},
            ],
        }
        assert client.post("/api/v1/interview-tasks", json=payload).status_code == 201
        agenda = client.get("/api/v1/me/interviews/today")
        assert agenda.status_code == 200
        matching = [item for item in agenda.json() if item["candidate"]["display_name"] == "HR 入口测试候选人"]
        assert [item["round_type"] for item in matching] == ["hr"]
        assert matching[0]["routing"]["question_bank_version"] == "hr-standard-v0.1"


def test_hr_workspace_never_routes_to_business_even_if_misassigned(tmp_path):
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'hr-misassignment.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        start = datetime.now().replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "错误分配测试候选人", "resume_text": "简历", "job_title": "岗位", "jd_text": "要求",
            "rounds": [
                {"round_type": "business", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": start.isoformat()},
                {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (start + timedelta(minutes=1)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (start + timedelta(minutes=2)).isoformat()},
            ],
        }
        assert client.post("/api/v1/interview-tasks", json=payload).status_code == 201
        matching = [item for item in client.get("/api/v1/me/interviews/today").json() if item["candidate"]["display_name"] == "错误分配测试候选人"]
        assert [item["round_type"] for item in matching] == ["hr"]


def test_locked_management_report_is_visible_to_assigned_interviewer_only_without_hr_appendix(tmp_path):
    from app.config import Settings

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'report-access.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        today = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "报告权限候选人",
                "resume_text": "有业务、协作和复盘经验。",
                "job_title": "报告权限岗位",
                "jd_text": "负责经营与组织协作。",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["王经理"], "interviewer_open_ids": ["dev-business"], "scheduled_at": today.isoformat()},
                    {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (today + timedelta(minutes=30)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (today + timedelta(minutes=60)).isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        for round_item in created.json()["rounds"]:
            acknowledge_and_start(client, round_item["id"])
            assert client.post(f"/api/v1/interviews/{round_item['id']}/end").status_code == 200
            assert client.post(
                f"/api/v1/interviews/{round_item['id']}/scorecard/submit",
                json={"submitted_by": "面试官", "decision": "hold", "summary_notes": "等待三轮汇总。", "scores": []},
            ).status_code == 200
        draft = client.post(
            f"/api/v1/admin/applications/{created.json()['task_id']}/reports/draft"
        ).json()
        assert client.post(
            f"/api/v1/admin/reports/{draft['id']}/lock",
            json={"confirmed_by_hr": True},
        ).status_code == 200

        client.post("/api/v1/auth/logout")
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-ceo"}).status_code == 200
        today_agenda = client.get("/api/v1/me/interviews/today")
        assert today_agenda.status_code == 200
        ceo_agenda = next(item for item in today_agenda.json() if item["round_type"] == "ceo")
        assert ceo_agenda["candidate_dossier"]["prior_round_count"] == 2
        assert ceo_agenda["candidate_dossier"]["management_report"]["id"] == draft["id"]
        assert ceo_agenda["candidate_dossier"]["management_report"]["version_label"] == "report-v1"
        assert ceo_agenda["candidate_dossier"]["management_report"]["view_path"] == f"/?report={draft['id']}"
        management = client.get(f"/api/v1/reports/{draft['id']}")
        assert management.status_code == 200
        assert management.json()["audience"] == "management"
        assert "hr_appendix" not in management.json()["content"]
        assert client.get(
            f"/api/v1/reports/{draft['id']}?audience=hr_archive"
        ).status_code == 403
        assert client.get(
            f"/api/v1/admin/applications/{created.json()['task_id']}/reports"
        ).status_code == 403


def test_notice_is_a_hard_start_gate(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]

        blocked = client.post(f"/api/v1/interviews/{interview_id}/start")
        assert blocked.status_code == 409
        assert "notice" in blocked.json()["detail"]

        false_ack = client.post(
            f"/api/v1/interviews/{interview_id}/notice",
            json={"acknowledged_by": "tester", "candidate_was_notified": False},
        )
        assert false_ack.status_code == 409

        acknowledge_and_start(client, interview_id)


def test_realtime_event_creates_traceable_evidence(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)

        with client.websocket_connect(f"/ws/interviews/{interview_id}/live") as socket:
            socket.send_json(
                {
                    "type": "transcript.final",
                    "payload": {
                        "speaker_role": "candidate",
                        "speaker_confidence": 1,
                        "start_ms": 0,
                        "end_ms": 5000,
                        "text": "我主动沟通并协调跨部门团队，最终推动项目按期交付。",
                        "is_final": True,
                    },
                }
            )
            message = socket.receive_json()

        assert message["type"] == "live.update"
        assert message["segment"]["text_raw"].startswith("我主动沟通")
        assert message["analysis"]["evidence"]
        assert all(item["segment_ids"] for item in message["analysis"]["evidence"])


def test_production_llm_adapter_adds_validated_suggestions_and_rejects_fabricated_quotes(tmp_path):
    from app.config import Settings

    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'llm.db'}", settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "只返回一个有效 JSON 对象" in body["messages"][0]["content"]
        prompt = json.loads(body["messages"][1]["content"])
        assert "关键词只能用于导航" in prompt["analysis_method"]
        assert prompt["questions"]
        latest = next(item for item in prompt["transcript"] if item["segment_id"] == prompt["latest_segment_id"])
        target_question = prompt["questions"][0]
        output = {
            "suggestions": [{
                "question_id": target_question["question_id"],
                "competency_id": target_question["competency_id"],
                "evidence_gap": "personal_action",
                "basis_segment_id": latest["segment_id"],
                "basis_quote": "根据数据调整执行顺序",
                "reason": "候选人提到按数据调整顺序，但决定优先级的判断规则尚不清楚",
                "question": "当数据和原计划冲突时，你依据哪条规则决定先调整执行顺序？",
                "priority": "high",
            }, {
                "question_id": target_question["question_id"],
                "competency_id": target_question["competency_id"],
                "evidence_gap": "result",
                "basis_segment_id": latest["segment_id"],
                "basis_quote": "模型虚构的候选人原话",
                "reason": "这条建议没有真实原话依据",
                "question": "这条无依据的建议不应出现。",
                "priority": "high",
            }],
            "evidence": [
                {
                    "segment_id": latest["segment_id"],
                    "competency_id": prompt["competencies"][0]["id"],
                    "quote": "这句话并没有出现在逐字稿里",
                    "direction": "support",
                    "strength": 0.9,
                    "explanation": "远程模型提议",
                }
            ],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})

    app.state.intelligence.client = httpx.Client(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["llm"]["status"] == "ready"
        assert capabilities["llm"]["model"] == "interview-model"
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        with client.websocket_connect(f"/ws/interviews/{interview_id}/live") as socket:
            socket.send_json(
                {
                    "type": "transcript.final",
                    "payload": {
                        "speaker_role": "candidate",
                        "speaker_confidence": 1,
                        "start_ms": 0,
                        "end_ms": 5000,
                        "text": "我负责整理每周风险，并根据数据调整执行顺序。",
                        "is_final": True,
                    },
                }
            )
            message = socket.receive_json()

        analysis = message["analysis"]
        assert analysis["provider"] == "openai-compatible:interview-model"
        assert analysis["mode"] == "production"
        assert analysis["suggestions"][0]["source"] == "llm_semantic_evidence_gap"
        assert analysis["suggestions"][0]["basis_quote"] == "根据数据调整执行顺序"
        assert analysis["suggestions"][0]["basis_quote"] in analysis["suggestions"][0]["question"]
        assert all(item.get("basis_quote") != "模型虚构的候选人原话" for item in analysis["suggestions"])
        assert all(item["quote"] != "这句话并没有出现在逐字稿里" for item in analysis["evidence"])
        assert analysis["model_assistance"]["automatic_decision"] is False


def test_production_plan_uses_model_to_design_resume_grounded_deep_question(tmp_path):
    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'semantic-plan.db'}", settings=settings)

    resume_quote = "CodeCLI 中设计三层记忆系统，区分短期上下文、摘要和长期项目记忆"

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        if "jd_text" in prompt:
            output = {}
        else:
            assert resume_quote in prompt["resume"]
            assert "AI 应用管培生" == prompt["job_title"]
            output = {
                "questions": [{
                    "dimension_id": "business-1",
                    "resume_quote": resume_quote,
                    "question": "三层记忆里，如果只能保留一层来降低幻觉，你会保留哪层？判断依据是什么？",
                    "follow_up": "什么现象会证明你的选择其实错了？",
                    "why_this_matters": "区分会罗列架构与能理解记忆机制、失败模式和取舍。",
                }]
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})

    app.state.intelligence.client = httpx.Client(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "AI 应届候选人",
                "resume_text": f"在校生\n项目经历\n{resume_quote}。",
                "job_title": "AI 应用管培生",
                "jd_text": "参与 AI Agent 工具搭建，把业务问题转化为可落地的 AI 方案。",
                "rounds": [{
                    "round_type": "business",
                    "interviewer_names": ["业务负责人"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        plan = created.json()["rounds"][0]["plan_payload"]
        assert plan["version"] == "plan-v1.1"
        assert plan["semantic_question_assistance"]["status"] == "active"
        question = next(item for item in plan["questions"] if item.get("generation_mode") == "llm_semantic")
        assert question["source_evidence"] == f"简历原文：{resume_quote}"
        assert "如果只能保留一层" in question["question"]
        assert not any(term in question["question"] for term in ("最后有什么结果", "你实际负责哪一部分"))


def test_production_plan_preserves_grounded_fallback_when_model_returns_no_questions(tmp_path):
    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'semantic-plan-fallback.db'}", settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_name = body.get("response_format", {}).get("json_schema", {}).get("name")
        output = {} if schema_name == "job_semantic_profile" else {"questions": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})

    app.state.intelligence.client = httpx.Client(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "有匹配经历的候选人",
                "resume_text": "项目经历：使用大模型开发校园 AI 助手，访谈用户并持续调整应用流程。",
                "job_title": "AI 应用管培生",
                "jd_text": "负责 AI 应用原型、用户需求分析和项目落地。",
                "rounds": [{
                    "round_type": "business",
                    "interviewer_names": ["业务负责人"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        plan = created.json()["rounds"][0]["plan_payload"]
        fallback = [item for item in plan["questions"] if item["source"] == "resume_jd_match"]
        assert fallback
        assert plan["semantic_question_assistance"]["status"] == "fallback_preserved"
        assert plan["semantic_question_assistance"]["model_question_count"] == 0
        assert plan["question_mix"]["resume_jd_match"] == len(fallback)
        assert all(item["source_evidence"].startswith("简历原文：") for item in fallback)


def test_opening_legacy_empty_semantic_plan_rebuilds_candidate_questions(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "待重建计划候选人",
                "resume_text": "项目经历：使用大模型开发 AI 助手，访谈用户并调整应用流程。",
                "job_title": "AI 应用管培生",
                "jd_text": "负责 AI 应用原型、用户需求分析和项目落地。",
                "rounds": [{
                    "round_type": "business",
                    "interviewer_names": ["业务负责人"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        ).json()
        interview_id = created["active_interview_id"]
        with client.app.state.database.session_factory() as db:
            interview = db.get(InterviewRound, interview_id)
            plan = dict(interview.plan_payload)
            required = list(plan["required_questions"])
            plan["questions"] = required
            plan["optional_questions"] = []
            plan["question_mix"] = {**plan["question_mix"], "resume_jd_match": 0}
            plan["semantic_question_assistance"] = {
                "status": "active",
                "provider": "llm_semantic",
                "error_code": None,
            }
            interview.plan_payload = plan
            db.commit()

        refreshed = client.get(f"/api/v1/interviews/{interview_id}")
        assert refreshed.status_code == 200
        rebuilt = refreshed.json()["interview"]["plan_payload"]
        assert rebuilt["question_mix"]["resume_jd_match"] >= 1
        assert len(rebuilt["questions"]) > len(rebuilt["required_questions"])


def test_answer_logic_review_requires_traceable_candidate_conflicts_and_rejects_lie_labels():
    candidate_one = TranscriptSegment(
        id="seg_candidate_1",
        interview_round_id="round_logic",
        speaker_role="candidate",
        start_ms=0,
        end_ms=2000,
        text_raw="这个项目里我主要协助整理用户需求，核心开发由团队同事完成。",
        is_final=True,
    )
    candidate_two = TranscriptSegment(
        id="seg_candidate_2",
        interview_round_id="round_logic",
        speaker_role="candidate",
        start_ms=2001,
        end_ms=4000,
        text_raw="这个项目从需求到核心开发都是我独立完成的。",
        is_final=True,
    )
    interviewer = TranscriptSegment(
        id="seg_interviewer",
        interview_round_id="round_logic",
        speaker_role="interviewer",
        start_ms=4001,
        end_ms=5000,
        text_raw="所以核心开发到底是谁完成的？",
        is_final=True,
    )
    proposed = {
        "sufficient_evidence": True,
        "logic_score": 2,
        "confidence": 0.86,
        "label": "本人贡献口径需要复核",
        "summary": "同一项目的核心开发责任出现两种不同表述。",
        "dimensions": [{
            "id": "ownership_consistency",
            "status": "needs_verification",
            "explanation": "本人贡献范围前后变化。",
            "segment_ids": [candidate_one.id, candidate_two.id],
        }],
        "consistency_flags": [
            {
                "flag_type": "ownership_shift",
                "severity": "high",
                "description": "对同一项目的核心开发责任前后口径不同。",
                "segment_ids": [candidate_one.id, candidate_two.id],
                "verification_question": "请按需求、设计、编码和交付分别说明由谁完成。",
            },
            {
                "flag_type": "factual_conflict",
                "severity": "high",
                "description": "候选人在撒谎。",
                "segment_ids": [candidate_two.id],
                "verification_question": "请解释。",
            },
            {
                "flag_type": "timeline_conflict",
                "severity": "medium",
                "description": "面试官的问题与回答不一致。",
                "segment_ids": [interviewer.id, candidate_two.id],
                "verification_question": "请重新说明时间线。",
            },
        ],
        "verification_questions": [],
    }

    review = OpenAICompatibleProvider._validated_answer_logic(
        proposed,
        [candidate_one, candidate_two, interviewer],
    )

    assert review is not None
    assert review["logic_score"] == 2
    assert len(review["consistency_flags"]) == 1
    assert review["consistency_flags"][0]["flag_type"] == "ownership_shift"
    assert len(review["consistency_flags"][0]["quotes"]) == 2
    assert "不能仅凭语音" in review["boundary"]
    assert all("撒谎" not in item["description"] for item in review["consistency_flags"])


def test_live_semantic_analysis_uses_company_behavior_rubric_without_keyword_dependency(tmp_path):
    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'llm-company-rubric.db'}", settings=settings)

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        company_competency = next(item for item in prompt["competencies"] if item["id"] == "company.ownership")
        assert company_competency["positive_evidence"]
        assert company_competency["risk_signals"]
        assert set(company_competency["score_anchors_reference_only"]) == {"1", "3", "5"}
        assert company_competency["keywords_navigation_only"]
        assert prompt["company_policy"]["profile_version"] == "company-profile-v1"
        assert prompt["company_policy"]["red_lines"]

        latest = next(item for item in prompt["transcript"] if item["segment_id"] == prompt["latest_segment_id"])
        company_question = next(item for item in prompt["questions"] if item["competency_id"] == "company.ownership")
        output = {
            "suggestions": [{
                "question_id": company_question["question_id"],
                "competency_id": "company.ownership",
                "evidence_gap": "reflection",
                "basis_segment_id": latest["segment_id"],
                "basis_quote": "把延期缩短了两周",
                "reason": "已经说明补救结果，但尚未说明偏差原因和后续改进",
                "question": "这次偏差的根因是什么？之后你把哪项改进固化了下来？",
                "priority": "high",
            }],
            "evidence": [{
                "segment_id": latest["segment_id"],
                "competency_id": "company.ownership",
                "quote": "每天公开进展",
                "direction": "support",
                "strength": 0.72,
                "explanation": "体现对进展透明和闭环的可观察行动，仍需面试官核对语境。",
            }],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}]})

    app.state.intelligence.client = httpx.Client(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        bootstrapped = bootstrap(client)
        center = client.get("/api/v1/admin/company-profile").json()
        saved = client.put(
            "/api/v1/admin/company-profile/draft",
            json={
                "company_name": "示例科技",
                "profile_purpose": center["editor_payload"]["profile_purpose"],
                "competencies": center["editor_payload"]["competencies"],
                "red_lines": center["editor_payload"]["red_lines"],
                "change_summary": "建立首版公司可观察行为标准。",
            },
        ).json()
        assert client.post(
            f"/api/v1/admin/company-profile/versions/{saved['id']}/activate",
            json={"confirmed_by_hr": True},
        ).status_code == 200

        interview_id = bootstrapped["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        answer = "项目出了偏差后，我先把可控环节列出来，重新排了顺序，并每天公开进展，最后把延期缩短了两周。"
        assert not any(keyword in answer for keyword in ["负责", "责任", "主动", "推动", "结果", "复盘"])
        with client.websocket_connect(f"/ws/interviews/{interview_id}/live") as socket:
            socket.send_json({
                "type": "transcript.final",
                "payload": {
                    "speaker_role": "candidate",
                    "speaker_confidence": 1,
                    "start_ms": 0,
                    "end_ms": 8000,
                    "text": answer,
                    "is_final": True,
                },
            })
            analysis = socket.receive_json()["analysis"]

        suggestion = analysis["suggestions"][0]
        assert suggestion["source"] == "llm_semantic_evidence_gap"
        assert suggestion["competency_id"] == "company.ownership"
        assert suggestion["evidence_gap"] == "reflection"
        assert suggestion["basis_quote"] == "把延期缩短了两周"
        assert next(item for item in analysis["coverage"] if item["competency_id"] == "company.ownership")["status"] == "mentioned"
        assert any(item["competency_id"] == "company.ownership" for item in analysis["evidence"])


def test_production_llm_failure_degrades_to_rules_without_breaking_live_interview(tmp_path):
    from app.config import Settings

    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'llm-fallback.db'}", settings=settings)
    app.state.intelligence.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "temporary"}))
    )
    with TestClient(app) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        statuses = []
        with client.websocket_connect(f"/ws/interviews/{interview_id}/live") as socket:
            for index in range(3):
                socket.send_json(
                    {
                        "type": "transcript.final",
                        "payload": {
                            "speaker_role": "candidate",
                            "speaker_confidence": 1,
                            "start_ms": index * 6000,
                            "end_ms": index * 6000 + 5000,
                            "text": f"第{index + 1}次回答：我主动沟通并协调跨部门团队，最终推动项目按期交付。",
                            "is_final": True,
                        },
                    }
                )
                message = socket.receive_json()
                statuses.append(message["analysis"]["model_assistance"]["status"])

        analysis = message["analysis"]
        assert statuses == ["recovering", "recovering", "degraded"]
        assert analysis["mode"] == "degraded"
        assert analysis["model_assistance"]["fallback_provider"] == "mock-rules-v0.1"
        assert analysis["model_assistance"]["error_code"] == "upstream_error"
        # Semantic analysis is unavailable, but the local evidence-linked
        # shallow-answer prompt remains usable instead of leaving the panel blank.
        assert analysis["suggestions"]
        assert analysis["coverage"]
        assert analysis["evidence"]


def test_streaming_fragments_are_joined_before_semantic_follow_up(tmp_path):
    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key="server-only-secret",
        llm_model="interview-model",
        recording_dir=tmp_path / "recordings",
    )
    captured_contexts = []

    def semantic_response(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        payload = json.loads(request_body["messages"][1]["content"])
        context = payload["current_answer_context"]
        captured_contexts.append(context)
        current_question = next(item for item in payload["questions"] if item["is_current"])
        content = {
            "suggestions": [
                {
                    "question_id": current_question["question_id"],
                    "competency_id": current_question["competency_id"],
                    "evidence_gap": "risk_clarification",
                    "basis_segment_id": context["source_segment_ids"][-1],
                    "basis_quote": context["text"],
                    "reason": "候选人说明了演示上线方式，但多人长期使用的关键边界仍未核实。",
                    "question": "如果改成多人长期使用，你会先解决权限、数据保存还是稳定性？为什么？",
                    "priority": "high",
                }
            ],
            "evidence": [],
            "transcript_corrections": [],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]},
        )

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'fragment-live.db'}",
        settings=settings,
    )
    app.state.intelligence.client = httpx.Client(transport=httpx.MockTransport(semantic_response))
    with TestClient(app) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 0, "end_ms": 1200, "text": "这个工具现在怎么上线的？", "is_final": True},
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 1201, "end_ms": 1800, "text": "我把这个工具部署", "is_final": True},
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 1801, "end_ms": 2500, "text": "用了内网穿透和免费域名", "is_final": True},
        )

        assert captured_contexts
        assert "我把这个工具部署" in captured_contexts[-1]["text"]
        assert "内网穿透和免费域名" in captured_contexts[-1]["text"]
        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        suggestion = next(item for item in live["suggestion_history"] if item["status"] == "active")
        assert suggestion["source"] == "llm_semantic_evidence_gap"
        assert suggestion["priority"] == "high"
        assert len(suggestion["evidence_segment_ids"]) == 2


def test_live_analysis_links_jd_question_and_generates_gap_specific_follow_up(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        interview_id = bootstrapped["active_interview_id"]
        hr_interview_id = next(item["id"] for item in bootstrapped["rounds"] if item["round_type"] == "hr")
        detail = client.get(f"/api/v1/interviews/{interview_id}").json()
        jd_question = next(
            item
            for item in detail["interview"]["plan_payload"]["questions"]
            if item["source"] == "resume_jd_match"
        )
        acknowledge_and_start(client, interview_id)

        asked = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 3000,
                "text": jd_question["question"],
                "is_final": True,
            },
        )
        assert asked.status_code == 201
        shallow = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 3001,
                "end_ms": 6000,
                "text": "我做过类似业务增长项目，最后也完成了。",
                "is_final": True,
            },
        )
        assert shallow.status_code == 201

        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        state = next(item for item in live["question_coverage"] if item["question_id"] == jd_question["id"])
        assert state["status"] == "shallow"
        assert shallow.json()["id"] in state["evidence_segment_ids"]
        targeted = next(item for item in live["suggestions"] if item.get("question_id") == jd_question["id"])
        assert targeted["answer_status"] == "shallow"
        assert targeted["source"] == "question_gap"
        assert "判断依据" in targeted["reason"]
        assert "结果验证" not in targeted["reason"]
        assert "衡量依据" not in targeted["reason"]
        assert targeted["basis_quote"] in shallow.json()["text_raw"]
        assert targeted["basis_quote"] in targeted["question"]

        deep = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 6001,
                "end_ms": 12000,
                    "text": "因为当时资源不足，我判断注册环节流失最大，所以先分析转化漏斗再协调产品调整流程，最终注册转化率从12%提升到19%。",
                "is_final": True,
            },
        )
        assert deep.status_code == 201
        completed = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        completed_state = next(item for item in completed["question_coverage"] if item["question_id"] == jd_question["id"])
        assert completed_state["status"] == "evidenced"
        assert not any(item.get("question_id") == jd_question["id"] for item in completed["suggestions"])
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200
        evidence = client.get(f"/api/v1/interviews/{interview_id}/evidence").json()
        domain_evidence = next(item for item in evidence if item["competency_id"] == "domain_expertise")
        confirmed = client.patch(
            f"/api/v1/evidence/{domain_evidence['id']}",
            json={"status": "confirmed", "reviewed_by": "业务负责人"},
        )
        assert confirmed.status_code == 200

        draft = client.post(f"/api/v1/interviews/{interview_id}/scorecard/draft")
        assert draft.status_code == 200
        scorecard = draft.json()
        assert scorecard["rubric_version"] == "five-level-v0.4"
        jd_assessments = scorecard["recommendation"]["jd_assessments"]
        assert next(item for item in jd_assessments if item["question_id"] == jd_question["id"])["status"] == "evidenced"
        assert scorecard["recommendation"]["question_evidence_summary"]["evidenced"] >= 1
        assert not any(
            item.get("source_question_id") == jd_question["id"]
            and item["source_type"] == "jd_gap"
            for item in scorecard["next_round_questions"]
        )
        domain_score = next(item for item in scorecard["ai_scores"] if item["competency_id"] == "domain_expertise")
        assert domain_evidence["id"] in domain_score["confirmed_evidence_ids"]

        unsupported_decision = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={"submitted_by": "业务负责人", "decision": "advance", "scores": []},
        )
        assert unsupported_decision.status_code == 422

        invalid_score = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={
                "submitted_by": "业务负责人",
                "decision": "advance",
                "scores": [{
                    "competency_id": "problem_solving",
                    "score": 4,
                    "evidence_ids": [domain_evidence["id"]],
                }],
            },
        )
        assert invalid_score.status_code == 422

        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={
                "submitted_by": "业务负责人",
                "decision": "supplementary_interview",
                "summary_notes": "业务增长案例已有证据，其他 JD 重点进入下一轮继续确认。",
                "scores": [{
                    "competency_id": "domain_expertise",
                    "score": 4,
                    "evidence_ids": [domain_evidence["id"]],
                }],
            },
        )
        assert submitted.status_code == 200
        saved = submitted.json()
        assert saved["status"] == "submitted"
        assert saved["final_scores"][0]["assessment"] == "human_confirmed"
        assert saved["recommendation"]["human_decision"]["candidate_stage_changed"] is False

        hr_detail = client.get(f"/api/v1/interviews/{hr_interview_id}")
        assert hr_detail.status_code == 200
        hr_plan = hr_detail.json()["interview"]["plan_payload"]
        assert hr_plan["prior_round_context"][0]["source_round_type"] == "business"
        assert hr_plan["prior_round_context"][0]["excluded_to_reduce_anchoring"] == ["numeric_scores", "recommendation"]
        assert len([item for item in hr_plan["questions"] if item["source"] == "prior_round"]) == 2

        final_review = client.get(f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-review")
        assert final_review.status_code == 200
        review = final_review.json()
        assert review["readiness"]["status"] == "not_ready"
        assert review["readiness"]["rounds_completed"] == 1
        assert review["readiness"]["scorecards_submitted"] == 1
        assert review["rounds"][0]["transcript_url"].endswith("/transcript.txt")
        assert review["competency_summary"][0]["evidence_count"] >= 1

        premature_offer = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-decision",
            json={
                "decision": "offer_approval",
                "decided_by": "招聘 HR",
                "notes": "三轮尚未完成，不能进入录用审批。",
                "confirmed_by_hr": True,
            },
        )
        assert premature_offer.status_code == 409
        missing_confirmation = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-decision",
            json={
                "decision": "supplementary_interview",
                "decided_by": "招聘 HR",
                "notes": "需要补充验证其他岗位重点。",
                "confirmed_by_hr": False,
            },
        )
        assert missing_confirmation.status_code == 422
        supplementary = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-decision",
            json={
                "decision": "supplementary_interview",
                "decided_by": "招聘 HR",
                "notes": "需要补充验证其他岗位重点。",
                "confirmed_by_hr": True,
            },
        )
        assert supplementary.status_code == 200
        assert supplementary.json()["current_stage"] == "supplementary_interview"


def test_fillers_and_candidate_confirmation_are_not_answers_or_evidence(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        detail = client.get(f"/api/v1/interviews/{interview_id}").json()
        question = detail["interview"]["plan_payload"]["questions"][0]
        acknowledge_and_start(client, interview_id)
        assert client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 1200,
                "text": question["question"],
                "is_final": True,
            },
        ).status_code == 201
        filler = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 1201,
                "end_ms": 2200,
                "text": "嗯，哦，好的，然后呢？",
                "is_final": True,
            },
        )
        assert filler.status_code == 201

        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        state = next(item for item in live["question_coverage"] if item["question_id"] == question["id"])
        assert state["status"] == "unanswered"
        assert filler.json()["id"] not in state["evidence_segment_ids"]
        assert live["suggestions"] == []
        assert client.get(f"/api/v1/interviews/{interview_id}/evidence").json() == []


def test_motivation_question_does_not_require_result_or_metric(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        assert client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 1600,
                "text": "你为什么想做这个岗位？",
                "is_final": True,
            },
        ).status_code == 201
        assert client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 1601,
                "end_ms": 4200,
                "text": "我一直觉得AI应用这个方向很有意思，也比较适合自己。",
                "is_final": True,
            },
        ).status_code == 201

        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        state = next(item for item in live["question_coverage"] if item["source"] == "interviewer_ad_hoc")
        assert state["status"] == "shallow"
        assert state["missing_dimensions"] == ["判断依据"]
        suggestion = next(item for item in live["suggestions"] if item["question_id"] == state["question_id"])
        assert suggestion["evidence_gap"] == "decision_basis"
        assert "结果" not in suggestion["reason"]
        assert "量化" not in suggestion["reason"]


def test_absence_or_filler_cannot_be_persisted_as_competency_evidence(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        segment = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 0,
                "end_ms": 2600,
                "text": "嗯，我今天来参加面试，很高兴认识大家。",
                "is_final": True,
            },
        ).json()
        settings = Settings(
            environment="test",
            llm_base_url="https://api.deepseek.com",
            llm_api_key="test-key",
            llm_model="deepseek-chat",
            recording_dir=tmp_path / "recordings",
        )
        provider = OpenAICompatibleProvider(settings)
        with client.app.state.database.session_factory() as db:
            interview = db.get(InterviewRound, interview_id)
            source = db.get(TranscriptSegment, segment["id"])
            provider._apply_conversation_assessments(
                db,
                interview,
                [{"id": "domain_expertise", "name": "专业能力"}],
                [source],
                [{
                    "competency_id": "domain_expertise",
                    "score": 1,
                    "confidence": 0.8,
                    "direction": "negative",
                    "rationale": "候选人未说明专业经历，证据不足，无法判断。",
                    "evidence_segment_ids": [source.id],
                    "limitations": "本段没有证据。",
                }],
                {"ai_scores": [], "recommendation": {}},
            )
            db.commit()
            saved = db.scalars(
                select(EvidenceItem).where(EvidenceItem.interview_round_id == interview_id)
            ).all()
            assert saved == []


def test_ad_hoc_questions_accumulate_without_forcing_conversation_backwards(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)

        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 0, "end_ms": 2000,
                  "text": "客户在现场突然发火时，你当时是怎么处理的？", "is_final": True},
        )
        first_answer = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 2001, "end_ms": 4000,
                  "text": "我就先解释了一下。", "is_final": True},
        )
        assert first_answer.status_code == 201
        first_live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        first_state = next(item for item in first_live["question_coverage"] if item["question_id"].startswith("adhoc:"))
        assert first_state["status"] == "shallow"
        first_prompt = next(item for item in first_live["suggestion_history"] if item.get("question_id") == first_state["question_id"])
        assert first_prompt["priority"] == "high"

        resolved = client.patch(
            f"/api/v1/interviews/{interview_id}/suggestions/{first_prompt['id']}",
            json={"status": "addressed"},
        )
        assert resolved.status_code == 200

        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 4001, "end_ms": 6000,
                  "text": "这份工作里你最难适应的排班是什么？", "is_final": True},
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 6001, "end_ms": 8000,
                  "text": "夜班会有一点难。", "is_final": True},
        )
        next_live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        old_prompt = next(item for item in next_live["suggestion_history"] if item["id"] == first_prompt["id"])
        assert old_prompt["status"] == "addressed"
        assert not any(item.get("question_id") == first_state["question_id"] for item in next_live["suggestions"])
        assert any(item["status"] == "active" for item in next_live["suggestion_history"])


def test_consecutive_interviewer_asr_fragments_form_one_logical_question(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        fragments = ["你刚才说这个工具", "已经通过内网穿透上线", "如果多人使用会先改什么"]
        for index, fragment in enumerate(fragments):
            client.post(
                f"/api/v1/interviews/{interview_id}/segments",
                json={
                    "speaker_role": "interviewer",
                    "start_ms": index * 700,
                    "end_ms": index * 700 + 650,
                    "text": fragment,
                    "is_final": True,
                },
            )
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 2200,
                "end_ms": 3300,
                "text": "我会先补登录权限和数据保存。",
                "is_final": True,
            },
        )
        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        ad_hoc = [item for item in live["question_coverage"] if item["question_id"].startswith("adhoc:")]
        assert len(ad_hoc) == 1
        assert all(fragment in ad_hoc[0]["question"] for fragment in fragments)
        assert ad_hoc[0]["answer_segment_count"] == 1


def test_same_cloud_speaker_id_can_switch_roles_between_turns(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)

        candidate = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "unknown",
                "provider_speaker_id": 0,
                "start_ms": 0,
                "end_ms": 3000,
                "text": "我负责上一份工作的客户服务，最终把投诉率降低了百分之十。",
                "is_final": True,
            },
        )
        assert candidate.status_code == 201
        interviewer = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "unknown",
                "provider_speaker_id": 0,
                "start_ms": 3001,
                "end_ms": 5000,
                "text": "你说的这个具体是什么？",
                "is_final": True,
            },
        )
        assert interviewer.status_code == 201

        segments = client.get(f"/api/v1/interviews/{interview_id}/segments").json()
        assert [item["speaker_role"] for item in segments[-2:]] == ["candidate", "interviewer"]
        assert all(item["speaker_confidence"] >= 0.72 for item in segments[-2:])


def test_existing_round_with_null_suggestion_history_is_backfilled(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'legacy.db'}"
    legacy_engine = create_engine(database_url)
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE transcript_segments (id TEXT, provider_speaker_id INTEGER)"))
        connection.execute(text("CREATE TABLE jobs (id TEXT PRIMARY KEY, semantic_profile JSON)"))
        connection.execute(text("CREATE TABLE interview_rounds (id TEXT PRIMARY KEY, suggestion_history JSON)"))
        connection.execute(
            text("INSERT INTO interview_rounds (id, suggestion_history) VALUES ('legacy-round', NULL)")
        )
        connection.execute(
            text("INSERT INTO jobs (id, semantic_profile) VALUES ('legacy-job', NULL)")
        )

    database = Database(database_url)
    database.create_all()
    with database.engine.connect() as connection:
        stored = connection.scalar(
            text("SELECT suggestion_history FROM interview_rounds WHERE id = 'legacy-round'")
        )
        job_profile = connection.scalar(
            text("SELECT semantic_profile FROM jobs WHERE id = 'legacy-job'")
        )
    assert stored == "[]"
    assert job_profile == "{}"


def test_repeated_shallow_answer_advances_to_a_different_follow_up(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 1800,
                "text": "客户当场不满意时，你是怎么处理的？",
                "is_final": True,
            },
        )
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 1801,
                "end_ms": 3300,
                "text": "我就先处理了一下。",
                "is_final": True,
            },
        )
        first_live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        first = next(item for item in first_live["suggestions"] if item.get("question_id", "").startswith("adhoc:"))

        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 3301,
                "end_ms": 4700,
                "text": "具体的数据我没有记下来。",
                "is_final": True,
            },
        )
        second_live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        second = next(item for item in second_live["suggestions"] if item.get("question_id") == first["question_id"])
        assert second["question"] != first["question"]
        assert second["evidence_gap"] == first["evidence_gap"]
        assert second["follow_up_stage"] == 1
        assert len([item for item in second_live["suggestion_history"] if item.get("question_id") == first["question_id"]]) == 1


def test_resume_question_is_simple_and_fact_first(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "口语题候选人",
                "resume_text": "当前公司：示例科技\n当前职位：客服专员\n负责客户接待与投诉处理。",
                "job_title": "客服专员",
                "jd_text": "负责接待客户、处理投诉并记录服务问题。",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        interview_id = created.json()["rounds"][0]["id"]
        plan = client.get(f"/api/v1/interviews/{interview_id}").json()["interview"]["plan_payload"]
        resume_question = next(item for item in plan["questions"] if item["source"] == "resume_jd_match")
        assert "负责客户接待与投诉处理" in resume_question["source_evidence"]
        assert "哪个判断最难" in resume_question["question"]
        assert not any(word in resume_question["question"] for word in ("证明", "胜任", "优先验证", "前三个月"))


def test_student_questions_start_from_matching_resume_experience_not_jd_only(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "应届候选人",
                "resume_text": "姓名：应届候选人\n在校生\n项目经历\n校园 AI 助手：使用大模型整理课程资料，并访谈同学改进使用流程。",
                "job_title": "AI 应用管培生",
                "jd_text": "负责 AI 应用原型、用户需求分析和项目落地。",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        plan = created.json()["rounds"][0]["plan_payload"]
        assert plan["preparation_context"]["candidate_stage"] == "student"
        assert len(plan["required_questions"]) == 1
        assert "课程、比赛、社团、实习或个人项目" in plan["required_questions"][0]["question"]
        matched = [item for item in plan["questions"] if item["source"] == "resume_jd_match"]
        assert matched
        assert all(item["source_evidence"].startswith("简历原文：") for item in matched)
        assert all("校园 AI 助手" in item["source_evidence"] for item in matched)
        assert all(not any(term in item["question"] for term in ("上一份工作", "过去一份工作", "工作经历")) for item in matched)


def test_plan_does_not_invent_jd_experience_when_resume_has_no_match(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "跨方向候选人",
                "resume_text": "姓名：跨方向候选人\n在校生\n社团经历\n组织校园篮球训练与比赛。",
                "job_title": "财务助理",
                "jd_text": "负责会计凭证、应收账款核对和月度结账。",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        plan = created.json()["rounds"][0]["plan_payload"]
        assert plan["question_mix"]["resume_jd_match"] == 0
        assert not any(item["source"] == "resume_jd_match" for item in plan["questions"])


def test_scorecard_still_recommends_when_required_questions_were_missed(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 1500,
                "text": "说说你之前处理客户问题的经历。",
                "is_final": True,
            },
        )
        answer = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 1501,
                "end_ms": 7000,
                "text": "当时客户投诉等待太久，因为交接记录不清楚，我先核对订单并联系仓库，随后重新说明处理时间，最终当天解决。复盘后我增加了交接清单，后来同类问题明显减少。",
                "is_final": True,
            },
        )
        assert answer.status_code == 201
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200

        scorecard = client.get(f"/api/v1/interviews/{interview_id}/scorecard").json()
        ai = scorecard["recommendation"]["ai_recommendation"]
        response_quality = scorecard["recommendation"]["response_quality"]
        answer_logic = scorecard["recommendation"]["answer_logic_review"]
        scope = scorecard["recommendation"]["evaluation_scope"]
        assert ai["decision"] == "supplementary_interview"
        assert ai["required_questions_asked"] == 0
        assert ai["required_questions_total"] > 0
        assert ai["interview_completeness_score"] < 5
        assert ai["overall_score"] is not None
        assert "不能作为候选人的负面证据" in ai["process_warning"]
        assert response_quality["score"] is not None
        assert answer.json()["id"] in response_quality["evidence_segment_ids"]
        assert "不推断智力" in response_quality["boundary"]
        assert "不能仅凭语音" in answer_logic["boundary"]
        assert "撒谎" in answer_logic["boundary"]
        assert scorecard["next_round_questions"]
        assert scope["round_type"] == "business"
        assert scope["planned_question_dependency"] is False
        assert any(item["source"] == "job_semantic_profile" for item in scope["dimensions"])


def test_each_round_has_distinct_role_and_job_evidence_scope(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        scopes = {}
        for round_item in bootstrapped["rounds"]:
            interview_id = round_item["id"]
            acknowledge_and_start(client, interview_id)
            assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200
            scorecard = client.get(f"/api/v1/interviews/{interview_id}/scorecard").json()
            scopes[round_item["round_type"]] = scorecard["recommendation"]["evaluation_scope"]

        assert scopes["business"]["round_label"] == "业务面"
        assert scopes["hr"]["round_label"] == "HR 面"
        assert scopes["ceo"]["round_label"] == "CEO 面"
        business_names = {item["competency_name"] for item in scopes["business"]["dimensions"]}
        hr_names = {item["competency_name"] for item in scopes["hr"]["dimensions"]}
        ceo_names = {item["competency_name"] for item in scopes["ceo"]["dimensions"]}
        assert "专业能力" in business_names
        assert "求职动机" in hr_names
        assert "战略理解" in ceo_names
        assert business_names != hr_names != ceo_names
        assert sum(item["source"] == "job_semantic_profile" for item in scopes["business"]["dimensions"]) == 3
        assert sum(item["source"] == "job_semantic_profile" for item in scopes["hr"]["dimensions"]) == 2
        assert sum(item["source"] == "job_semantic_profile" for item in scopes["ceo"]["dimensions"]) == 2


def test_semantic_final_review_scores_freeform_dialogue_without_planned_question(tmp_path):
    class StubSemanticProvider(OpenAICompatibleProvider):
        def _chat_json(self, *, schema_name, payload, **_kwargs):
            candidate_ids = [
                item["segment_id"]
                for item in payload.get("transcript", [])
                if item["speaker_role"] == "candidate"
            ]
            if schema_name == "full_conversation_competency_assessment":
                return {
                    "competency_assessments": [
                        {
                            "competency_id": "domain_expertise",
                            "score": 4,
                            "confidence": 0.82,
                            "direction": "support",
                            "rationale": "候选人能够从临场问题中说明核对顺序、个人动作和结果。",
                            "evidence_segment_ids": candidate_ids,
                            "limitations": "仍需人工核对业务复杂度。",
                        }
                    ]
                }
            return {
                "summary": "已分析面试官临场问题及候选人的回答，最终判断仍需人工确认。",
                "response_quality": {
                    "score": 4,
                    "label": "回答清楚",
                    "rationale": "回答包含顺序、动作与结果。",
                    "observed_dimensions": ["事实具体性", "表达结构"],
                    "evidence_segment_ids": candidate_ids,
                    "limitations": "仅反映本轮回答。",
                },
                "next_round_questions": [],
            }

    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "interviewer",
                "start_ms": 0,
                "end_ms": 1500,
                "text": "不用看题库，你说说桌面资料很乱时会先做什么？",
                "is_final": True,
            },
        )
        answer = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 1501,
                "end_ms": 5500,
                "text": "我先把三张表按日期排好，再逐行核对差异，第二天交给同事检查时没有发现遗漏。",
                "is_final": True,
            },
        ).json()

        settings = Settings(
            environment="test",
            llm_base_url="https://api.deepseek.com",
            llm_api_key="test-key",
            llm_model="deepseek-chat",
            recording_dir=tmp_path / "recordings",
        )
        provider = StubSemanticProvider(settings)
        with client.app.state.database.session_factory() as db:
            interview = db.get(InterviewRound, interview_id)
            application = db.get(Application, interview.application_id)
            job = db.get(Job, application.job_id)
            draft = provider.draft_scorecard(db, interview, job)

        domain = next(item for item in draft["ai_scores"] if item["competency_id"] == "domain_expertise")
        assert domain["score"] == 4
        assert domain["assessment"] == "full_conversation_semantic"
        assert domain["evidence_ids"]
        assert draft["recommendation"]["ai_recommendation"]["overall_score"] == 4
        assert draft["recommendation"]["ai_recommendation"]["planned_question_dependency"] is False
        assert draft["recommendation"]["conversation_assessment"]["status"] == "complete"
        assert answer["id"] in draft["recommendation"]["response_quality"]["evidence_segment_ids"]


def test_three_submitted_rounds_allow_hr_to_enter_offer_approval(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        answers = {
            "business": "我负责业务项目，分析问题后协调资源推动方案上线，最终转化率提升了12%。",
            "hr": "我选择这个岗位是因为长期发展机会，也曾主动沟通协调团队达成共识并完成目标。",
            "ceo": "我曾经判断错误，复盘后学习并调整方案，最终推动团队完成业务增长结果。",
        }
        for round_item in bootstrapped["rounds"]:
            interview_id = round_item["id"]
            acknowledge_and_start(client, interview_id)
            segment = client.post(
                f"/api/v1/interviews/{interview_id}/segments",
                json={
                    "speaker_role": "candidate",
                    "start_ms": 0,
                    "end_ms": 6000,
                    "text": answers[round_item["round_type"]],
                    "is_final": True,
                },
            )
            assert segment.status_code == 201
            assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200
            evidence = client.get(f"/api/v1/interviews/{interview_id}/evidence").json()
            assert evidence
            selected = evidence[0]
            assert client.patch(
                f"/api/v1/evidence/{selected['id']}",
                json={"status": "confirmed", "reviewed_by": "面试官"},
            ).status_code == 200
            draft = client.post(f"/api/v1/interviews/{interview_id}/scorecard/draft").json()
            score = next(
                item for item in draft["ai_scores"]
                if selected["id"] in item.get("confirmed_evidence_ids", [])
            )
            submitted = client.post(
                f"/api/v1/interviews/{interview_id}/scorecard/submit",
                json={
                    "submitted_by": "面试官",
                    "decision": "advance",
                    "summary_notes": "已根据逐字稿确认该能力项。",
                    "scores": [{
                        "competency_id": score["competency_id"],
                        "score": 4,
                        "evidence_ids": [selected["id"]],
                    }],
                },
            )
            assert submitted.status_code == 200

        review = client.get(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-review"
        ).json()
        assert review["readiness"]["status"] == "ready_for_hr_decision"
        assert review["readiness"]["rounds_completed"] == 3
        assert review["readiness"]["scorecards_submitted"] == 3
        assert len(review["competency_summary"]) == 3
        dialogue_review = review["cross_round_ai_assessment"]
        assert dialogue_review["overall_score"] is not None
        assert dialogue_review["rounds_assessed"] == 3
        assert dialogue_review["total_transcript_segments"] == 3
        assert {item["round_type"] for item in dialogue_review["rounds"]} == {
            "business", "hr", "ceo"
        }
        assert all(item["evaluation_scope"]["planned_question_dependency"] is False for item in dialogue_review["rounds"])

        first_draft = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/reports/draft"
        )
        assert first_draft.status_code == 201
        assert first_draft.json()["version_label"] == "report-v1"
        assert first_draft.json()["status"] == "draft"
        assert "hr_appendix" not in first_draft.json()["content"]
        first_report_id = first_draft.json()["id"]
        locked = client.post(
            f"/api/v1/admin/reports/{first_report_id}/lock",
            json={"confirmed_by_hr": True},
        )
        assert locked.status_code == 200
        assert locked.json()["status"] == "locked"
        assert locked.json()["share_path"] == f"/?report={first_report_id}"
        management_report = client.get(f"/api/v1/reports/{first_report_id}").json()
        assert "hr_appendix" not in management_report["content"]
        assert management_report["content"]["executive_summary"]["conclusion_label"] == "等待 HR 最终确认"
        hr_report = client.get(
            f"/api/v1/reports/{first_report_id}?audience=hr_archive"
        ).json()
        assert "hr_appendix" in hr_report["content"]
        management_print = client.get(f"/api/v1/reports/{first_report_id}/print")
        assert management_print.status_code == 200
        assert "打印 / 保存为 PDF" in management_print.text
        assert "面试官质量复盘" not in management_print.text
        hr_print = client.get(
            f"/api/v1/reports/{first_report_id}/print?audience=hr_archive"
        )
        assert "面试官质量复盘" in hr_print.text

        transcript = client.get(review["rounds"][0]["transcript_url"])
        assert transcript.status_code == 200
        assert "候选人" in transcript.text

        final_decision = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/final-decision",
            json={
                "decision": "offer_approval",
                "decided_by": "招聘 HR",
                "notes": "三轮人工评价均已提交，关键能力有已确认逐字稿证据。",
                "confirmed_by_hr": True,
            },
        )
        assert final_decision.status_code == 200
        result = final_decision.json()
        assert result["current_stage"] == "offer_approval"
        assert result["human_final_decision"] == "offer_approval"
        assert result["final_decision_details"]["decided_by"] == "招聘 HR"

        second_draft = client.post(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/reports/draft"
        )
        assert second_draft.status_code == 201
        assert second_draft.json()["version_label"] == "report-v2"
        assert second_draft.json()["content"]["executive_summary"]["conclusion_label"] == "进入录用审批"
        assert client.post(
            f"/api/v1/admin/reports/{second_draft.json()['id']}/lock",
            json={"confirmed_by_hr": True},
        ).status_code == 200
        versions = client.get(
            f"/api/v1/admin/applications/{bootstrapped['application_id']}/reports"
        ).json()["versions"]
        assert [(item["version_label"], item["status"]) for item in versions] == [
            ("report-v2", "locked"),
            ("report-v1", "superseded"),
        ]


def test_only_configured_rounds_are_required_for_final_review(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "两轮终审候选人",
                "resume_text": "有客户沟通和业务复盘经验。",
                "job_title": "两轮终审岗位",
                "jd_text": "负责客户沟通、业务执行与复盘。",
                "rounds": [
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": start.isoformat()},
                    {"round_type": "business", "interviewer_names": ["业务"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        data = created.json()
        for index, round_item in enumerate(data["rounds"]):
            acknowledge_and_start(client, round_item["id"])
            assert client.post(f"/api/v1/interviews/{round_item['id']}/end").status_code == 200
            submitted = client.post(
                f"/api/v1/interviews/{round_item['id']}/scorecard/submit",
                json={
                    "submitted_by": "面试官",
                    "decision": "hold",
                    "summary_notes": "当前证据有限，交由后续流程综合判断。",
                    "scores": [],
                },
            )
            assert submitted.status_code == 200
            review = client.get(
                f"/api/v1/admin/applications/{data['task_id']}/final-review"
            ).json()
            assert review["current_stage"] == (
                "business_interview" if index == 0 else "final_review"
            )

        review = client.get(
            f"/api/v1/admin/applications/{data['task_id']}/final-review"
        ).json()
        assert review["readiness"]["status"] == "ready_for_hr_decision"
        assert review["readiness"]["rounds_total"] == 2
        assert review["readiness"]["rounds_completed"] == 2
        assert review["readiness"]["scorecards_submitted"] == 2
        assert review["readiness"]["configured_round_order"] == ["hr", "business"]
        assert review["readiness"]["missing_steps"] == []

        final_decision = client.post(
            f"/api/v1/admin/applications/{data['task_id']}/final-decision",
            json={
                "decision": "offer_approval",
                "decided_by": "招聘 HR",
                "notes": "岗位配置的两轮人工评价均已提交。",
                "confirmed_by_hr": True,
            },
        )
        assert final_decision.status_code == 200


def test_role_action_centers_surface_feedback_and_hr_final_review(tmp_path):
    with make_client(tmp_path) as client:
        today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        payload = {
            "candidate_name": "行动中心候选人",
            "resume_text": "有业务增长、跨团队协作和复盘经验。",
            "job_title": "行动中心岗位",
            "jd_text": "负责业务增长、组织协作与经营复盘。",
            "rounds": [
                {"round_type": "business", "interviewer_names": ["王经理"], "interviewer_open_ids": ["dev-business"], "scheduled_at": today.isoformat()},
                {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (today + timedelta(minutes=30)).isoformat()},
                {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (today + timedelta(minutes=60)).isoformat()},
            ],
        }
        created = client.post("/api/v1/interview-tasks", json=payload)
        assert created.status_code == 201
        rounds = created.json()["rounds"]

        early_report = client.post(
            f"/api/v1/admin/applications/{created.json()['task_id']}/reports/draft"
        )
        assert early_report.status_code == 201
        blocked_lock = client.post(
            f"/api/v1/admin/reports/{early_report.json()['id']}/lock",
            json={"confirmed_by_hr": True},
        )
        assert blocked_lock.status_code == 409

        personal = client.get("/api/v1/me/action-center")
        assert personal.status_code == 200
        assert personal.json()["summary"]["today_interviews"] == 1
        assert [item["round_type"] for item in personal.json()["items"]] == ["hr"]

        hr_center = client.get("/api/v1/admin/action-center")
        assert hr_center.status_code == 200
        assert hr_center.json()["summary"]["today_interviews"] == 3
        assert hr_center.json()["summary"]["ready_for_decision"] == 0

        first_round_id = rounds[0]["id"]
        acknowledge_and_start(client, first_round_id)
        assert client.post(f"/api/v1/interviews/{first_round_id}/end").status_code == 200
        waiting = client.get("/api/v1/admin/action-center").json()
        assert waiting["summary"]["missing_scorecards"] == 1
        assert any(
            item["type"] == "missing_scorecard" and item["interview_id"] == first_round_id
            for item in waiting["items"]
        )

        for round_item in rounds:
            interview_id = round_item["id"]
            if interview_id != first_round_id:
                acknowledge_and_start(client, interview_id)
                assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200
            submitted = client.post(
                f"/api/v1/interviews/{interview_id}/scorecard/submit",
                json={
                    "submitted_by": "对应面试官",
                    "decision": "hold",
                    "summary_notes": "当前证据有限，保留给 HR 结合三轮材料判断。",
                    "scores": [],
                },
            )
            assert submitted.status_code == 200

        ready = client.get("/api/v1/admin/action-center").json()
        assert ready["summary"]["missing_scorecards"] == 0
        assert ready["summary"]["ready_for_decision"] == 1
        final_item = next(item for item in ready["items"] if item["type"] == "ready_for_decision")
        assert final_item["application_id"] == created.json()["task_id"]
        assert final_item["action"] == "open_final_review"
        assert "不自动提交评价" in ready["boundary"]


def test_feedback_todo_can_be_dismissed_without_deleting_scorecard(tmp_path):
    with make_client(tmp_path) as client:
        today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "待评价清理候选人",
                "resume_text": "做过招聘运营和面试流程优化。",
                "job_title": "招聘运营",
                "jd_text": "负责招聘流程、面试协同和数据复盘。",
                "rounds": [
                    {
                        "round_type": "hr",
                        "interviewer_names": ["开发环境 HR"],
                        "interviewer_open_ids": ["dev-hr"],
                        "scheduled_at": today.isoformat(),
                    }
                ],
            },
        ).json()
        interview_id = created["rounds"][0]["id"]
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 0,
                "end_ms": 3000,
                "text": "我做过招聘流程梳理，也协调过业务面试官。",
                "is_final": True,
            },
        )
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200

        before = client.get("/api/v1/me/action-center").json()
        todo = next(item for item in before["items"] if item["interview_id"] == interview_id)
        assert todo["type"] == "feedback_due"
        assert todo["ai_summary"]

        dismissed = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/dismiss",
            json={"dismissed_by": "开发环境 HR", "reason": "无需继续占用我的待办"},
        )
        assert dismissed.status_code == 200
        assert dismissed.json()["status"] == "dismissed"
        assert dismissed.json()["recommendation"]["todo_dismissal"]["data_deleted"] is False

        after = client.get("/api/v1/me/action-center").json()
        assert not any(item["interview_id"] == interview_id for item in after["items"])
        preserved = client.get(f"/api/v1/interviews/{interview_id}/scorecard")
        assert preserved.status_code == 200
        assert preserved.json()["status"] == "dismissed"


def test_scorecard_never_scores_without_evidence(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        segment = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 0,
                "end_ms": 4000,
                "text": "我选择这个岗位是因为希望继续做业务发展。",
                "is_final": True,
            },
        )
        assert segment.status_code == 201
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200

        response = client.post(f"/api/v1/interviews/{interview_id}/scorecard/draft")
        assert response.status_code == 200
        scorecard = response.json()
        assert scorecard["recommendation"]["decision"] == "insufficient_evidence"
        assert scorecard["recommendation"]["ai_recommendation"]["decision"] == "supplementary_interview"
        assert scorecard["recommendation"]["ai_recommendation"]["human_confirmation_required"] is True
        assert any(item["score"] is None for item in scorecard["ai_scores"])
        assert all(
            item["score"] is None or item["evidence_ids"]
            for item in scorecard["ai_scores"]
        )


def test_end_creates_scorecard_and_human_submission_creates_deidentified_knowledge(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "首页.md").write_text("# Test Vault\n", encoding="utf-8")
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        bootstrapped = bootstrap(client)
        interview_id = bootstrapped["active_interview_id"]
        candidate_name = client.get(f"/api/v1/interviews/{interview_id}").json()["candidate"]["display_name"]
        acknowledge_and_start(client, interview_id)

        ended = client.post(f"/api/v1/interviews/{interview_id}/end")
        assert ended.status_code == 200

        automatic_draft = client.get(f"/api/v1/interviews/{interview_id}/scorecard")
        assert automatic_draft.status_code == 200
        assert automatic_draft.json()["status"] == "ai_draft"
        assert client.get("/api/v1/knowledge/proposals").json() == []

        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={
                "submitted_by": "业务负责人",
                "decision": "hold",
                "summary_notes": "本轮时间不足，下一轮继续确认。",
                "scores": [],
            },
        )
        assert submitted.status_code == 200
        learning = submitted.json()["recommendation"]["knowledge_learning"]
        assert learning["status"] == "pending_hr_review"
        assert learning["pending_hr_review"] >= 1
        assert learning["created_this_submission"] == learning["proposal_count"]

        proposals = client.get("/api/v1/knowledge/proposals").json()
        assert len(proposals) == learning["proposal_count"]
        assert all(item["status"] == "pending" for item in proposals)
        assert all(item["payload"]["_auto_generated"] is True for item in proposals)
        assert all(item["payload"]["_occurrence_count"] == 1 for item in proposals)
        serialized = str(proposals)
        assert candidate_name not in serialized
        assert "resume_text" not in serialized
        assert "transcript" not in serialized
        assert "quote" not in serialized

        resubmitted = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={
                "submitted_by": "业务负责人",
                "decision": "hold",
                "summary_notes": "更新说明，但不应重复生成提案。",
                "scores": [],
            },
        )
        assert resubmitted.status_code == 200
        repeated_learning = resubmitted.json()["recommendation"]["knowledge_learning"]
        assert repeated_learning["created_this_submission"] == 0
        assert len(client.get("/api/v1/knowledge/proposals").json()) == len(proposals)

        approved = client.patch(
            f"/api/v1/knowledge/proposals/{proposals[0]['id']}",
            json={"decision": "approved", "reviewed_by": "招聘 HR"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "published"
        note = (vault / approved.json()["publication"]["relative_path"]).read_text(encoding="utf-8")
        assert candidate_name not in note
        assert "_source_round_ids" not in note
        assert "_fingerprint" not in note
        assert "transcript" not in note


def test_knowledge_change_stops_at_approval_boundary(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        created = client.post(
            "/api/v1/knowledge/proposals",
            json={
                "source_round_id": interview_id,
                "proposal_type": "question",
                "payload": {"question": "请说明你如何验证关键假设。"},
                "rationale": "多场面试中该能力缺少可引用证据",
            },
        )
        assert created.status_code == 201
        assert created.json()["status"] == "pending"

        reviewed = client.patch(
            f"/api/v1/knowledge/proposals/{created.json()['id']}",
            json={"decision": "approved", "reviewed_by": "knowledge-owner"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "approved_for_publish"
        assert reviewed.json()["status"] != "published"
        assert reviewed.json()["publication"]["status"] == "failed"
        assert "尚未配置" in reviewed.json()["publication"]["error_message"]


def test_approved_knowledge_is_published_to_obsidian_vault(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "首页.md").write_text("# Test Vault\n", encoding="utf-8")
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        created = client.post(
            "/api/v1/knowledge/proposals",
            json={
                "source_round_id": interview_id,
                "proposal_type": "question",
                "payload": {
                    "question": "请说明你如何验证关键假设。",
                    "competency_id": "problem_solving",
                    "required": True,
                },
                "rationale": "多场面试中该能力缺少可引用证据",
            },
        )

        reviewed = client.patch(
            f"/api/v1/knowledge/proposals/{created.json()['id']}",
            json={"decision": "approved", "reviewed_by": "knowledge-owner"},
        )

        assert reviewed.status_code == 200
        data = reviewed.json()
        assert data["status"] == "published"
        assert data["publication"]["status"] == "published"
        note_path = vault / data["publication"]["relative_path"]
        assert note_path.is_file()
        note = note_path.read_text(encoding="utf-8")
        assert "请说明你如何验证关键假设" in note
        assert "contains_pii: false" in note
        assert "resume_text" not in note
        release_note = vault / "90-知识版本发布记录" / f"{data['publication']['release_version']}.md"
        assert release_note.is_file()

        status_response = client.get("/api/v1/admin/knowledge/status")
        assert status_response.status_code == 200
        assert status_response.json()["vault"]["writable"] is True
        assert status_response.json()["counts"]["published"] == 1


def test_system_documentation_requires_hr_confirmation_and_syncs_idempotently(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "首页.md").write_text("# Test Vault\n", encoding="utf-8")
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        initial = client.get("/api/v1/admin/knowledge/system-docs")
        assert initial.status_code == 200
        status = initial.json()
        assert status["summary"] == {
            "total": 9,
            "synced": 0,
            "pending": 9,
            "in_sync": False,
        }
        assert status["policy"]["rag_scope"] == "excluded"
        assert status["policy"]["contains_candidate_data"] is False

        unconfirmed = client.post(
            "/api/v1/admin/knowledge/system-docs/sync",
            json={"confirmed_by_hr": False},
        )
        assert unconfirmed.status_code == 422
        assert not (vault / "70-系统使用与运维").exists()

        synchronized = client.post(
            "/api/v1/admin/knowledge/system-docs/sync",
            json={"confirmed_by_hr": True},
        )
        assert synchronized.status_code == 200
        result = synchronized.json()
        assert len(result["written"]) == 9
        assert result["unchanged"] == []
        assert result["system_docs"]["summary"]["in_sync"] is True
        for item in result["system_docs"]["items"]:
            note = (vault / item["target_path"]).read_text(encoding="utf-8")
            assert "type: system_documentation" in note
            assert "contains_pii: false" in note
            assert "rag_scope: excluded" in note
            assert "source: repository_managed" in note

        repeated = client.post(
            "/api/v1/admin/knowledge/system-docs/sync",
            json={"confirmed_by_hr": True},
        )
        assert repeated.status_code == 200
        assert repeated.json()["written"] == []
        assert len(repeated.json()["unchanged"]) == 9

        governance = client.get("/api/v1/admin/governance")
        assert governance.status_code == 200
        assert "system_docs.synced" in {
            event["action"] for event in governance.json()["audit_events"]
        }


def test_readiness_center_never_exposes_configured_secret_values(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    settings = Settings(
        environment="test",
        provider_mode="production",
        llm_api_key="llm-secret-must-not-leak",
        llm_model="test-model",
        recording_dir=tmp_path / "recordings",
        asr_provider="tencent",
        tencent_asr_app_id="123456",
        tencent_asr_secret_id="asr-id-must-not-leak",
        tencent_asr_secret_key="asr-key-must-not-leak",
        feishu_app_id="cli_test",
        feishu_app_secret="feishu-secret-must-not-leak",
        feishu_redirect_uri="https://example.com/api/v1/auth/feishu/callback",
        feishu_notifications_enabled=True,
        knowledge_vault_dir=vault,
    )
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'readiness.db'}",
        settings=settings,
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/readiness")
        assert response.status_code == 200
        data = response.json()
        assert data["viewer"] == {"role": "hr", "can_run_tests": False}
        assert data["policy"]["secret_values_returned"] is False
        assert {item["id"] for item in data["checks"]} >= {
            "database",
            "feishu_oauth",
            "realtime_asr",
            "llm",
            "knowledge_vault",
        }
        serialized = response.text
        for secret in (
            "llm-secret-must-not-leak",
            "asr-id-must-not-leak",
            "asr-key-must-not-leak",
            "feishu-secret-must-not-leak",
        ):
            assert secret not in serialized
        assert all(
            field["value"] is None
            for check in data["checks"]
            for field in check["configuration"]
        )

        forbidden = client.post("/api/v1/admin/readiness/checks/database/test")
        assert forbidden.status_code == 403


def test_development_admin_can_run_safe_readiness_tests_with_audit(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    settings = Settings(
        environment="development",
        recording_dir=tmp_path / "recordings",
        knowledge_vault_dir=vault,
    )
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'admin-readiness.db'}",
        settings=settings,
    )
    with TestClient(app) as client:
        logged_in = client.post(
            "/api/v1/auth/dev-login",
            json={"open_id": "dev-admin"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["user"]["role"] == "admin"

        center = client.get("/api/v1/admin/readiness")
        assert center.status_code == 200
        assert center.json()["viewer"] == {"role": "admin", "can_run_tests": True}
        assert next(
            item for item in center.json()["checks"] if item["id"] == "database"
        )["can_test"] is True

        database_test = client.post(
            "/api/v1/admin/readiness/checks/database/test"
        )
        assert database_test.status_code == 200
        assert database_test.json()["result"]["status"] == "passed"

        storage_test = client.post(
            "/api/v1/admin/readiness/checks/recording_storage/test"
        )
        assert storage_test.status_code == 200
        assert storage_test.json()["result"]["status"] == "passed"
        assert (tmp_path / "recordings").is_dir()
        assert not list((tmp_path / "recordings").glob(".readiness-*.tmp"))

        governance = client.get("/api/v1/admin/governance")
        assert governance.status_code == 200
        tested = [
            event
            for event in governance.json()["audit_events"]
            if event["action"] == "readiness.connection_tested"
        ]
        assert {event["resource_id"] for event in tested} >= {
            "database",
            "recording_storage",
        }


def test_sensitive_knowledge_payload_is_approved_but_not_published(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        created = client.post(
            "/api/v1/knowledge/proposals",
            json={
                "source_round_id": interview_id,
                "proposal_type": "question",
                "payload": {
                    "question": "联系候选人 13800138000 继续确认。",
                },
                "rationale": "错误示例，用于验证隐私阻断",
            },
        )

        reviewed = client.patch(
            f"/api/v1/knowledge/proposals/{created.json()['id']}",
            json={"decision": "approved", "reviewed_by": "knowledge-owner"},
        )

        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "approved_for_publish"
        assert reviewed.json()["publication"]["status"] == "failed"
        assert "手机号" in reviewed.json()["publication"]["error_message"]
        assert not list((vault / "40-已批准经验").rglob("*.md")) if (vault / "40-已批准经验").exists() else True


def test_talent_profile_versions_require_hr_activation_and_sample_threshold(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "首页.md").write_text("# Test Vault\n", encoding="utf-8")
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        bootstrapped = bootstrap(client)
        detail = client.get(f"/api/v1/interviews/{bootstrapped['active_interview_id']}").json()
        job_id = detail["job"]["id"]
        candidate_name = detail["candidate"]["display_name"]

        empty = client.get(f"/api/v1/admin/jobs/{job_id}/talent-profile")
        assert empty.status_code == 200
        assert empty.json()["active_version"] is None
        assert empty.json()["draft_version"] is None

        draft = client.post(f"/api/v1/admin/jobs/{job_id}/talent-profile/draft")
        assert draft.status_code == 200
        baseline = draft.json()
        assert baseline["version_label"] == "profile-v1"
        assert baseline["source_mode"] == "jd_baseline"
        assert baseline["status"] == "draft"
        assert len(baseline["profile_payload"]["must_have"]) == 7
        assert candidate_name not in str(baseline)
        assert "resume_text" not in str(baseline)

        unconfirmed = client.post(
            f"/api/v1/admin/jobs/{job_id}/talent-profile/versions/{baseline['id']}/activate",
            json={"confirmed_by_hr": False},
        )
        assert unconfirmed.status_code == 422

        activated = client.post(
            f"/api/v1/admin/jobs/{job_id}/talent-profile/versions/{baseline['id']}/activate",
            json={"confirmed_by_hr": True},
        )
        assert activated.status_code == 200
        active = activated.json()
        assert active["status"] == "active"
        assert active["publication"]["status"] == "published"
        note = (vault / active["publication"]["relative_path"]).read_text(encoding="utf-8")
        assert candidate_name not in note
        assert "contains_pii: false" in note
        assert "5 年互联网运营经验" not in note
        assert "resume_text" not in note

        next_draft = client.post(f"/api/v1/admin/jobs/{job_id}/talent-profile/draft")
        assert next_draft.status_code == 200
        outcome_version = next_draft.json()
        assert outcome_version["version_label"] == "profile-v2"
        assert outcome_version["source_mode"] == "outcome_aggregation"
        assert outcome_version["evidence_summary"]["threshold_met"] is False

        blocked = client.post(
            f"/api/v1/admin/jobs/{job_id}/talent-profile/versions/{outcome_version['id']}/activate",
            json={"confirmed_by_hr": True},
        )
        assert blocked.status_code == 409
        center = client.get(f"/api/v1/admin/jobs/{job_id}/talent-profile").json()
        assert center["active_version"]["version_label"] == "profile-v1"
        assert center["draft_version"]["version_label"] == "profile-v2"
        assert [item["status"] for item in center["versions"]] == ["draft", "active"]


def test_company_profile_is_versioned_and_inherited_by_interviews_and_jobs(tmp_path):
    vault = tmp_path / "Interview-Knowledge"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "首页.md").write_text("# Test Vault\n", encoding="utf-8")
    with make_client(tmp_path, knowledge_vault_dir=vault) as client:
        bootstrapped = bootstrap(client)
        initial = client.get("/api/v1/admin/company-profile")
        assert initial.status_code == 200
        center = initial.json()
        assert center["active_version"] is None
        assert center["draft_version"] is None
        assert len(center["editor_payload"]["competencies"]) == 5

        saved = client.put(
            "/api/v1/admin/company-profile/draft",
            json={
                "company_name": "示例科技",
                "profile_purpose": "为所有岗位提供统一、可观察并且可以回到面试证据的公司级人才标准。",
                "competencies": center["editor_payload"]["competencies"],
                "red_lines": center["editor_payload"]["red_lines"],
                "change_summary": "根据 HR 用人原则建立首版公司基础人才画像。",
            },
        )
        assert saved.status_code == 200
        draft = saved.json()
        assert draft["version_label"] == "company-profile-v1"
        assert draft["status"] == "draft"

        unconfirmed = client.post(
            f"/api/v1/admin/company-profile/versions/{draft['id']}/activate",
            json={"confirmed_by_hr": False},
        )
        assert unconfirmed.status_code == 422

        activated = client.post(
            f"/api/v1/admin/company-profile/versions/{draft['id']}/activate",
            json={"confirmed_by_hr": True},
        )
        assert activated.status_code == 200
        active = activated.json()
        assert active["status"] == "active"
        assert active["publication"]["status"] == "published"
        assert active["refreshed_interviews"] == 3
        assert active["refreshed_job_profiles"] == 1
        published = (vault / active["publication"]["relative_path"]).read_text(encoding="utf-8")
        assert "责任担当" in published
        assert "contains_pii: false" in published

        expected_company_questions = {"business": 1, "hr": 1, "ceo": 1}
        for round_item in bootstrapped["rounds"]:
            detail = client.get(f"/api/v1/interviews/{round_item['id']}").json()
            plan = detail["interview"]["plan_payload"]
            company_questions = [
                item for item in plan["questions"] if item["source"] == "company_standard"
            ]
            assert len(company_questions) == expected_company_questions[round_item["round_type"]]
            assert all(item["required"] is True for item in company_questions)
            assert plan["company_profile_version"] == "company-profile-v1"

        detail = client.get(f"/api/v1/interviews/{bootstrapped['active_interview_id']}").json()
        job_profile = client.get(
            f"/api/v1/admin/jobs/{detail['job']['id']}/talent-profile"
        ).json()
        assert job_profile["draft_version"]["profile_payload"]["company_foundation"]["version_label"] == "company-profile-v1"

        editor = client.get("/api/v1/admin/company-profile").json()["editor_payload"]
        revised = client.put(
            "/api/v1/admin/company-profile/draft",
            json={
                "company_name": "示例科技",
                "profile_purpose": editor["profile_purpose"] + " 每年复核一次。",
                "competencies": editor["competencies"],
                "red_lines": editor["red_lines"],
                "change_summary": "增加年度复核要求，其他行为标准保持不变。",
            },
        )
        assert revised.status_code == 200
        assert revised.json()["version_label"] == "company-profile-v2"
        final_center = client.get("/api/v1/admin/company-profile").json()
        assert final_center["active_version"]["version_label"] == "company-profile-v1"
        assert final_center["draft_version"]["version_label"] == "company-profile-v2"


def test_company_profile_activation_does_not_change_an_in_progress_interview_plan(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        interview_id = bootstrapped["active_interview_id"]
        before = client.get(f"/api/v1/interviews/{interview_id}").json()["interview"]["plan_payload"]
        assert before["company_profile_version"] is None
        acknowledge_and_start(client, interview_id)

        center = client.get("/api/v1/admin/company-profile").json()
        saved = client.put(
            "/api/v1/admin/company-profile/draft",
            json={
                "company_name": "示例科技",
                "profile_purpose": center["editor_payload"]["profile_purpose"],
                "competencies": center["editor_payload"]["competencies"],
                "red_lines": center["editor_payload"]["red_lines"],
                "change_summary": "验证进行中的面试不会临时切换评价标准。",
            },
        ).json()
        activated = client.post(
            f"/api/v1/admin/company-profile/versions/{saved['id']}/activate",
            json={"confirmed_by_hr": True},
        )
        assert activated.status_code == 200
        assert activated.json()["refreshed_interviews"] == 2
        assert activated.json()["frozen_in_progress"] == 1

        frozen = client.get(f"/api/v1/interviews/{interview_id}").json()["interview"]["plan_payload"]
        assert frozen["company_profile_version"] is None
        future_round = next(item for item in bootstrapped["rounds"] if item["id"] != interview_id)
        future = client.get(f"/api/v1/interviews/{future_round['id']}").json()["interview"]["plan_payload"]
        assert future["company_profile_version"] == "company-profile-v1"


def test_hr_can_preview_and_commit_deidentified_historical_samples(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        detail = client.get(f"/api/v1/interviews/{bootstrapped['active_interview_id']}").json()
        job_id = detail["job"]["id"]
        csv_content = (
            "姓名,最终结果,面试评价,专业能力,沟通表达,手机号,简历\n"
            "张三,录用,专业能力突出，沟通表达良好,5,4,13800138000,秘密简历甲\n"
            "李四,已入职,问题解决能力较强,4,3,13900139000,秘密简历乙\n"
            "王五,试用期通过,战略理解优秀,4,5,13700137000,秘密简历丙\n"
            "赵六,淘汰,专业能力不足,2,3,13600136000,秘密简历丁\n"
        ).encode("utf-8")

        preview = client.post(
            f"/api/v1/admin/historical-samples/preview?job_id={job_id}",
            content=csv_content,
            headers={"Content-Type": "application/octet-stream", "X-Filename": "beisen.csv"},
        )
        assert preview.status_code == 200
        data = preview.json()
        assert data["summary"]["total_rows"] == 4
        assert data["summary"]["eligible_rows"] == 3
        assert data["mapping"]["ignored_pii_columns"] == ["手机号", "简历"]
        serialized = preview.text
        for private_value in ("张三", "李四", "13800138000", "秘密简历甲", "专业能力突出"):
            assert private_value not in serialized
        assert data["items"][0]["display_ref"].endswith("张*")

        eligible = [item for item in data["items"] if item["eligible_for_profile"]]
        committed = client.post(
            "/api/v1/admin/historical-samples/commit",
            json={
                "job_id": job_id,
                "filename": data["filename"],
                "file_hash": data["file_hash"],
                "total_rows": data["summary"]["total_rows"],
                "samples": [
                    {
                        "row_number": item["row_number"],
                        "record_hash": item["record_hash"],
                        "outcome": item["outcome"],
                        "competency_signals": item["competency_signals"],
                        "quality_flags": item["quality_flags"],
                    }
                    for item in eligible
                ],
            },
        )
        assert committed.status_code == 201
        result = committed.json()
        assert result["imported_rows"] == 3
        assert result["skipped_duplicates"] == 0
        assert result["talent_profile_update"]["evidence_summary"]["threshold_met"] is True
        assert result["talent_profile_update"]["evidence_summary"]["historical_positive_samples"] == 3
        assert result["talent_profile_update"]["evidence_summary"]["performance_validated_samples"] == 1
        with client.app.state.database.session_factory() as db:
            stored_samples = db.query(HistoricalHiringSample).all()
            assert len(stored_samples) == 3
            persisted = str([
                {
                    "outcome": item.outcome,
                    "signals": item.competency_signals,
                    "flags": item.quality_flags,
                }
                for item in stored_samples
            ])
            for private_value in ("张三", "李四", "13800138000", "秘密简历甲", "专业能力突出"):
                assert private_value not in persisted

        duplicate = client.post(
            "/api/v1/admin/historical-samples/commit",
            json={
                "job_id": job_id,
                "filename": data["filename"],
                "file_hash": data["file_hash"],
                "total_rows": data["summary"]["total_rows"],
                "samples": [
                    {
                        "row_number": item["row_number"],
                        "record_hash": item["record_hash"],
                        "outcome": item["outcome"],
                        "competency_signals": item["competency_signals"],
                        "quality_flags": item["quality_flags"],
                    }
                    for item in eligible
                ],
            },
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["imported_rows"] == 0
        assert duplicate.json()["skipped_duplicates"] == 3

        center = client.get(f"/api/v1/admin/jobs/{job_id}/talent-profile").json()
        assert center["outcome_samples"]["eligible_offer_samples"] == 3
        assert center["draft_version"]["source_mode"] == "outcome_aggregation"
        assert "record_hash" not in str(center)
        template = client.get("/api/v1/admin/historical-samples/template.csv")
        assert template.status_code == 200
        assert "最终结果" in template.text


def _minimal_history_xlsx() -> bytes:
    output = BytesIO()
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="历史数据" sheetId="1" r:id="rId1"/></sheets></workbook>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>"""
    sheet = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>姓名</t></is></c><c r="B1" t="inlineStr"><is><t>最终结果</t></is></c><c r="C1" t="inlineStr"><is><t>专业能力</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>陈七</t></is></c><c r="B2" t="inlineStr"><is><t>已入职</t></is></c><c r="C2" t="inlineStr"><is><t>4</t></is></c></row>
</sheetData></worksheet>"""
    with ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return output.getvalue()


def test_historical_preview_supports_xlsx_without_storing_raw_values(tmp_path):
    with make_client(tmp_path) as client:
        bootstrapped = bootstrap(client)
        detail = client.get(f"/api/v1/interviews/{bootstrapped['active_interview_id']}").json()
        response = client.post(
            f"/api/v1/admin/historical-samples/preview?job_id={detail['job']['id']}",
            content=_minimal_history_xlsx(),
            headers={"Content-Type": "application/octet-stream", "X-Filename": quote("历史招聘.xlsx")},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["outcome"] == "hired"
        assert response.json()["items"][0]["competency_signals"][0]["competency_id"] == "domain_expertise"
        assert "陈七" not in response.text
        assert response.json()["items"][0]["display_ref"].endswith("陈*")


def pcm_tone(duration_ms: int = 100, sample_rate: int = 16000) -> bytes:
    frames = int(sample_rate * duration_ms / 1000)
    samples = [int(9000 * math.sin(2 * math.pi * 440 * index / sample_rate)) for index in range(frames)]
    return struct.pack(f"<{len(samples)}h", *samples)


def test_audio_socket_requires_an_in_progress_interview(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        with client.websocket_connect(f"/ws/interviews/{interview_id}/audio") as socket:
            socket.send_json(
                {
                    "type": "audio.start",
                    "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }
            )
            message = socket.receive_json()
        assert message["type"] == "error"
        assert message["code"] == "invalid_state"


def test_audio_socket_records_valid_mono_pcm_as_wav(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        pcm = pcm_tone()
        with client.websocket_connect(f"/ws/interviews/{interview_id}/audio") as socket:
            socket.send_json(
                {
                    "type": "audio.start",
                    "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
                }
            )
            ready = socket.receive_json()
            assert ready["type"] == "audio.ready"
            assert ready["pipeline"]["asr_status"] == "not_configured"
            socket.send_bytes(pcm)
            metrics = socket.receive_json()
            assert metrics["type"] == "audio.metrics"
            assert metrics["duration_ms"] == 100
            socket.send_json({"type": "audio.stop"})
            stopped = socket.receive_json()
            assert stopped["type"] == "audio.stopped"

        recordings = client.get(f"/api/v1/interviews/{interview_id}/recordings")
        assert recordings.status_code == 200
        recording = recordings.json()[0]
        assert recording["status"] == "completed"
        assert recording["byte_count"] == len(pcm)
        assert recording["duration_ms"] == 100
        assert recording["peak_level"] > 0

        wav_files = list((tmp_path / "recordings" / interview_id).glob("*.wav"))
        assert len(wav_files) == 1
        with wave.open(str(wav_files[0]), "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getframerate() == 16000
            assert wav_file.getsampwidth() == 2
            assert wav_file.getnframes() == len(pcm) // 2


def test_audio_bridge_builds_a_real_pipecat_input_frame():
    import asyncio

    from app.audio.bridge import AudioFrameBridge, pipecat_available

    captured = []

    async def consumer(frame):
        captured.append(frame)

    assert pipecat_available()
    pcm = pcm_tone(duration_ms=20)
    info = asyncio.run(AudioFrameBridge(consumer=consumer).push(pcm))
    assert info.backend == "pipecat-input-frame"
    assert len(captured) == 1
    frame = captured[0]
    assert frame.audio == pcm
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1
    assert frame.num_frames == len(pcm) // 2


def test_demo_uses_business_hr_ceo_order_and_routes_today_agenda(tmp_path):
    with make_client(tmp_path) as client:
        data = bootstrap(client)
        assert [item["round_type"] for item in data["rounds"]] == ["business", "hr", "ceo"]

        agenda = client.get("/api/v1/me/interviews/today?include_demo=true")
        assert agenda.status_code == 200
        assert [item["round_type"] for item in agenda.json()] == ["business", "hr", "ceo"]
        first = agenda.json()[0]
        assert first["routing"]["rule"] == "schedule -> application -> job -> round_type -> versioned plan"
        assert first["routing"]["question_bank_version"] == "business-standard-v0.1"

        for expected_round, round_item in zip(("business", "hr", "ceo"), data["rounds"]):
            detail = client.get(f"/api/v1/interviews/{round_item['id']}")
            assert detail.status_code == 200
            payload = detail.json()
            assert [item["round_type"] for item in payload["rounds"]] == ["business", "hr", "ceo"]
            assert payload["routing"]["round_type"] == expected_round
            assert payload["routing"]["question_bank_version"] == f"{expected_round}-standard-v0.1"
            assert all(question["id"].startswith(f"q-{expected_round}-") for question in payload["interview"]["plan_payload"]["questions"])


def test_resume_recognition_reads_current_role_from_work_history_layout():
    from app.services.resume_recognition import recognize_resume

    resume = """王小明
13800138002 | xiaoming@example.com
工作经历
2023.06 - 至今 未来科技有限公司 | 高级产品经理
负责产品战略、商业化与跨部门协作
2020.01 - 2023.05 示例网络有限公司 产品经理
"""
    result = recognize_resume(resume, "王小明简历.pdf")
    assert result["fields"]["current_company"] == "未来科技有限公司"
    assert result["fields"]["current_title"] == "高级产品经理"
    assert result["confidence"]["current_company"] >= 0.9
    assert result["recognition_version"] == "resume-rules-v0.2"


def test_resume_recognition_reads_split_line_current_role():
    from app.services.resume_recognition import recognize_resume

    resume = """姓名：赵敏
邮箱：zhaomin@example.com
工作经历
2022年3月—至今
星河咨询集团
人力资源业务伙伴
工作内容：负责组织发展与招聘体系建设
"""
    result = recognize_resume(resume, "赵敏.docx")
    assert result["fields"]["current_company"] == "星河咨询集团"
    assert result["fields"]["current_title"] == "人力资源业务伙伴"


def test_required_question_progress_and_interviewer_quality_review(tmp_path):
    with make_client(tmp_path) as client:
        data = bootstrap(client)
        interview_id = data["active_interview_id"]
        job_id = data["job_id"]

        progress = client.get(f"/api/v1/interviews/{interview_id}/questions/progress")
        assert progress.status_code == 200
        required = [item for item in progress.json()["items"] if item["required"]]
        assert len(required) == 1

        for item in required:
            updated = client.put(
                f"/api/v1/interviews/{interview_id}/questions/progress",
                json={
                    "question_id": item["question_id"],
                    "asked": True,
                    "asked_by": "业务面试官",
                },
            )
            assert updated.status_code == 200
        assert updated.json()["required_complete"] is True

        acknowledge_and_start(client, interview_id)
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200

        draft = client.get(f"/api/v1/interviews/{interview_id}/interviewer-review")
        assert draft.status_code == 200
        assert draft.json()["automated_metrics"]["required_question_coverage"] == 1
        assert draft.json()["status"] == "ai_draft"

        reviewed = client.post(
            f"/api/v1/interviews/{interview_id}/interviewer-review",
            json={
                "reviewed_by": "招聘负责人",
                "notes": "统一问题覆盖完整，后续加强追问。",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "reviewed"
        assert reviewed.json()["human_ratings"] == {}
        assert reviewed.json()["automated_metrics"]["ai_ratings"]["fairness"] == 5

        aggregate = client.get(f"/api/v1/jobs/{job_id}/interviewer-quality")
        assert aggregate.status_code == 200
        assert aggregate.json()["interview_count"] == 1
        assert aggregate.json()["average_required_question_coverage"] == 1

        overview = client.get("/api/v1/admin/interviewer-quality/overview")
        assert overview.status_code == 200
        quality = overview.json()
        assert quality["summary"]["completed_interviews"] == 1
        assert quality["summary"]["review_completion_rate"] == 1
        assert quality["interviewers"][0]["display_name"] == reviewed.json()["interviewer_names"][0]
        assert quality["interviewers"][0]["ai_rating_averages"]["fairness"] == 5
        assert quality["interviewers"][0]["risk_level"] == "insufficient_sample"
        assert quality["governance"]["minimum_sample"] == 3

        filtered = client.get(
            f"/api/v1/admin/interviewer-quality/overview?job_id={job_id}"
        )
        assert filtered.status_code == 200
        assert filtered.json()["summary"]["completed_interviews"] == 1

        bootstrap(client)
        consolidated = client.get("/api/v1/admin/interviewer-quality/overview")
        same_job_rows = [
            item for item in consolidated.json()["jobs"]
            if item["job_title"] == "演示岗位 · 业务运营"
        ]
        assert len(same_job_rows) == 1
        assert same_job_rows[0]["application_count"] == 2


def test_hr_governance_preview_cleanup_and_audit_preserve_structured_results(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "到期材料候选人",
                "resume_text": "已进入面试流程的候选人简历。",
                "job_title": "数据治理测试岗位",
                "jd_text": "负责业务分析与跨团队协作。",
                "retention_days": 90,
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["业务负责人"], "scheduled_at": start.isoformat()},
                    {"round_type": "hr", "interviewer_names": ["HR"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["CEO"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
                ],
            },
        )
        assert created.status_code == 201
        task = created.json()
        candidate_id = task["candidate"]["id"]
        application_id = task["task_id"]
        round_id = task["rounds"][0]["id"]
        recording_dir = client.app.state.settings.recording_dir
        recording_dir.mkdir(parents=True, exist_ok=True)
        recording_path = recording_dir / "expired.wav"
        recording_path.write_bytes(b"RIFF-sensitive-audio")

        with client.app.state.database.session_factory() as db:
            candidate = db.get(Candidate, candidate_id)
            candidate.retention_until = utc_now() - timedelta(days=1)
            segment = TranscriptSegment(
                id=new_id("seg"), interview_round_id=round_id, speaker_role="candidate",
                start_ms=0, end_ms=1000, text_raw="我把业务指标提升了百分之二十。", is_final=True,
            )
            evidence = EvidenceItem(
                id=new_id("evi"), interview_round_id=round_id, competency_id="business_impact",
                segment_ids=[segment.id], quote=segment.text_raw, direction="positive", strength=.8,
                explanation="候选人给出了量化结果。", human_status="confirmed",
            )
            db.add_all([
                segment,
                evidence,
                AudioRecording(
                    id=new_id("rec"), interview_round_id=round_id, storage_key="expired.wav",
                    byte_count=recording_path.stat().st_size, status="completed",
                    retention_until=utc_now() - timedelta(days=1),
                ),
                Scorecard(
                    id=new_id("sc"), interview_round_id=round_id,
                    ai_scores=[{"competency_id": "business_impact", "score": 4, "confirmed_evidence_ids": [evidence.id]}],
                    human_scores=[{"competency_id": "business_impact", "score": 4, "evidence_ids": [evidence.id]}],
                    final_scores=[{"competency_id": "business_impact", "score": 4, "evidence_ids": [evidence.id]}],
                    recommendation={"human_decision": {"decision": "advance", "summary_notes": "人工确认通过"}},
                    status="submitted",
                ),
            ])
            db.commit()

        report = client.post(f"/api/v1/admin/applications/{application_id}/reports/draft")
        assert report.status_code == 201
        report_id = report.json()["id"]
        assert report.json()["content"]["key_evidence"][0]["quote"] == "我把业务指标提升了百分之二十。"

        preview = client.get("/api/v1/admin/governance")
        assert preview.status_code == 200
        due = next(item for item in preview.json()["items"] if item["candidate_id"] == candidate_id)
        assert due["status"] == "expired"
        assert due["recording_count"] == 1
        assert due["transcript_segment_count"] == 1
        assert due["evidence_count"] == 1
        assert preview.json()["policy"]["automatic_cleanup_enabled"] is False

        blocked = client.post(
            "/api/v1/admin/governance/retention/execute",
            json={"candidate_ids": [candidate_id], "confirmed_by_hr": False},
        )
        assert blocked.status_code == 422
        assert recording_path.exists()

        cleaned = client.post(
            "/api/v1/admin/governance/retention/execute",
            json={"candidate_ids": [candidate_id], "confirmed_by_hr": True},
        )
        assert cleaned.status_code == 200
        result = cleaned.json()
        assert result["summary"]["recordings_deleted"] == 1
        assert result["summary"]["transcript_segments_deleted"] == 1
        assert result["summary"]["evidence_items_deleted"] == 1
        assert result["summary"]["scorecards_redacted"] == 1
        assert result["summary"]["reports_redacted"] == 1
        assert not recording_path.exists()
        assert client.get(f"/api/v1/interviews/{round_id}/transcript.txt").status_code == 410

        redacted_report = client.get(f"/api/v1/reports/{report_id}?audience=hr_archive")
        assert redacted_report.status_code == 200
        assert redacted_report.json()["content"]["key_evidence"] == []
        redacted_artifact = redacted_report.json()["content"]["hr_appendix"]["artifacts"][0]
        assert redacted_artifact["transcript_url"] is None
        assert redacted_artifact["recordings"] == []

        with client.app.state.database.session_factory() as db:
            assert db.scalar(select(TranscriptSegment).where(TranscriptSegment.interview_round_id == round_id)) is None
            assert db.scalar(select(EvidenceItem).where(EvidenceItem.interview_round_id == round_id)) is None
            assert db.scalar(select(AudioRecording).where(AudioRecording.interview_round_id == round_id)) is None
            scorecard = db.scalar(select(Scorecard).where(Scorecard.interview_round_id == round_id))
            assert scorecard.final_scores[0]["score"] == 4
            assert scorecard.final_scores[0]["evidence_ids"] == []
            assert scorecard.recommendation["human_decision"]["decision"] == "advance"
            stored_report = db.get(InterviewReportVersion, report_id)
            assert stored_report.snapshot_payload["management"]["key_evidence"] == []
            artifact = stored_report.snapshot_payload["hr_appendix"]["artifacts"][0]
            assert artifact["transcript_url"] is None
            assert artifact["recordings"] == []

        refreshed = result["governance"]
        item = next(item for item in refreshed["items"] if item["candidate_id"] == candidate_id)
        assert item["status"] == "cleaned"
        actions = {event["action"] for event in refreshed["audit_events"]}
        assert "retention.sensitive_artifacts_cleaned" in actions
        assert "retention.cleanup_executed" in actions


def test_final_decision_and_transcript_download_are_audited(tmp_path):
    with make_client(tmp_path) as client:
        data = bootstrap(client)
        interview_id = data["active_interview_id"]
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 0, "end_ms": 500, "text": "审计测试", "is_final": True},
        )
        assert client.get(f"/api/v1/interviews/{interview_id}/transcript.txt").status_code == 200
        decision = client.post(
            f"/api/v1/admin/applications/{data['application_id']}/final-decision",
            json={
                "decision": "hold",
                "decided_by": "招聘 HR",
                "notes": "等待补充材料后再作判断。",
                "confirmed_by_hr": True,
            },
        )
        assert decision.status_code == 200
        actions = {item["action"] for item in client.get("/api/v1/admin/governance").json()["audit_events"]}
        assert "transcript.downloaded" in actions
        assert "application.final_decision" in actions


def test_notification_queue_is_idempotent_and_does_not_send_without_configuration(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "通知测试候选人",
                "resume_text": "敏感简历原文不应进入通知。",
                "job_title": "通知测试岗位",
                "jd_text": "负责业务分析。",
                "rounds": [
                    {"round_type": "business", "interviewer_names": ["王经理"], "interviewer_open_ids": ["dev-business"], "scheduled_at": start.isoformat()},
                    {"round_type": "hr", "interviewer_names": ["开发环境 HR"], "interviewer_open_ids": ["dev-hr"], "scheduled_at": (start + timedelta(days=1)).isoformat()},
                    {"round_type": "ceo", "interviewer_names": ["陈总"], "interviewer_open_ids": ["dev-ceo"], "scheduled_at": (start + timedelta(days=2)).isoformat()},
                ],
            },
        )
        assert created.status_code == 201

        first_sync = client.post("/api/v1/admin/notifications/sync")
        assert first_sync.status_code == 200
        assert first_sync.json()["created"] == 6
        queue = first_sync.json()["queue"]
        assert queue["integration"]["status"] == "not_configured"
        assert queue["integration"]["automatic_sending"] is False
        assert {item["notification_type"] for item in queue["items"]} == {
            "interview_assigned", "interview_reminder"
        }
        assert all("敏感简历原文" not in item["message"] for item in queue["items"])
        assert all(item["action_path"].startswith("/?interview=") for item in queue["items"])

        second_sync = client.post("/api/v1/admin/notifications/sync")
        assert second_sync.status_code == 200
        assert second_sync.json()["created"] == 0
        assert second_sync.json()["queue"]["summary"]["total"] == 6

        first_round_id = created.json()["rounds"][0]["id"]
        changed_time = start + timedelta(hours=3)
        changed = client.patch(
            f"/api/v1/admin/interviews/{first_round_id}",
            json={"scheduled_at": changed_time.isoformat()},
        )
        assert changed.status_code == 200
        resynced = client.post("/api/v1/admin/notifications/sync").json()["queue"]
        updated_assignment = next(
            item for item in resynced["items"]
            if item["resource_id"] == first_round_id and item["notification_type"] == "interview_assigned"
        )
        assert changed_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") in updated_assignment["message"]
        assert resynced["summary"]["total"] == 6

        due_ids = [item["id"] for item in queue["items"] if item["is_due"]]
        blocked_confirmation = client.post(
            "/api/v1/admin/notifications/dispatch",
            json={"notification_ids": due_ids, "confirmed_by_hr": False},
        )
        assert blocked_confirmation.status_code == 422
        blocked_config = client.post(
            "/api/v1/admin/notifications/dispatch",
            json={"notification_ids": due_ids, "confirmed_by_hr": True},
        )
        assert blocked_config.status_code == 409
        assert client.get("/api/v1/admin/notifications").json()["summary"]["sent"] == 0


def test_feishu_notification_sender_uses_tenant_token_and_open_id():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            body = json.loads(request.content)
            assert body == {"app_id": "cli_test", "app_secret": "secret_test"}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200})
        assert request.url.path.endswith("/im/v1/messages")
        assert request.url.params["receive_id_type"] == "open_id"
        assert request.headers["Authorization"] == "Bearer tenant-token"
        body = json.loads(request.content)
        assert body["receive_id"] == "ou_interviewer"
        assert body["msg_type"] == "text"
        assert "Interview Copilot" in json.loads(body["content"])["text"]
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_test"}})

    settings = Settings(
        environment="test",
        feishu_app_id="cli_test",
        feishu_app_secret="secret_test",
        feishu_notifications_enabled=True,
    )
    sender = FeishuNotificationSender(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    first = sender.send_text(
        recipient_open_id="ou_interviewer",
        title="面试提醒",
        message="请查看今日面试。",
        action_url="https://interview.example.com/?interview=round_1",
    )
    second = sender.send_text(
        recipient_open_id="ou_interviewer",
        title="评价提醒",
        message="请补充评价。",
        action_url="https://interview.example.com/?interview=round_1",
    )
    assert first == second == "om_test"
    assert sum(request.url.path.endswith("/tenant_access_token/internal") for request in requests) == 1
    assert sum(request.url.path.endswith("/im/v1/messages") for request in requests) == 2


def test_conversation_mode_follows_real_questions_without_competency_scores(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "自由对话候选人",
                "resume_text": "有客户服务经验。",
                "job_title": "客户服务",
                "jd_text": "负责客户咨询与问题处理。",
                "rounds": [{
                    "round_type": "business",
                    "interview_mode": "conversation",
                    "interviewer_names": ["业务负责人"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        interview_id = created.json()["active_interview_id"]
        detail = client.get(f"/api/v1/interviews/{interview_id}").json()["interview"]
        assert detail["interview_mode"] == "conversation"
        assert detail["plan_payload"]["interview_mode"] == "conversation"
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 0, "end_ms": 1000, "text": "遇到客户投诉时你一般怎么办？", "is_final": True},
        )
        answer = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 1001, "end_ms": 2500, "text": "我会先跟客户解释一下。", "is_final": True},
        )
        assert answer.status_code == 201
        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert live["analysis_mode"] == "conversation"
        assert live["coverage"] == []
        assert live["suggestions"][0]["source_question_text"] == "遇到客户投诉时你一般怎么办？"
        assert client.post(f"/api/v1/interviews/{interview_id}/end").status_code == 200
        scorecard = client.get(f"/api/v1/interviews/{interview_id}/scorecard").json()
        assert scorecard["rubric_version"] == "conversation-review-v1.0"
        assert scorecard["ai_scores"] == []
        assert scorecard["recommendation"]["interview_mode"] == "conversation"
        assert scorecard["recommendation"]["ai_recommendation"]["overall_score"] is not None
        assert scorecard["recommendation"]["ai_recommendation"]["decision"] in {
            "advance", "supplementary_interview", "hold", "insufficient_evidence"
        }
        assert scorecard["recommendation"]["ai_recommendation"]["planned_question_dependency"] is False
        rejected_without_notes = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={"submitted_by": "业务负责人", "decision": "advance", "scores": []},
        )
        assert rejected_without_notes.status_code == 422
        submitted = client.post(
            f"/api/v1/interviews/{interview_id}/scorecard/submit",
            json={"submitted_by": "业务负责人", "decision": "advance", "summary_notes": "候选人的处理过程仍需下一轮核实。", "scores": []},
        )
        assert submitted.status_code == 200


def test_semantic_live_analysis_preserves_raw_asr_and_saves_high_confidence_correction(tmp_path):
    class TranscriptCorrectionProvider(OpenAICompatibleProvider):
        def _chat_json(self, *, payload, **_kwargs):
            latest_id = payload["latest_segment_id"]
            return {
                "suggestions": [],
                "evidence": [],
                "transcript_corrections": [{
                    "segment_id": latest_id,
                    "corrected_text": "我在九州通负责区域财务管理。",
                    "confidence": 0.96,
                    "reason": "简历与上下文均显示公司名称为九州通。",
                }],
            }

    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        settings = Settings(
            environment="test",
            llm_base_url="https://api.deepseek.com",
            llm_api_key="test-key",
            llm_model="deepseek-chat",
            recording_dir=tmp_path / "recordings",
        )
        client.app.state.intelligence = TranscriptCorrectionProvider(settings)
        acknowledge_and_start(client, interview_id)
        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 0, "end_ms": 900, "text": "请介绍上一家公司。", "is_final": True},
        )
        response = client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 901, "end_ms": 2200, "text": "我在九洲通负责区域财务管理。", "is_final": True},
        )
        assert response.status_code == 201
        corrected = response.json()
        assert corrected["text_raw"] == "我在九洲通负责区域财务管理。"
        assert corrected["text_corrected"] == "我在九州通负责区域财务管理。"


def test_follow_up_history_keeps_source_question_and_only_three_active(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        acknowledge_and_start(client, interview_id)
        for index in range(4):
            question = f"请讲讲第{index + 1}个实际场景，你当时怎么处理？"
            client.post(
                f"/api/v1/interviews/{interview_id}/segments",
                json={"speaker_role": "interviewer", "start_ms": index * 3000, "end_ms": index * 3000 + 1000, "text": question, "is_final": True},
            )
            client.post(
                f"/api/v1/interviews/{interview_id}/segments",
                json={"speaker_role": "candidate", "start_ms": index * 3000 + 1001, "end_ms": index * 3000 + 2000, "text": "我就简单处理了一下。", "is_final": True},
            )
        live = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert len([item for item in live["suggestion_history"] if item["status"] == "active"]) <= 3
        assert any(item["status"] == "deferred" for item in live["suggestion_history"])
        current = next(item for item in live["suggestion_history"] if item["status"] == "active" and item.get("source_question_text"))
        assert "实际场景" in current["source_question_text"]


def test_resume_import_and_unscheduled_candidate_can_be_deleted(tmp_path):
    with make_client(tmp_path) as client:
        batch = client.post(
            "/api/v1/admin/resume-imports",
            json={"job_title": "删除测试岗位", "jd_text": "负责客户服务与日常记录。"},
        ).json()
        uploaded = client.post(
            f"/api/v1/admin/resume-imports/{batch['id']}/items",
            headers={"x-filename": quote("错误简历.txt"), "content-type": "application/octet-stream"},
            content="姓名：错误候选人\n负责客户服务。".encode("utf-8"),
        ).json()
        assert client.delete(f"/api/v1/admin/resume-imports/{batch['id']}/items/{uploaded['id']}").status_code == 204
        assert client.get(f"/api/v1/admin/resume-imports/{batch['id']}").json()["items"] == []

        uploaded = client.post(
            f"/api/v1/admin/resume-imports/{batch['id']}/items",
            headers={"x-filename": quote("再次错误.txt"), "content-type": "application/octet-stream"},
            content="姓名：再次错误\n负责客户服务。".encode("utf-8"),
        ).json()
        committed = client.post(
            f"/api/v1/admin/resume-imports/{batch['id']}/commit",
            json={"item_ids": [uploaded["id"]], "retention_days": 120},
        ).json()
        application_id = committed["created"][0]["application_id"]
        assert client.delete(f"/api/v1/admin/applications/{application_id}?confirmed=true").status_code == 200
        assert all(item["task_id"] != application_id for item in client.get("/api/v1/admin/interview-tasks").json())


def test_hr_can_remove_unstarted_or_started_task_without_losing_history(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        payload = {
            "candidate_name": "待删除任务候选人",
            "resume_text": "尚未开始面试。",
            "job_title": "任务删除测试岗位",
            "jd_text": "负责日常业务执行与跨团队协作。",
            "rounds": [
                {
                    "round_type": "business",
                    "interviewer_names": ["业务负责人"],
                    "interviewer_open_ids": ["dev-business"],
                    "scheduled_at": start.isoformat(),
                }
            ],
        }
        created = client.post("/api/v1/interview-tasks", json=payload)
        assert created.status_code == 201
        application_id = created.json()["task_id"]
        round_id = created.json()["rounds"][0]["id"]
        task = next(
            item
            for item in client.get("/api/v1/admin/interview-tasks").json()
            if item["task_id"] == application_id
        )
        assert task["deletion"]["allowed"] is True
        assert task["deletion"]["mode"] == "hard_delete"
        deleted = client.delete(f"/api/v1/admin/applications/{application_id}?confirmed=true")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["mode"] == "hard_deleted"
        assert client.get(f"/api/v1/interviews/{round_id}").status_code == 404
        assert all(
            item["task_id"] != application_id
            for item in client.get("/api/v1/admin/interview-tasks").json()
        )

        started = client.post(
            "/api/v1/interview-tasks",
            json={
                **payload,
                "candidate_name": "已开始任务候选人",
                "rounds": [
                    {
                        **payload["rounds"][0],
                        "scheduled_at": (start + timedelta(hours=1)).isoformat(),
                    }
                ],
            },
        )
        assert started.status_code == 201
        started_id = started.json()["task_id"]
        started_round_id = started.json()["rounds"][0]["id"]
        acknowledge_and_start(client, started_round_id)
        segment = client.post(
            f"/api/v1/interviews/{started_round_id}/segments",
            json={
                "speaker_role": "candidate",
                "start_ms": 0,
                "end_ms": 1200,
                "text": "我完成过一次业务流程优化。",
                "is_final": True,
            },
        )
        assert segment.status_code == 201
        assert client.post(f"/api/v1/interviews/{started_round_id}/end").status_code == 200
        assert client.get(f"/api/v1/interviews/{started_round_id}/scorecard").status_code == 200
        removed = client.delete(f"/api/v1/admin/applications/{started_id}?confirmed=true")
        assert removed.status_code == 200
        assert removed.json()["mode"] == "archived"
        assert removed.json()["historical_data_preserved"] is True
        assert client.get(f"/api/v1/interviews/{started_round_id}").status_code == 200
        assert client.get(f"/api/v1/interviews/{started_round_id}/segments").json()[0]["text_raw"] == "我完成过一次业务流程优化。"
        assert client.get(f"/api/v1/interviews/{started_round_id}/scorecard").status_code == 200
        assert all(
            item["task_id"] != started_id
            for item in client.get("/api/v1/admin/interview-tasks").json()
        )


def test_interviewer_cannot_delete_hr_interview_task(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'development.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "权限测试任务候选人",
                "resume_text": "尚未开始面试。",
                "job_title": "权限测试岗位",
                "jd_text": "负责日常业务执行。",
                "rounds": [
                    {
                        "round_type": "hr",
                        "interviewer_names": ["开发环境 HR"],
                        "interviewer_open_ids": ["dev-hr"],
                        "scheduled_at": (
                            datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
                        ).isoformat(),
                    }
                ],
            },
        )
        assert created.status_code == 201
        application_id = created.json()["task_id"]
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-business"}).status_code == 200
        forbidden = client.delete(f"/api/v1/admin/applications/{application_id}?confirmed=true")
        assert forbidden.status_code == 403


def test_final_rejection_archives_task_from_recent_queue_but_preserves_history(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "不进入下一轮候选人",
                "resume_text": "有客户沟通与问题处理经验。",
                "job_title": "客户服务岗位",
                "jd_text": "负责客户咨询、问题处理和服务复盘。",
                "rounds": [{
                    "round_type": "business",
                    "interview_mode": "conversation",
                    "interviewer_names": ["业务负责人"],
                    "interviewer_open_ids": ["dev-business"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        application_id = created.json()["task_id"]
        round_id = created.json()["rounds"][0]["id"]
        assert any(item["task_id"] == application_id for item in client.get("/api/v1/admin/interview-tasks").json())

        acknowledge_and_start(client, round_id)
        for role, text_value, start_ms, end_ms in [
            ("interviewer", "请介绍一次你亲自处理的客户问题。", 0, 1200),
            ("candidate", "我先确认问题，再逐项解决并复盘。", 1201, 2500),
        ]:
            assert client.post(
                f"/api/v1/interviews/{round_id}/segments",
                json={
                    "speaker_role": role,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text_value,
                    "is_final": True,
                },
            ).status_code == 201
        assert client.post(f"/api/v1/interviews/{round_id}/end").status_code == 200
        submitted = client.post(
            f"/api/v1/interviews/{round_id}/scorecard/submit",
            json={
                "submitted_by": "招聘 HR",
                "decision": "reject",
                "summary_notes": "当前不进入下一轮，岗位匹配度不足。",
                "scores": [],
            },
        )
        assert submitted.status_code == 200

        review = client.post(
            f"/api/v1/admin/applications/{application_id}/final-decision",
            json={
                "decision": "reject",
                "decided_by": "招聘 HR",
                "notes": "明确不进入下一轮，关闭当前招聘流程。",
                "confirmed_by_hr": True,
            },
        )
        assert review.status_code == 200
        assert review.json()["current_stage"] == "closed_rejected"
        assert not any(
            item["task_id"] == application_id
            for item in client.get("/api/v1/admin/interview-tasks").json()
        )
        assert not any(
            item.get("application_id") == application_id
            for item in client.get("/api/v1/admin/action-center").json()["items"]
        )

        history = client.get(f"/api/v1/interviews/{round_id}")
        assert history.status_code == 200
        assert history.json()["application"]["archived_at"]
        assert len(client.get(f"/api/v1/interviews/{round_id}/segments").json()) == 2
        assert client.get(f"/api/v1/interviews/{round_id}/scorecard").status_code == 200


def test_cancelled_task_can_leave_recent_queue_without_deleting_round(tmp_path):
    with make_client(tmp_path) as client:
        start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "取消后归档候选人",
                "resume_text": "面试尚未开始。",
                "job_title": "取消测试岗位",
                "jd_text": "负责日常业务执行与团队沟通。",
                "rounds": [{
                    "round_type": "hr",
                    "interviewer_names": ["开发环境 HR"],
                    "interviewer_open_ids": ["dev-hr"],
                    "scheduled_at": start.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        application_id = created.json()["task_id"]
        round_id = created.json()["rounds"][0]["id"]
        assert client.post(f"/api/v1/admin/interviews/{round_id}/cancel").status_code == 200
        task = next(
            item
            for item in client.get("/api/v1/admin/interview-tasks").json()
            if item["task_id"] == application_id
        )
        assert task["deletion"]["mode"] == "archive"
        removed = client.delete(f"/api/v1/admin/applications/{application_id}?confirmed=true")
        assert removed.status_code == 200
        assert removed.json()["mode"] == "archived"
        assert client.get(f"/api/v1/interviews/{round_id}").status_code == 200


def test_future_assigned_interview_is_visible_in_seven_day_agenda(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'future-agenda.db'}",
        settings=Settings(environment="development", recording_dir=tmp_path / "recordings"),
    )
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        tomorrow = datetime.now().replace(microsecond=0) + timedelta(days=1)
        created = client.post(
            "/api/v1/interview-tasks",
            json={
                "candidate_name": "明日候选人",
                "resume_text": "有招聘运营经验。",
                "job_title": "招聘运营",
                "jd_text": "负责招聘运营和候选人沟通。",
                "rounds": [{
                    "round_type": "hr",
                    "interviewer_names": ["开发环境 HR"],
                    "interviewer_open_ids": ["dev-hr"],
                    "scheduled_at": tomorrow.isoformat(),
                }],
            },
        )
        assert created.status_code == 201
        assert not any(item["candidate"]["display_name"] == "明日候选人" for item in client.get("/api/v1/me/interviews/today").json())
        upcoming = client.get("/api/v1/me/interviews/today?days=7").json()
        assert any(item["candidate"]["display_name"] == "明日候选人" for item in upcoming)


def test_follow_up_suggestions_wait_for_start_and_candidate_answer(tmp_path):
    with make_client(tmp_path) as client:
        interview_id = bootstrap(client)["active_interview_id"]
        before_start = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert before_start["availability"] == "waiting_for_start"
        assert before_start["suggestions"] == []
        assert before_start["suggestion_history"] == []
        assert before_start["evidence_digest"]["key_evidence"] == []
        assert before_start["evidence_digest"]["unknowns"] == []

        acknowledge_and_start(client, interview_id)
        before_answer = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert before_answer["availability"] == "waiting_for_candidate_answer"
        assert before_answer["suggestions"] == []

        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "interviewer", "start_ms": 0, "end_ms": 1000, "text": "请讲一个你处理客户问题的例子。", "is_final": True},
        )
        still_waiting = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert still_waiting["suggestions"] == []

        client.post(
            f"/api/v1/interviews/{interview_id}/segments",
            json={"speaker_role": "candidate", "start_ms": 1001, "end_ms": 2000, "text": "我简单处理了一下。", "is_final": True},
        )
        after_answer = client.get(f"/api/v1/interviews/{interview_id}/live-state").json()
        assert after_answer["suggestions"]
        assert after_answer["suggestion_history"]
        assert "evidence_digest" in after_answer
        assert after_answer["evidence_digest"]["summary"]["unknown"] >= 1
        assert after_answer["evidence_digest"]["risks"] == []

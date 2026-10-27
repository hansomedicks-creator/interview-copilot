"""Task creation with real FK enforcement; also runnable on a disposable Postgres DB.

COPILOT_TASK_TEST_DATABASE_URL must name interview_copilot_test_* (never production).
Run directly with `python -m unittest discover -s tests -p test_task_creation_integrity.py`.
Only synthetic data and mock AI are used. The caller owns the disposable database.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app
from app.models import Application, Candidate, InterviewAssignment, InterviewRound, Job


class TaskCreationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        url = os.getenv("COPILOT_TASK_TEST_DATABASE_URL")
        if url:
            parsed = make_url(url)
            if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("interview_copilot_test_"):
                raise RuntimeError("Refusing a non-disposable PostgreSQL database")
        else:
            url = f"sqlite:///{Path(self.temp.name) / 'test.db'}"
        self.app = create_app(database_url=url, settings=Settings(
            environment="test", provider_mode="mock", asr_provider="disabled",
            pipecat_enabled=False, feishu_notifications_enabled=False,
            knowledge_vault_dir=None, recording_dir=Path(self.temp.name) / "recordings",
        ))
        self.database = self.app.state.database
        self.addCleanup(self.database.engine.dispose)
        if self.database.engine.dialect.name == "sqlite":
            @event.listens_for(self.database.engine, "connect")
            def enable_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
        self.client = self.enterContext(TestClient(self.app))
        with self.database.engine.connect() as connection:
            if self.database.engine.dialect.name == "sqlite":
                self.assertEqual(connection.scalar(text("PRAGMA foreign_keys")), 1)
        self.title = "Synthetic FK regression " + uuid4().hex

    def payload(self):
        return {
            "candidate_name": "Synthetic candidate", "resume_text": "Synthetic resume",
            "job_title": self.title, "jd_text": "Synthetic job requirements",
            "rounds": [{
                "round_type": "business", "interviewer_names": ["Test interviewer"],
                "interviewer_open_ids": ["dev-business"],
                "scheduled_at": datetime.now(timezone.utc).isoformat(),
                "meeting_source": "offline",
            }],
        }

    def counts(self):
        with self.database.session_factory() as session:
            return tuple(session.scalar(select(func.count()).select_from(model)) for model in
                         (Candidate, Job, Application, InterviewRound, InterviewAssignment))

    def check_created_and_open(self, payload):
        response = self.client.post("/api/v1/interview-tasks", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        round_id = task["active_interview_id"]
        with self.database.session_factory() as session:
            application = session.get(Application, task["task_id"])
            self.assertIsNotNone(session.get(Candidate, application.candidate_id))
            self.assertIsNotNone(session.get(Job, application.job_id))
            self.assertEqual(session.get(InterviewRound, round_id).application_id, application.id)
        self.assertEqual(self.client.get("/api/v1/me/interviews/today?days=7").status_code, 200)
        for suffix in ("", "/segments", "/live-state"):
            self.assertEqual(self.client.get(f"/api/v1/interviews/{round_id}{suffix}").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/interviews/{round_id}/notice", json={
            "acknowledged_by": "Synthetic tester", "candidate_was_notified": True,
        }).status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/interviews/{round_id}/start").status_code, 200)
        return task

    def test_new_candidate_and_new_job_can_create_and_enter_first_round(self):
        self.check_created_and_open(self.payload())

    def test_new_candidate_with_existing_job_can_create_and_enter_first_round(self):
        with self.database.session_factory() as session:
            job = Job(id="job_" + uuid4().hex, title=self.title, jd_text="Existing requirements", status="active")
            session.add(job)
            session.commit()
            job_id = job.id
        task = self.check_created_and_open({**self.payload(), "job_id": job_id})
        self.assertEqual(task["job"]["id"], job_id)

    def test_later_assignment_failure_rolls_back_all_task_records(self):
        before = self.counts()
        payload = self.payload()
        payload["rounds"].append({**payload["rounds"][0], "round_type": "hr",
                                  "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                                  "interviewer_open_ids": ["missing-user"]})
        response = self.client.post("/api/v1/interview-tasks", json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("interviewer identity is unknown or inactive", response.text)
        self.assertEqual(self.counts(), before)

    def test_later_plan_failure_rolls_back_all_task_records(self):
        from unittest.mock import patch
        import app.main as main
        original = main._build_interview_plan
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Synthetic second-round plan failure")
            return original(*args, **kwargs)

        before = self.counts()
        payload = self.payload()
        payload["rounds"].append({**payload["rounds"][0], "round_type": "hr",
                                  "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        with patch.object(main, "_build_interview_plan", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "Synthetic second-round plan failure"):
                self.client.post("/api/v1/interview-tasks", json=payload)
        self.assertEqual(calls, 2)
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()

from datetime import timedelta
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import ResumeDocument, utc_now
from test_api import make_client, bootstrap


def pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_resume_upload_preview_replacement_expiration(tmp_path):
    with make_client(tmp_path) as client:
        demo = bootstrap(client)
        interview_id = demo["rounds"][0]["id"]
        url = f"/api/v1/interviews/{interview_id}/resume-document"
        assert client.get(url).status_code == 404
        assert client.put(url, content=b"not pdf").status_code == 422
        assert client.put(url, content=pdf_bytes()).status_code == 200
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == pdf_bytes()
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "application/pdf"
        assert client.put(url, content=pdf_bytes()).status_code == 200
        with client.app.state.database.session_factory() as db:
            rows = list(db.scalars(select(ResumeDocument)))
            assert len(rows) == 1
            rows[0].expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
        assert client.get(url).status_code == 404


def test_resume_requires_login_assignment_and_hr_write(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'auth.db'}", settings=Settings(environment="development", recording_dir=tmp_path / "audio"))
    with TestClient(app) as client:
        assert client.get("/api/v1/interviews/no-id/resume-document").status_code == 401
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-hr"}).status_code == 200
        demo = bootstrap(client)
        url = f"/api/v1/interviews/{demo['rounds'][0]['id']}/resume-document"
        assert client.put(url, content=pdf_bytes()).status_code == 200
        assert client.post("/api/v1/auth/dev-login", json={"open_id": "dev-business"}).status_code == 200
        assert client.get("/api/v1/interviews/not-assigned/resume-document").status_code == 403
        assert client.put(url, content=pdf_bytes()).status_code == 403


def test_resume_rejects_oversized_body(tmp_path):
    with make_client(tmp_path) as client:
        demo = bootstrap(client)
        response = client.put(f"/api/v1/interviews/{demo['rounds'][0]['id']}/resume-document", content=b"x" * (10 * 1024 * 1024 + 1))
        assert response.status_code == 413


def test_batch_original_links_to_candidate_and_deleted_item_removes_original(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main._extract_document", lambda content, filename: "测试候选人\n姓名：李测试\n工作经历：负责客户工单")
    with make_client(tmp_path) as client:
        batch = client.post("/api/v1/admin/resume-imports", json={"job_title": "测试岗位", "jd_text": "处理客户工单"}).json()
        url = f"/api/v1/admin/resume-imports/{batch['id']}/items"
        item = client.post(url, content=pdf_bytes(), headers={"X-Filename": "resume.pdf"}).json()
        assert client.delete(f"{url}/{item['id']}").status_code == 204
        with client.app.state.database.session_factory() as db:
            assert not list(db.scalars(select(ResumeDocument)))
        item = client.post(url, content=pdf_bytes(), headers={"X-Filename": "resume.pdf"}).json()
        client.patch(f"{url}/{item['id']}", json={"name": "李测试"})
        result = client.post(f"/api/v1/admin/resume-imports/{batch['id']}/commit", json={"item_ids": [item['id']]} )
        assert result.status_code == 200
        with client.app.state.database.session_factory() as db:
            doc = db.scalar(select(ResumeDocument))
            assert doc.candidate_id == result.json()["created"][0]["candidate_id"]
            assert doc.content == pdf_bytes()

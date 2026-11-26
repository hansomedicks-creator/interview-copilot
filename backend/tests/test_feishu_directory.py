import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.providers.feishu_directory import FeishuDirectory, FeishuDirectoryError
from app.providers.feishu_notifications import FeishuNotificationSender
from app.models import UserIdentity
from test_api import make_client


def directory(handler):
    sender = FeishuNotificationSender(Settings(feishu_app_id="test-app", feishu_app_secret="test-secret"), httpx.Client(transport=httpx.MockTransport(handler)))
    return FeishuDirectory(sender)


def test_directory_paginates_deduplicates_and_minimizes_data():
    def handler(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "test-token"})
        if "children" in request.url.path:
            return httpx.Response(200, json={"data": {"items": [{"open_department_id": "od-a"}]}})
        assert request.url.params["user_id_type"] == "open_id"
        if request.url.params.get("page_token"):
            return httpx.Response(200, json={"data": {"items": [{"open_id": "ou-b", "name": "乙", "status": {"is_resigned": True}}]}})
        return httpx.Response(200, json={"data": {"items": [{"open_id": "ou-a", "name": "甲", "mobile": "private"}], "has_more": True, "page_token": "next"}})
    assert directory(handler).list_employees() == [
        {"open_id": "ou-a", "name": "甲", "unavailable": False},
        {"open_id": "ou-b", "name": "乙", "unavailable": True},
    ]


def test_directory_permission_failure_is_actionable_and_sanitized():
    def handler(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"tenant_access_token": "secret-token"})
        return httpx.Response(403, json={"code": 40004, "msg": "private diagnostic"})
    with pytest.raises(FeishuDirectoryError, match="全部成员") as error:
        directory(handler).list_employees()
    assert "private" not in str(error.value)
    assert "secret" not in str(error.value)


def test_directory_permission_denied_shows_missing_scope_and_stage_only():
    def handler(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(200, json={"tenant_access_token": "secret-token"})
        if "children" in request.url.path:
            return httpx.Response(200, json={"data": {"items": []}})
        return httpx.Response(403, json={"code": 99991672, "error": {
            "permission_violations": [{"subject": "contact:user.base:readonly"}, {"subject": "untrusted secret"}],
        }})
    with pytest.raises(FeishuDirectoryError, match="读取部门成员（99991672）") as error:
        directory(handler).list_employees()
    assert "contact:user.base:readonly" in str(error.value)
    assert "untrusted" not in str(error.value)


def test_directory_sync_preserves_disabled_accounts_and_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(FeishuDirectory, "list_employees", lambda self: [
        {"open_id": "ou-admin", "name": "管理员新名", "unavailable": False},
        {"open_id": "ou-disabled", "name": "已停用", "unavailable": False},
        {"open_id": "ou-new", "name": "新同事", "unavailable": False},
        {"open_id": "ou-left", "name": "离职", "unavailable": True},
    ])
    with make_client(tmp_path) as client:
        with client.app.state.database.session_factory() as db:
            db.add_all([UserIdentity(id="a", open_id="ou-admin", display_name="管理员", role="admin", active=True), UserIdentity(id="b", open_id="ou-disabled", display_name="停用", role="hr", active=False)])
            db.commit()
        response = client.post("/api/v1/admin/users/sync-feishu")
        assert response.status_code == 200
        assert response.json()["added"] == 1
        with client.app.state.database.session_factory() as db:
            users = {u.open_id: u for u in db.scalars(select(UserIdentity))}
            assert users["ou-admin"].role == "admin"
            assert not users["ou-disabled"].active
            assert users["ou-new"].role == "interviewer"
            assert "ou-left" not in users

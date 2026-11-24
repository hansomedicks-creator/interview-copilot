"""Read the configured app's tenant directory, never a second app's identities."""
from time import monotonic

import httpx

from .feishu_notifications import FeishuNotificationError, FeishuNotificationSender


class FeishuDirectoryError(RuntimeError):
    pass


class FeishuDirectory:
    def __init__(self, sender: FeishuNotificationSender):
        self.sender = sender

    def list_employees(self) -> list[dict]:
        if not (self.sender.settings.feishu_app_id and self.sender.settings.feishu_app_secret):
            raise FeishuDirectoryError("请先配置本公司的飞书应用 App ID 和 App Secret。")
        try:
            token = self.sender._get_tenant_token()
        except (FeishuNotificationError, httpx.HTTPError):
            raise FeishuDirectoryError("飞书应用认证失败，请管理员检查应用配置。") from None
        deadline = monotonic() + 50

        def pages(path, **params):
            seen = set()
            for _ in range(200):
                if monotonic() >= deadline:
                    raise FeishuDirectoryError("通讯录同步超时，本次未写入，请稍后重试。")
                try:
                    response = self.sender.client.get(
                        f"https://open.feishu.cn/open-apis/contact/v3/{path}",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"user_id_type": "open_id", "department_id_type": "open_department_id", "page_size": 50, **params},
                    )
                    data = response.json()
                    if response.is_error or data.get("code", 0):
                        code = data.get("code", response.status_code)
                        raise FeishuDirectoryError(
                            f"飞书通讯录读取失败（{code}）。请管理员确认通讯录读取、用户基本信息权限，以及数据范围为全部成员；发布应用后重试。本次未写入。"
                        )
                    page = data["data"]
                    yield from page.get("items", [])
                    if not page.get("has_more"):
                        return
                    cursor = page.get("page_token")
                    if not cursor or cursor in seen:
                        raise FeishuDirectoryError("飞书分页异常，本次未写入，请重试。")
                    seen.add(cursor)
                    params["page_token"] = cursor
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    raise FeishuDirectoryError("飞书通讯录连接或响应异常，本次未写入，请重试。") from None
            raise FeishuDirectoryError("通讯录超过单次同步上限，本次未写入。")

        departments = ["0"] + [
            item["open_department_id"]
            for item in pages("departments/0/children", fetch_child="true")
            if item.get("open_department_id") and not (item.get("status") or {}).get("is_deleted")
        ]
        employees = {}
        for department in dict.fromkeys(departments):
            for item in pages("users/find_by_department", department_id=department):
                if not item.get("open_id") or not item.get("name"):
                    raise FeishuDirectoryError("通讯录缺少姓名或用户标识，请管理员开启用户基本信息读取权限。本次未写入。")
                # Store only identity and employment availability, not phone, gender, etc.
                employees[item["open_id"]] = {
                    "open_id": item["open_id"], "name": item["name"],
                    "unavailable": any((item.get("status") or {}).get(key) for key in ("is_frozen", "is_resigned", "is_exited", "is_unjoin")),
                }
        return list(employees.values())

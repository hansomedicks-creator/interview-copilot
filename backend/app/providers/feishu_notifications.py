from __future__ import annotations

import json
from time import monotonic

import httpx

from ..config import Settings


class FeishuNotificationError(RuntimeError):
    pass


class FeishuNotificationSender:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=12)
        self._tenant_token: str | None = None
        self._tenant_token_expires_at = 0.0

    @property
    def ready(self) -> bool:
        return self.settings.feishu_notifications_configured

    def test_connection(self) -> None:
        """Validate app credentials without sending a message."""
        self._get_tenant_token()

    def send_text(
        self,
        *,
        recipient_open_id: str,
        title: str,
        message: str,
        action_url: str,
    ) -> str:
        if not self.ready:
            raise FeishuNotificationError("Feishu notifications are not configured")
        token = self._get_tenant_token()
        content = f"{title}\n\n{message}\n\n打开 Interview Copilot：{action_url}"
        response = self.client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": recipient_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": content}, ensure_ascii=False),
            },
        )
        data = _response_payload(response, "send message")
        message_id = ((data.get("data") or {}).get("message_id"))
        if not message_id:
            raise FeishuNotificationError("Feishu send message response did not include message_id")
        return str(message_id)

    def _get_tenant_token(self) -> str:
        if self._tenant_token and monotonic() < self._tenant_token_expires_at:
            return self._tenant_token
        response = self.client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
        )
        data = _response_payload(response, "obtain tenant access token")
        token = data.get("tenant_access_token")
        if not token:
            raise FeishuNotificationError("Feishu token response did not include tenant_access_token")
        expires_in = max(60, int(data.get("expire") or 7200))
        self._tenant_token = str(token)
        self._tenant_token_expires_at = monotonic() + expires_in - 60
        return self._tenant_token


def _response_payload(response: httpx.Response, operation: str) -> dict:
    try:
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise FeishuNotificationError(f"Feishu could not {operation}: {error}") from error
    if data.get("code", 0) != 0:
        raise FeishuNotificationError(
            f"Feishu could not {operation}: {data.get('msg') or data.get('code')}"
        )
    return data

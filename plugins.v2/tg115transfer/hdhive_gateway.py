"""Client for the self-hosted HDHive OAuth gateway."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

import requests


class HDHiveGatewayError(Exception):
    pass


class HDHiveGatewayClient:
    def __init__(
        self,
        service_url: str,
        installation_id: str,
        installation_key: str,
        timeout: int = 30,
        proxies: dict[str, str] | None = None,
    ):
        self.service_url = (service_url or "").strip().rstrip("/")
        self.installation_id = (installation_id or "").strip()
        self.installation_key = (installation_key or "").strip()
        self.timeout = max(5, min(int(timeout or 30), 120))
        self.proxies = proxies

    @property
    def configured(self) -> bool:
        return bool(self.service_url and self.installation_id and self.installation_key)

    def build_authorize_url(self, valid_seconds: int = 600) -> str:
        if not self.configured:
            return ""
        expires = int(time.time()) + max(60, min(valid_seconds, 900))
        payload = f"{self.installation_id}:{expires}".encode()
        signature = hmac.new(
            self.installation_key.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return f"{self.service_url}/oauth/start?{urlencode({'installation_id': self.installation_id, 'expires': expires, 'signature': signature})}"

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def query_resources(self, media_type: str, tmdb_id: int) -> list[dict[str, Any]]:
        data = self._request(
            "POST",
            "/v1/resources/query",
            {"media_type": media_type, "tmdb_id": int(tmdb_id)},
        )
        for key in ("items", "resources", "list"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        nested = data.get("data")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return []

    def resolve_resource(
        self,
        resource_url: str = "",
        slug: str = "",
        max_unlock_points: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"resource_url": resource_url, "slug": slug}
        if max_unlock_points is not None:
            body["max_unlock_points"] = int(max_unlock_points)
        return self._request("POST", "/v1/resources/resolve", body)

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.configured:
            raise HDHiveGatewayError("影巢OAuth服务尚未配置")
        try:
            response = requests.request(
                method,
                f"{self.service_url}{path}",
                headers={
                    "X-Installation-ID": self.installation_id,
                    "X-Installation-Key": self.installation_key,
                    "Accept": "application/json",
                },
                json=body,
                timeout=self.timeout,
                proxies=self.proxies,
            )
        except requests.RequestException as exc:
            raise HDHiveGatewayError(f"影巢OAuth服务网络错误: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise HDHiveGatewayError(
                f"影巢OAuth服务返回HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else ""
            raise HDHiveGatewayError(str(detail or "影巢OAuth服务请求失败"))
        return data if isinstance(data, dict) else {}

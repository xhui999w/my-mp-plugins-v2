"""HDHive OpenAPI client and HDHive resource-link helpers."""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

import requests


logger = logging.getLogger(__name__)

TRUSTED_HDHIVE_DOMAINS = ("hdhive.com",)
TRUSTED_115_DOMAINS = ("115.com", "115cdn.com", "anxia.com")
RESOURCE_PATH_PATTERN = re.compile(
    r"^/resource/([A-Za-z0-9-]+?)/?$",
    flags=re.IGNORECASE,
)
RESOURCE_URL_PATTERN = re.compile(
    r"https?://(?:[A-Za-z0-9-]+\.)?hdhive\.com/resource/[A-Za-z0-9-]+"
    r"(?:\?[^\s\"'<>#]*)?",
    flags=re.IGNORECASE,
)


def _host_in_allowlist(host: str, allowlist: tuple[str, ...]) -> bool:
    host = (host or "").lower().strip(".")
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in allowlist
    )


def normalize_hdhive_url(url: str) -> str:
    """Validate and normalize a HDHive ``/resource/<slug>`` URL."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in ("http", "https"):
            return ""
        if not _host_in_allowlist(
            parsed.hostname or "",
            TRUSTED_HDHIVE_DOMAINS,
        ):
            return ""
        match = RESOURCE_PATH_PATTERN.match(parsed.path)
        if not match:
            return ""
        return f"https://hdhive.com/resource/{match.group(1)}"
    except Exception:
        return ""


def is_hdhive_url(url: str) -> bool:
    return bool(normalize_hdhive_url(url))


def extract_hdhive_urls(text: str) -> List[str]:
    """Extract unique, trusted HDHive resource URLs from text or HTML."""
    if not text:
        return []
    found: List[str] = []
    for match in RESOURCE_URL_PATTERN.finditer(text):
        normalized = normalize_hdhive_url(match.group(0))
        if normalized and normalized not in found:
            found.append(normalized)
    return found


def extract_resource_slug(resource_url: str) -> str:
    normalized = normalize_hdhive_url(resource_url)
    if not normalized:
        raise ValueError("无效的影巢资源链接")
    match = RESOURCE_PATH_PATTERN.match(urlparse(normalized).path)
    if not match:
        raise ValueError("影巢资源链接缺少资源标识")
    return match.group(1)


class HDHiveError(Exception):
    """Structured HDHive OpenAPI error."""

    def __init__(
        self,
        code: str,
        message: str,
        description: str = "",
        status: int = 0,
        retry_after: int = 0,
    ):
        super().__init__(description or message or code)
        self.code = str(code or "HDHIVE_ERROR")
        self.message = str(message or "影巢请求失败")
        self.description = str(description or "")
        self.status = int(status or 0)
        self.retry_after = int(retry_after or 0)

    def safe_message(self) -> str:
        detail = self.description or self.message
        if self.retry_after:
            return f"{self.code}: {detail}（{self.retry_after}秒后重试）"
        return f"{self.code}: {detail}"


@dataclass
class HDHiveResource:
    name: str = ""
    share_url: str = ""
    password: str = ""
    files: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.files is None:
            self.files = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HDHiveClient:
    """Independent HDHive OpenAPI client.

    The client uses the official dual-layer authentication model:
    ``X-API-Key`` for the approved application and OAuth Bearer tokens for
    the authorized HDHive user.
    """

    REFRESH_REQUIRED = "OPENAPI_REFRESH_REQUIRED"

    def __init__(
        self,
        api_secret: str,
        access_token: str = "",
        refresh_token: str = "",
        base_url: str = "https://hdhive.com",
        timeout: int = 30,
        proxies: Optional[Dict[str, str]] = None,
        on_token_refresh: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.api_secret = (api_secret or "").strip()
        self.access_token = (access_token or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.base_url = (base_url or "https://hdhive.com").rstrip("/")
        self.timeout = max(int(timeout or 30), 5)
        self.proxies = proxies
        self.on_token_refresh = on_token_refresh
        self.session = requests.Session()

    @staticmethod
    def build_authorize_url(
        client_id: str,
        redirect_uri: str,
        scope: str = "meta query unlock",
        state: str = "",
        response_mode: str = "",
        base_url: str = "https://hdhive.com",
    ) -> str:
        params = {
            "client_id": (client_id or "").strip(),
            "redirect_uri": (redirect_uri or "").strip(),
            "scope": (scope or "").strip(),
            "state": (state or secrets.token_hex(16)).strip(),
        }
        if response_mode:
            params["response_mode"] = response_mode.strip()
        return (
            f"{(base_url or 'https://hdhive.com').rstrip('/')}"
            f"/openapi/authorize?{urlencode(params)}"
        )

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        data = self._request(
            "POST",
            "/api/public/openapi/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": (code or "").strip(),
                "redirect_uri": (redirect_uri or "").strip(),
            },
            user_required=False,
            allow_refresh=False,
        )
        return self._set_tokens(data)

    def refresh_access_token(self) -> Dict[str, Any]:
        if not self.refresh_token:
            raise HDHiveError(
                "OPENAPI_REAUTH_REQUIRED",
                "缺少 Refresh Token",
                "请重新完成影巢 OAuth 授权",
                401,
            )
        data = self._request(
            "POST",
            "/api/public/openapi/oauth/refresh",
            body={"refresh_token": self.refresh_token},
            user_required=False,
            allow_refresh=False,
        )
        return self._set_tokens(data)

    def ping(self) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/api/open/ping",
            user_required=False,
        )

    def get_me(self) -> Dict[str, Any]:
        return self._request("GET", "/api/open/me")

    def get_resource_detail(self, slug: str) -> Dict[str, Any]:
        return self._request(
            "GET",
            f"/api/open/shares/{quote(slug, safe='')}",
        )

    def get_115_share(self, slug: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/api/open/resources/unlock",
            body={"slug": slug},
        )

    def resolve_resource(self, resource_url: str) -> Dict[str, Any]:
        """Resolve a HDHive resource URL into a standard 115 resource."""
        slug = extract_resource_slug(resource_url)
        name = slug

        try:
            detail = self.get_resource_detail(slug)
            media = detail.get("media") if isinstance(detail, dict) else {}
            if not isinstance(media, dict):
                media = {}
            name = str(
                detail.get("title")
                or detail.get("name")
                or media.get("title")
                or media.get("name")
                or slug
            ).strip()
        except HDHiveError as exc:
            # Resource details improve display only. Unlock remains authoritative.
            logger.warning(
                "影巢资源详情获取失败，继续尝试解锁: %s",
                exc.safe_message(),
            )

        unlocked = self.get_115_share(slug)
        if not isinstance(unlocked, dict):
            raise HDHiveError(
                "INVALID_RESPONSE",
                "影巢解锁接口返回格式错误",
            )

        raw_url = str(
            unlocked.get("full_url")
            or unlocked.get("url")
            or ""
        ).strip()
        password = str(unlocked.get("access_code") or "").strip()
        share_url, query_password = self._normalize_115_share(raw_url)
        if not password:
            password = query_password
        if not share_url:
            raise HDHiveError(
                "INVALID_115_SHARE",
                "影巢接口未返回有效的115分享链接",
            )

        return HDHiveResource(
            name=name,
            share_url=share_url,
            password=password,
            files=[],
        ).to_dict()

    def _set_tokens(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise HDHiveError("INVALID_TOKEN_RESPONSE", "Token响应格式错误")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token:
            raise HDHiveError("INVALID_TOKEN_RESPONSE", "响应中缺少Access Token")
        self.access_token = access_token
        if refresh_token:
            self.refresh_token = refresh_token
        tokens = dict(data)
        tokens["access_token"] = self.access_token
        tokens["refresh_token"] = self.refresh_token
        if self.on_token_refresh:
            self.on_token_refresh(tokens)
        return tokens

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        user_required: bool = True,
        allow_refresh: bool = True,
    ) -> Dict[str, Any]:
        if not self.api_secret:
            raise HDHiveError("MISSING_API_KEY", "未配置影巢应用Secret", status=401)
        if user_required and not self.access_token:
            raise HDHiveError(
                "OPENAPI_USER_REQUIRED",
                "未配置影巢Access Token",
                status=401,
            )

        headers = {
            "X-API-Key": self.api_secret,
            "Accept": "application/json",
            "User-Agent": "MoviePilot-Tg115Transfer/1.0",
        }
        if user_required:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            response = self.session.request(
                method=method,
                url=f"{self.base_url}{path}",
                headers=headers,
                json=body,
                proxies=self.proxies,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.error("影巢OpenAPI网络错误: %s", exc)
            raise HDHiveError(
                "NETWORK_ERROR",
                "影巢OpenAPI网络错误",
                str(exc),
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(
                "影巢OpenAPI返回非JSON数据，HTTP %s",
                response.status_code,
            )
            raise HDHiveError(
                "INVALID_RESPONSE",
                "影巢OpenAPI返回了无法解析的数据",
                status=response.status_code,
            ) from exc

        success = (
            response.ok
            and isinstance(payload, dict)
            and payload.get("success", True) is not False
        )
        if not success:
            error = self._build_error(response, payload)
            if (
                allow_refresh
                and error.code == self.REFRESH_REQUIRED
                and self.refresh_token
            ):
                self.refresh_access_token()
                return self._request(
                    method,
                    path,
                    body,
                    user_required=user_required,
                    allow_refresh=False,
                )
            logger.error("影巢OpenAPI请求失败: %s", error.safe_message())
            raise error

        if isinstance(payload, dict) and "data" in payload:
            data = payload.get("data")
            return data if isinstance(data, dict) else {"items": data}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _build_error(
        response: requests.Response,
        payload: Any,
    ) -> HDHiveError:
        data = payload if isinstance(payload, dict) else {}
        retry_after_raw = (
            response.headers.get("Retry-After")
            or data.get("retry_after_seconds")
            or 0
        )
        try:
            retry_after = int(retry_after_raw)
        except (TypeError, ValueError):
            retry_after = 0
        return HDHiveError(
            code=str(data.get("code") or response.status_code),
            message=str(data.get("message") or response.reason),
            description=str(data.get("description") or ""),
            status=response.status_code,
            retry_after=retry_after,
        )

    @staticmethod
    def _normalize_115_share(url: str) -> tuple[str, str]:
        if not url:
            return "", ""
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in ("http", "https"):
                return "", ""
            if not _host_in_allowlist(
                parsed.hostname or "",
                TRUSTED_115_DOMAINS,
            ):
                return "", ""
            match = re.match(
                r"^/s/([A-Za-z0-9]+)",
                parsed.path,
                flags=re.IGNORECASE,
            )
            if not match:
                return "", ""
            query = parse_qs(parsed.query)
            password = ""
            for key in ("password", "pwd", "code"):
                value = (query.get(key) or [""])[0].strip()
                if value:
                    password = value
                    break
            return f"https://115.com/s/{match.group(1)}", password
        except Exception:
            return "", ""

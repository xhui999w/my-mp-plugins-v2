"""Small self-hosted HDHive OAuth and OpenAPI gateway.

App Secret and OAuth tokens never leave this service. MoviePilot authenticates
with an installation id/key and receives only normalized resource metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import base64
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import asyncio
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import qrcode
import qrcode.image.svg
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .explore import RANKINGS, TMDBProvider, filter_metadata, registry
from .douban import DoubanProvider
from .explore_ui import explore_html
from .subscriptions_ui import subscriptions_html
from .tasks_ui import tasks_html
from .resource_ui import resource_detail_html
from .search_ui import search_html
from .resources import HDHiveResourceProvider, ResourceProviderRegistry, filter_options, humanize_pan_type
from .unlocks_ui import unlocks_html
from .logs_ui import logs_html
from .settings_ui import settings_html
from .authorizations_ui import authorizations_html
from .telegram_ui import telegram_html
from .rankings_ui import rankings_html
from .theme import theme_head
from .notifications import ChannelMonitor, NotificationError, NotificationService, TelegramProvider
from .p115 import P115Client, P115Error
from .credentials import AUTHORIZATION_PROVIDERS


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BASE_URL = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com").rstrip("/")
CLIENT_ID = required_env("HDHIVE_CLIENT_ID")
WEB_ADMIN_USER = os.getenv("WEB_ADMIN_USER", "").strip()
WEB_ADMIN_PASSWORD = os.getenv("WEB_ADMIN_PASSWORD", "").strip()
ADMIN_COOKIE = "moon_admin"
APP_SECRET = required_env("HDHIVE_APP_SECRET")
REDIRECT_URI = required_env("HDHIVE_REDIRECT_URI")
INSTALLATION_KEY = required_env("INSTALLATION_KEY")
WEB_INSTALLATION_ID = os.getenv("WEB_INSTALLATION_ID", "personal-web")
TOKEN_ENCRYPTION_KEY = required_env("TOKEN_ENCRYPTION_KEY")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/data/hdhive-oauth.db"))
OAUTH_SCOPE = os.getenv("HDHIVE_OAUTH_SCOPE", "meta query unlock").strip()
REQUEST_TIMEOUT = max(5, min(int(os.getenv("REQUEST_TIMEOUT", "30")), 120))
EMBY_TIMEOUT = max(3, min(int(os.getenv("EMBY_TIMEOUT", "8")), 30))
STATE_TTL = max(120, min(int(os.getenv("OAUTH_STATE_TTL", "600")), 3600))

try:
    CIPHER = Fernet(TOKEN_ENCRYPTION_KEY.encode("ascii"))
except (ValueError, TypeError) as exc:
    raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

RESOURCE_RE = re.compile(
    r"^https?://(?:[A-Za-z0-9-]+\.)?hdhive\.com/resource/([A-Za-z0-9-]+)/?$",
    re.IGNORECASE,
)


SHARE_115_RE = re.compile(r"(?:115\.com|115cdn\.com)/s/", re.IGNORECASE)
OFFLINE_LINK_RE = re.compile(r"^(?:magnet:\?|ed2k://)", re.IGNORECASE)


def classify_share_link(url: str) -> str:
    """判断解锁后真实链接类型：115 / offline(magnet,ed2k) / unsupported / empty。"""
    if not url.strip():
        return "empty"
    if SHARE_115_RE.search(url):
        return "115"
    if OFFLINE_LINK_RE.match(url):
        return "offline"
    return "unsupported"


def _list_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " / ".join(str(x).strip() for x in value if str(x).strip())
    return str(value).strip()


def _link_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return str(urlparse(url).netloc or "unknown")
    except Exception:
        return "unknown"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    if os.getenv("DISABLE_BACKGROUND_WORKERS", "").strip() != "1":
        start_channel_worker()
    yield


app = FastAPI(
    title="HDHive OAuth Gateway",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
logger = logging.getLogger("hdhive-oauth")


def _admin_token(expires: int) -> str:
    payload = f"{WEB_ADMIN_USER}:{expires}"
    signature = hmac.new(INSTALLATION_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def _admin_token_valid(value: str | None) -> bool:
    if not value or "." not in value:
        return False
    expires_text, signature = value.split(".", 1)
    if not expires_text.isdigit() or int(expires_text) < int(time.time()):
        return False
    return hmac.compare_digest(_admin_token(int(expires_text)), value)


@app.middleware("http")
async def protect_personal_dashboard(request: Request, call_next):
    """Protect the public personal dashboard when admin credentials are configured."""
    if not (WEB_ADMIN_USER and WEB_ADMIN_PASSWORD):
        return await call_next(request)
    path = request.url.path
    public = path == "/health" or path.startswith("/v1/") or path.startswith("/oauth/callback") or path in {"/admin/login", "/admin/logout"}
    if public or _admin_token_valid(request.cookies.get(ADMIN_COOKIE)):
        modern_routes = {"/": "/rankings", "/web/rankings": "/rankings", "/web/discover": "/explore"}
        if path in modern_routes and _admin_token_valid(request.cookies.get(ADMIN_COOKIE)):
            return RedirectResponse(modern_routes[path], status_code=303)
        return await call_next(request)
    if path.startswith("/api/"):
        return HTMLResponse('{"detail":"管理会话已失效，请重新登录"}', status_code=401, media_type="application/json")
    return RedirectResponse(f"/admin/login?next={quote(path)}", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(next: str = Query("/rankings")) -> HTMLResponse:
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/rankings"
    return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · 影舟 MovieArk</title>{theme_head()}<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 70% 10%,var(--accent-soft),var(--bg-deep) 50%);color:var(--text-primary);font:15px system-ui,"Microsoft YaHei"}}form{{width:min(390px,calc(100vw - 36px));padding:34px;background:var(--card-bg);border:1px solid var(--accent-primary);border-radius:18px;box-shadow:var(--card-shadow)}}h1{{color:var(--gold)}}p{{color:var(--text-muted)}}input,button{{display:block;width:100%;margin:12px 0;padding:12px;border-radius:9px;border:1px solid var(--input-border);background:var(--input-bg);color:var(--text-primary)}}button{{background:var(--accent-primary);color:var(--button-primary-text);cursor:pointer;font-weight:800}}</style><form method="post" action="/admin/login"><h1>◉ 影舟 MovieArk</h1><p>个人管理中心登录</p><input name="username" autocomplete="username" placeholder="用户名" required autofocus><input name="password" type="password" autocomplete="current-password" placeholder="密码" required><input type="hidden" name="next" value="{html.escape(safe_next)}"><button>登录</button></form></html>''')


@app.post("/admin/login")
def admin_login(username: str = Form(...), password: str = Form(...), next: str = Form("/rankings")) -> RedirectResponse:
    if not WEB_ADMIN_USER or not WEB_ADMIN_PASSWORD or not (hmac.compare_digest(username, WEB_ADMIN_USER) and hmac.compare_digest(password, WEB_ADMIN_PASSWORD)):
        raise HTTPException(401, "用户名或密码错误")
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/rankings"
    response = RedirectResponse(safe_next, status_code=303)
    response.set_cookie(ADMIN_COOKIE, _admin_token(int(time.time()) + 86400 * 30), max_age=86400 * 30, secure=True, httponly=True, samesite="strict")
    return response


@app.get("/admin/logout")
def admin_logout() -> RedirectResponse:
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response


@contextmanager
def database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DATABASE_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_database() -> None:
    with database() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            installation_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS installations (
            installation_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expires_at INTEGER DEFAULT 0,
            user_json TEXT DEFAULT '{}',
            updated_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS transfer_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            share_url TEXT DEFAULT '',
            status TEXT NOT NULL,
            error TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS web_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS subscription_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            subscription_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            resource_count INTEGER DEFAULT 0,
            transfer_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            created_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS web_settings (
            installation_id TEXT PRIMARY KEY,
            moviepilot_url TEXT DEFAULT '',
            save_directory TEXT DEFAULT '',
            offline_enabled INTEGER DEFAULT 1,
            tmdb_api_key TEXT DEFAULT '',
            tmdb_language TEXT DEFAULT 'zh-CN',
            tmdb_region TEXT DEFAULT 'CN',
            updated_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS channel_state (
            installation_id TEXT PRIMARY KEY,
            last_message_id INTEGER DEFAULT 0,
            last_check INTEGER DEFAULT 0,
            next_check INTEGER DEFAULT 0,
            last_status TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            processed INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            name TEXT DEFAULT '',
            username TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            check_interval INTEGER DEFAULT 600,
            last_message_id INTEGER DEFAULT 0,
            last_check_at INTEGER DEFAULT 0,
            next_check_at INTEGER DEFAULT 0,
            last_status TEXT DEFAULT '',
            last_error TEXT DEFAULT '',
            processed_count INTEGER DEFAULT 0,
            rules_json TEXT DEFAULT '{}',
            created_at INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS telegram_channel_messages (
            installation_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            processed_at INTEGER DEFAULT 0,
            PRIMARY KEY (installation_id, channel_id, message_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS provider_state (
            installation_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            state_json TEXT DEFAULT '{}',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (installation_id, provider)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS offline_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            task_url TEXT NOT NULL,
            provider_task_id TEXT DEFAULT '',
            status TEXT DEFAULT 'submitted',
            save_path TEXT DEFAULT '',
            error TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS resource_search_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id INTEGER DEFAULT 0,
            douban_id TEXT DEFAULT '',
            title TEXT NOT NULL,
            year TEXT DEFAULT '',
            result_json TEXT NOT NULL,
            searched_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            installation_id TEXT NOT NULL,
            title TEXT NOT NULL,
            media_type TEXT DEFAULT '',
            year TEXT DEFAULT '',
            tmdb_id INTEGER DEFAULT 0,
            douban_id TEXT DEFAULT '',
            poster TEXT DEFAULT '',
            result_count INTEGER DEFAULT 0,
            searched_at INTEGER NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            level TEXT NOT NULL,
            module TEXT DEFAULT '',
            message TEXT NOT NULL
        )""")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_settings)")}
        for name, definition in (
            ("tmdb_api_key", "TEXT DEFAULT ''"),
            ("tmdb_language", "TEXT DEFAULT 'zh-CN'"),
            ("tmdb_region", "TEXT DEFAULT 'CN'"),
            ("douban_cookie", "TEXT DEFAULT ''"),
            ("netflix_token", "TEXT DEFAULT ''"),
            ("max_token", "TEXT DEFAULT ''"),
            ("prime_token", "TEXT DEFAULT ''"),
            ("disney_token", "TEXT DEFAULT ''"),
            ("apple_token", "TEXT DEFAULT ''"),
            ("root_directory", "TEXT DEFAULT ''"),
            ("scrape_directory", "TEXT DEFAULT ''"),
            ("save_folder_id", "TEXT DEFAULT ''"),
            ("save_wait_seconds", "INTEGER DEFAULT 30"),
            ("retry_count", "INTEGER DEFAULT 3"),
            ("duplicate_policy", "TEXT DEFAULT 'skip'"),
            ("subscription_auto_transfer", "INTEGER DEFAULT 1"),
            ("subscription_interval", "INTEGER DEFAULT 1800"),
            ("ed2k_directory", "TEXT DEFAULT ''"),
            ("ed2k_poll_interval", "INTEGER DEFAULT 60"),
            ("ed2k_retry_count", "INTEGER DEFAULT 3"),
            ("ed2k_auto_archive", "INTEGER DEFAULT 1"),
            ("emby_url", "TEXT DEFAULT ''"),
            ("emby_api_key", "TEXT DEFAULT ''"),
            ("emby_user_id", "TEXT DEFAULT ''"),
            ("p115_cookie", "TEXT DEFAULT ''"),
            ("p115_security_key", "TEXT DEFAULT ''"),
            ("telegram_bot_token", "TEXT DEFAULT ''"),
            ("telegram_chat_id", "TEXT DEFAULT ''"),
            ("telegram_api_id", "TEXT DEFAULT ''"),
            ("telegram_api_hash", "TEXT DEFAULT ''"),
            ("telegram_user_id", "TEXT DEFAULT ''"),
            ("telegram_update_offset", "INTEGER DEFAULT 0"),
            ("telegram_enabled", "INTEGER DEFAULT 0"),
            ("telegram_events", "TEXT DEFAULT '{}'"),
            ("telegram_template", "TEXT DEFAULT ''"),
            ("channel_enabled", "INTEGER DEFAULT 0"),
            ("channel_name", "TEXT DEFAULT 'oneonefivewpfx'"),
            ("channel_interval", "INTEGER DEFAULT 600"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE web_settings ADD COLUMN {name} {definition}")
        subscription_columns = {row[1] for row in conn.execute("PRAGMA table_info(web_subscriptions)")}
        for name, definition in (
            ("year", "INTEGER"), ("original_title", "TEXT DEFAULT ''"),
            ("poster", "TEXT DEFAULT ''"), ("season", "TEXT DEFAULT ''"),
            ("subscription_scope", "TEXT DEFAULT '普通订阅'"),
            ("category", "TEXT DEFAULT ''"), ("save_path", "TEXT DEFAULT ''"),
            ("updated_at", "INTEGER DEFAULT 0"), ("douban_id", "TEXT DEFAULT ''"),
            ("moviepilot_id", "TEXT DEFAULT ''"), ("error", "TEXT DEFAULT ''"),
            ("last_run_at", "INTEGER DEFAULT 0"),
        ):
            if name not in subscription_columns:
                conn.execute(f"ALTER TABLE web_subscriptions ADD COLUMN {name} {definition}")
        transfer_columns = {row[1] for row in conn.execute("PRAGMA table_info(transfer_records)")}
        for name, definition in (
            ("subscription_id", "INTEGER"), ("media_type", "TEXT DEFAULT ''"),
            ("tmdb_id", "INTEGER DEFAULT 0"), ("resolution", "TEXT DEFAULT ''"),
            ("quality", "TEXT DEFAULT ''"), ("size", "TEXT DEFAULT ''"),
            ("uploader", "TEXT DEFAULT ''"), ("points", "INTEGER DEFAULT 0"),
            ("action", "TEXT DEFAULT '资源解锁'"), ("save_path", "TEXT DEFAULT ''"),
            ("processing_status", "TEXT DEFAULT 'pending'"),
            ("source_type", "TEXT DEFAULT '115网盘'"), ("updated_at", "INTEGER DEFAULT 0"),
            ("unlock_status", "TEXT DEFAULT ''"), ("transfer_status", "TEXT DEFAULT ''"),
        ):
            if name not in transfer_columns:
                conn.execute(f"ALTER TABLE transfer_records ADD COLUMN {name} {definition}")
        # 安全迁移：把旧 status 拆分为 unlock_status / transfer_status，只处理一次
        migrated = conn.execute("SELECT COUNT(*) FROM transfer_records WHERE (unlock_status='' OR transfer_status='') AND status!=''").fetchone()[0]
        if migrated:
            rows = conn.execute("SELECT id,status,share_url FROM transfer_records WHERE (unlock_status='' OR transfer_status='') AND status!=''").fetchall()
            for row in rows:
                if row["status"] == "completed":
                    unlock_s, transfer_s = "unlocked", "success"
                elif row["status"] == "resolved":
                    unlock_s, transfer_s = "unlocked", "pending"
                elif row["status"] == "failed":
                    unlock_s = "unlocked" if (row["share_url"] or "") else "failed"
                    transfer_s = "failed" if (row["share_url"] or "") else "pending"
                else:
                    unlock_s, transfer_s = "pending", "pending"
                conn.execute("UPDATE transfer_records SET unlock_status=?,transfer_status=? WHERE id=?", (unlock_s, transfer_s, row["id"]))
        # 单频道 → 多频道迁移：旧 channel_name/channel_state 自动成为第一个频道记录，不丢配置。
        existing = conn.execute("SELECT COUNT(*) FROM telegram_channels WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()[0]
        if not existing:
            legacy = conn.execute("SELECT channel_enabled,channel_name,channel_interval FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
            if legacy and (legacy["channel_name"] or "").strip():
                legacy_state_row = conn.execute("SELECT last_message_id,last_check,next_check,last_status,last_error,processed FROM channel_state WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
                legacy_state = dict(legacy_state_row) if legacy_state_row else {}
                now = int(time.time())
                username = _normalize_channel_username(legacy["channel_name"])
                conn.execute(
                    """INSERT INTO telegram_channels (installation_id,name,username,enabled,check_interval,last_message_id,last_check_at,next_check_at,last_status,last_error,processed_count,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (WEB_INSTALLATION_ID, legacy["channel_name"], username, int(legacy["channel_enabled"] or 0), int(legacy["channel_interval"] or 600),
                     int(legacy_state.get("last_message_id") or 0), int(legacy_state.get("last_check") or 0), int(legacy_state.get("next_check") or 0),
                     str(legacy_state.get("last_status") or "等待运行"), str(legacy_state.get("last_error") or ""), int(legacy_state.get("processed") or 0), now, now))
    install_db_log_handler()


_SECRET_PATTERN = re.compile(r"(?i)\b(cookie|token|api[_-]?key|password|passwd|secret|authorization|access[_-]?key)[=:]\s*([^\s&,;\"]{3,})")


def _mask_secrets(text: str) -> str:
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=***", text)


class DBLogHandler(logging.Handler):
    """Persist business logs to app_logs with token/cookie/key masking."""

    _installed = False
    _emit_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = _mask_secrets(self.format(record))
            match = re.search(r"(?:^|\s)module=([A-Za-z0-9_\-]+)", message)
            module = match.group(1) if match else ""
            with database() as conn:
                conn.execute(
                    "INSERT INTO app_logs (ts, level, module, message) VALUES (?,?,?,?)",
                    (int(record.created), record.levelname, module, message[:4000]),
                )
            DBLogHandler._emit_count += 1
            if DBLogHandler._emit_count % 200 == 0:
                self._prune()
        except Exception:
            pass

    def _prune(self) -> None:
        try:
            with database() as conn:
                conn.execute("DELETE FROM app_logs WHERE ts < ?", (int(time.time()) - 30 * 86400,))
                conn.execute(
                    "DELETE FROM app_logs WHERE id NOT IN (SELECT id FROM app_logs ORDER BY id DESC LIMIT 10000)"
                )
        except Exception:
            pass


def install_db_log_handler() -> None:
    if DBLogHandler._installed:
        return
    logging.getLogger("hdhive-oauth").setLevel(logging.INFO)
    handler = DBLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger("hdhive-oauth").addHandler(handler)
    logging.getLogger().addHandler(handler)
    DBLogHandler._installed = True
    handler._prune()


def encrypt(value: str) -> str:
    return CIPHER.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    try:
        return CIPHER.decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(500, "Stored OAuth token cannot be decrypted") from exc


def explore_tmdb_provider() -> TMDBProvider:
    init_database()
    with database() as conn:
        row = conn.execute("SELECT tmdb_api_key, tmdb_language, tmdb_region FROM web_settings WHERE installation_id = ?", (WEB_INSTALLATION_ID,)).fetchone()
    token = decrypt(row["tmdb_api_key"]) if row and row["tmdb_api_key"] else os.getenv("TMDB_API_KEY", "").strip()
    return TMDBProvider(token, row["tmdb_language"] if row else "zh-CN", row["tmdb_region"] if row else "CN", REQUEST_TIMEOUT)


_douban_provider = DoubanProvider(timeout=REQUEST_TIMEOUT)


def explore_douban_provider() -> DoubanProvider:
    return _douban_provider


def valid_installation_id(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", value):
        raise HTTPException(400, "Invalid installation id")
    return value


def sign_start(installation_id: str, expires: int) -> str:
    payload = f"{installation_id}:{expires}".encode()
    return hmac.new(
        INSTALLATION_KEY.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def require_installation(
    x_installation_id: str = Header(...),
    x_installation_key: str = Header(...),
) -> str:
    installation_id = valid_installation_id(x_installation_id)
    if not hmac.compare_digest(x_installation_key, INSTALLATION_KEY):
        raise HTTPException(401, "Invalid installation credential")
    return installation_id


async def hdhive_request(
    method: str,
    path: str,
    access_token: str = "",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"X-API-Key": APP_SECRET, "Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            response = await client.request(
                method, f"{BASE_URL}{path}", headers=headers, json=body
            )
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"HDHive network error: {exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            502, f"HDHive returned HTTP {response.status_code}"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, "HDHive returned an unexpected response")
    if response.status_code >= 400 or payload.get("success") is False:
        code = str(payload.get("code") or response.status_code)
        message = str(
            payload.get("description")
            or payload.get("message")
            or "HDHive request failed"
        )
        raise HDHiveAPIError(response.status_code, code, message)
    data = payload.get("data", payload)
    return data if isinstance(data, dict) else {"items": data}


class HDHiveAPIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def load_installation(installation_id: str) -> sqlite3.Row:
    with database() as conn:
        row = conn.execute(
            "SELECT * FROM installations WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "HDHive account is not authorized")
    return row


async def save_tokens(
    installation_id: str, token_data: dict[str, Any]
) -> dict[str, Any]:
    access = str(token_data.get("access_token") or "").strip()
    refresh = str(token_data.get("refresh_token") or "").strip()
    if not access:
        raise HTTPException(502, "OAuth response is missing access_token")
    expires_in = int(token_data.get("expires_in") or 0)
    expires_at = int(time.time()) + expires_in if expires_in else 0
    user = await hdhive_request("GET", "/api/open/me", access_token=access)
    with database() as conn:
        old = conn.execute(
            "SELECT refresh_token FROM installations WHERE installation_id = ?",
            (installation_id,),
        ).fetchone()
        if not refresh and old:
            refresh = decrypt(old["refresh_token"])
        conn.execute(
            """INSERT INTO installations
            (installation_id, access_token, refresh_token, expires_at, user_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(installation_id) DO UPDATE SET
              access_token=excluded.access_token,
              refresh_token=excluded.refresh_token,
              expires_at=excluded.expires_at,
              user_json=excluded.user_json,
              updated_at=excluded.updated_at""",
            (
                installation_id,
                encrypt(access),
                encrypt(refresh),
                expires_at,
                json.dumps(user, ensure_ascii=False),
                int(time.time()),
            ),
        )
    return user


async def access_for(installation_id: str) -> str:
    row = load_installation(installation_id)
    access = decrypt(row["access_token"])
    if not row["expires_at"] or int(row["expires_at"]) > int(time.time()) + 60:
        return access
    refresh = decrypt(row["refresh_token"])
    if not refresh:
        raise HTTPException(401, "HDHive OAuth authorization must be renewed")
    return await refresh_for(installation_id, refresh)


async def refresh_for(installation_id: str, refresh_token: str = "") -> str:
    if not refresh_token:
        row = load_installation(installation_id)
        refresh_token = decrypt(row["refresh_token"])
    if not refresh_token:
        raise HTTPException(401, "HDHive OAuth authorization must be renewed")
    try:
        token_data = await hdhive_request(
            "POST",
            "/api/public/openapi/oauth/refresh",
            body={"refresh_token": refresh_token},
        )
        await save_tokens(installation_id, token_data)
        return str(token_data.get("access_token") or "")
    except HDHiveAPIError as exc:
        with database() as conn:
            conn.execute(
                "INSERT INTO transfer_records (installation_id, slug, name, status, error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (installation_id, slug, slug, "failed", f"{exc.code}: {exc.message}", int(time.time())),
            )
        raise HTTPException(exc.status, f"{exc.code}: {exc.message}") from exc


async def authorized_request(
    installation_id: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    access = await access_for(installation_id)
    try:
        return await hdhive_request(method, path, access_token=access, body=body)
    except HDHiveAPIError as exc:
        if exc.code != "OPENAPI_REFRESH_REQUIRED":
            raise
        access = await refresh_for(installation_id)
        return await hdhive_request(method, path, access_token=access, body=body)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "hdhive-oauth"}


@app.get("/api/image-proxy")
async def image_proxy(url: str = Query(..., min_length=12, max_length=2048)) -> Response:
    """Proxy public poster images so Douban's hotlink protection stays server-side."""
    if not url.startswith(("https://img", "http://img")) or "doubanio.com" not in url:
        raise HTTPException(400, "Unsupported image host")
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://movie.douban.com/"})
    if response.status_code != 200:
        raise HTTPException(response.status_code, "Image unavailable")
    return Response(content=response.content, media_type=response.headers.get("content-type", "image/jpeg"), headers={"Cache-Control": "public, max-age=86400"})


@app.get("/explore", response_class=HTMLResponse)
def explore_page() -> HTMLResponse:
    return HTMLResponse(explore_html())


@app.get("/resources", response_class=HTMLResponse)
def resource_detail_page() -> HTMLResponse:
    return HTMLResponse(resource_detail_html())


@app.get("/search", response_class=HTMLResponse)
def media_search_page() -> HTMLResponse:
    return HTMLResponse(search_html())


@app.get("/api/explore/status")
def explore_status() -> dict[str, Any]:
    tmdb = explore_tmdb_provider()
    return {"ok": True, "configured": tmdb.configured, "providers": registry(tmdb.configured)}


@app.get("/api/explore/providers")
def explore_providers() -> dict[str, Any]:
    tmdb = explore_tmdb_provider()
    return {"items": registry(tmdb.configured)}


@app.get("/api/explore/filters")
async def explore_filters(media_type: str = Query("movie", pattern="^(movie|tv)$")) -> dict[str, Any]:
    tmdb = explore_tmdb_provider()
    genres: list[dict[str, Any]] = []
    if tmdb.configured:
        try:
            genres = await tmdb.genres(media_type)
        except Exception:
            genres = []
    return {"data": filter_metadata(genres), "douban": explore_douban_provider().metadata(), "configured": tmdb.configured}


@app.get("/api/explore/discover")
async def explore_discover(
    provider: str = Query("tmdb"), media_type: str = Query("movie", pattern="^(movie|tv)$"),
    region: str = Query(""), country: str = Query(""), language: str = Query(""), genre: str = Query(""), year: str = Query(""),
    sort: str = Query("popularity.desc"), rating: float = Query(0, ge=0, le=10), page: int = Query(1, ge=1),
    category: str = Query(""),
) -> dict[str, Any]:
    if provider == "douban":
        result = await explore_douban_provider().discover({"media_type": media_type, "country": country,
            "genre": genre, "year": year, "sort": sort, "page": page})
        result["capabilities"] = explore_douban_provider().capabilities
        return result
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {"items": [], "page": page, "total_pages": 0, "has_more": False, "provider": provider, "configured": False, "error": "该数据源尚未配置，请在设置页面填写 TMDB API Key。"}
    if provider not in {"tmdb", "netflix", "max", "prime", "disney", "apple"}:
        return {"items": [], "page": page, "total_pages": 0, "has_more": False, "provider": provider, "configured": False, "error": "该数据源暂不可用。"}
    try:
        normalized_year = int(year) if year.isdigit() and 1900 <= int(year) <= 2100 else None
        return await tmdb.discover({"platform": "" if provider == "tmdb" else provider, "media_type": media_type, "region": region, "language": language, "genre": genre, "year": normalized_year, "sort": sort, "rating": rating, "page": page})
    except Exception as exc:
        return {"items": [], "page": page, "total_pages": 0, "has_more": False, "provider": provider, "configured": True, "error": "影视数据加载失败，请稍后重试。", "detail": type(exc).__name__}


@app.get("/api/explore/rankings")
async def explore_rankings(provider: str = Query("tmdb")) -> dict[str, Any]:
    """Return categories belonging to the selected ranking provider."""
    if provider == "douban":
        return {"items": [
            {"id": "popular-movie", "name": "热门电影"},
            {"id": "top-movie", "name": "高分电影"},
            {"id": "top250", "name": "Top250"},
            {"id": "popular-tv", "name": "热门电视剧"},
            {"id": "top-tv", "name": "高分电视剧"},
        ]}
    if provider in {"netflix", "max", "prime", "disney", "apple"}:
        return {"items": [
            {"id": "popular-movie", "name": "热门电影"},
            {"id": "popular-tv", "name": "热门电视剧"},
            {"id": "top-movie", "name": "高分电影"},
            {"id": "top-tv", "name": "高分电视剧"},
        ]}
    return {"items": [{"id": key, "name": name} for key, name in RANKINGS.items()]}


RANKING_PROVIDERS: tuple[dict[str, Any], ...] = (
    {"id": "tmdb", "name": "TheMovieDB", "logo": "tmdb", "enabled": True, "kind": "tmdb"},
    {"id": "douban", "name": "豆瓣", "logo": "douban", "enabled": True, "kind": "douban"},
    {"id": "netflix", "name": "Netflix", "logo": "netflix", "enabled": True, "kind": "watch_provider"},
    {"id": "max", "name": "HBO Max", "logo": "max", "enabled": True, "kind": "watch_provider"},
    {"id": "prime", "name": "Prime Video", "logo": "prime", "enabled": True, "kind": "watch_provider"},
    {"id": "disney", "name": "Disney+", "logo": "disney", "enabled": True, "kind": "watch_provider"},
    {"id": "apple", "name": "Apple TV+", "logo": "apple", "enabled": True, "kind": "watch_provider"},
)


@app.get("/api/explore/ranking-providers")
async def explore_ranking_providers() -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM web_settings WHERE installation_id = ?", (WEB_INSTALLATION_ID,)).fetchone()
    result = []
    for provider in RANKING_PROVIDERS:
        configured = provider["kind"] == "douban"
        if provider["kind"] in {"tmdb", "watch_provider"}:
            configured = bool(row and row["tmdb_api_key"]) or bool(os.getenv("TMDB_API_KEY", "").strip())
        result.append({**provider, "configured": configured, "status": "connected" if configured else ("unavailable" if provider["kind"] == "unavailable" else "unconfigured")})
    return {"items": result}


STREAMING_RANKING_SORTS = {
    "popular-movie": ("movie", "popularity.desc", 0),
    "popular-tv": ("tv", "popularity.desc", 0),
    "top-movie": ("movie", "vote_average.desc", 50),
    "top-tv": ("tv", "vote_average.desc", 50),
}


async def streaming_ranking(tmdb: TMDBProvider, provider: str, ranking: str, page: int) -> dict[str, Any]:
    """Netflix / HBO Max / Prime Video / Disney+ / Apple TV+ 榜单。

    这些平台没有独立凭据，全部基于 TMDB Watch Providers + TMDB Discover。
    只要 TMDB 已配置可用，平台就自动可用。
    """
    spec = STREAMING_RANKING_SORTS.get(ranking)
    if not spec:
        return {"items": [], "configured": True, "provider": provider, "error": "该平台暂无此榜单。"}
    media_type, sort, min_votes = spec
    try:
        return await tmdb.discover({
            "platform": provider, "media_type": media_type, "region": "US",
            "sort": sort, "rating": min_votes, "page": page,
        })
    except Exception as exc:
        return {"items": [], "configured": True, "provider": provider, "error": "榜单加载失败，请稍后重试。", "detail": type(exc).__name__}


@app.get("/api/explore/ranking/{provider}/{ranking}")
async def explore_ranking(provider: str, ranking: str, page: int = Query(1, ge=1)) -> dict[str, Any]:
    if provider == "douban":
        mapping = {"popular-movie": "hot-movie", "popular-tv": "hot-tv", "top-movie": "high-movie", "top-tv": "high-tv", "top250": "top250"}
        category = mapping.get(ranking, "hot-movie")
        return await explore_douban_provider().discover(category, page)
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {"items": [], "configured": False, "error": "TMDB 尚未配置，流媒体榜单依赖 TMDB。前往授权中心配置 TMDB 后即可启用。"}
    if provider in {"netflix", "max", "prime", "disney", "apple"}:
        return await streaming_ranking(tmdb, provider, ranking, page)
    if provider != "tmdb" or ranking not in RANKINGS:
        raise HTTPException(404, "榜单不存在")
    try:
        return await tmdb.ranking(ranking, page)
    except Exception as exc:
        return {"items": [], "configured": True, "error": "榜单加载失败，请稍后重试。", "detail": type(exc).__name__}


@app.get("/api/explore/media/{media_type}/{tmdb_id}")
async def explore_media(media_type: str, tmdb_id: int) -> dict[str, Any]:
    if media_type not in ("movie", "tv") or tmdb_id <= 0:
        raise HTTPException(400, "无效的媒体参数")
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {"configured": False, "error": "TMDB 尚未配置。", "data": None}
    try:
        payload, cached = await tmdb.request(f"/{media_type}/{tmdb_id}", {"append_to_response": "credits,watch/providers"}, ttl=3600)
        item = tmdb.normalize(payload, media_type)
        item["genres"] = payload.get("genres", [])
        item["countries"] = payload.get("production_countries", [])
        item["actors"] = [{"id": actor.get("id"), "name": actor.get("name"), "character": actor.get("character")} for actor in payload.get("credits", {}).get("cast", [])[:12]]
        directors = [person.get("name") for person in payload.get("credits", {}).get("crew", []) if person.get("job") == "Director"]
        item["director"] = directors[0] if directors else None
        item["watch_providers"] = payload.get("watch/providers", {}).get("results", {}).get(tmdb.region, {})
        return {"configured": True, "cached": cached, "data": item, "error": None}
    except Exception as exc:
        return {"configured": True, "data": None, "error": "详情加载失败，请稍后重试。", "detail": type(exc).__name__}


async def resolve_tmdb_id(media_type: str, title: str, year: str = "") -> dict[str, Any]:
    """通过 TMDB 标题搜索反查 TMDB ID，用于只有豆瓣/标题、没有 TMDB ID 的影视。"""
    if not title.strip():
        return {}
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {}
    try:
        path = "/search/movie" if media_type == "movie" else "/search/tv"
        payload, _ = await tmdb.request(path, {"query": title.strip(), "page": 1, "include_adult": "false"}, ttl=3600)
    except Exception as exc:
        logger.warning("module=media provider=tmdb operation=resolve_id error_type=%s reason=%s", type(exc).__name__, exc)
        return {}
    results = payload.get("results") or []
    if not results:
        return {}
    target_year = str(year or "").strip()
    best: dict[str, Any] | None = None
    for raw in results:
        date = str(raw.get("release_date") or raw.get("first_air_date") or "")
        item_year = date[:4]
        candidate = {
            "tmdb_id": raw.get("id"),
            "title": raw.get("title") or raw.get("name") or "",
            "year": item_year,
            "poster": f"https://image.tmdb.org/t/p/w342{raw.get('poster_path')}" if raw.get("poster_path") else "",
            "backdrop": f"https://image.tmdb.org/t/p/w780{raw.get('backdrop_path')}" if raw.get("backdrop_path") else "",
            "rating": round(float(raw.get("vote_average") or 0), 1),
            "overview": raw.get("overview") or "",
        }
        if best is None:
            best = candidate
        if target_year and item_year == target_year:
            return candidate
    return best or {}


@app.get("/api/web/media/search")
async def web_media_search(keyword: str = Query(..., min_length=1, max_length=200), media_type: str = Query("", pattern="^(|movie|tv)$")) -> dict[str, Any]:
    """Search normalized media metadata; resource searching remains a separate shared step."""
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        raise HTTPException(409, "TMDB 尚未配置，无法匹配影视资料")
    path = f"/search/{media_type}" if media_type else "/search/multi"
    payload, cached = await tmdb.request(path, {"query": keyword.strip(), "page": 1}, ttl=300)
    items: list[dict[str, Any]] = []
    for raw in payload.get("results", []):
        kind = media_type or raw.get("media_type")
        if kind not in ("movie", "tv"):
            continue
        items.append(tmdb.normalize(raw, kind))
    if items:
        first = items[0]
        _save_search_history(
            str(first.get("media_type") or media_type or "movie"),
            int(first.get("tmdb_id") or 0),
            str(first.get("douban_id") or ""),
            str(first.get("title") or keyword),
            str(first.get("year") or ""),
            str(first.get("poster") or ""),
            len(items),
        )
    return {"items": items[:30], "total": len(items), "cached": cached}

def _emby_settings() -> dict[str, str]:
    """Single source of truth for the persisted Emby configuration."""
    with database() as conn:
        row = conn.execute("SELECT emby_url,emby_api_key,emby_user_id FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not row:
        return {"url": "", "api_key": "", "user_id": ""}
    return {
        "url": str(row["emby_url"] or "").strip().rstrip("/"),
        "api_key": _decrypt_optional(row["emby_api_key"]),
        "user_id": str(row["emby_user_id"] or "").strip(),
    }


def _emby_configured(cfg: dict[str, str]) -> bool:
    return bool(cfg.get("url") and cfg.get("api_key"))


def _emby_items_path(cfg: dict[str, str]) -> str:
    """Library items endpoint; falls back to the admin view when no User ID is set."""
    if cfg.get("user_id"):
        return f"/Users/{cfg['user_id']}/Items"
    return "/Items"


async def _emby_library_status(media_type: str, tmdb_id: int) -> tuple[str | None, bool, bool]:
    cfg = _emby_settings()
    configured = _emby_configured(cfg)
    if not configured:
        return None, False, False
    try:
        headers = {"X-Emby-Token": cfg["api_key"]}
        params = {"Recursive": "true", "IncludeItemTypes": "Movie" if media_type == "movie" else "Series", "AnyProviderIdEquals": f"tmdb.{tmdb_id}", "Fields": "ProviderIds", "Limit": 5}
        async with httpx.AsyncClient(timeout=EMBY_TIMEOUT) as client:
            emby_response = await client.get(f"{cfg['url']}{_emby_items_path(cfg)}", headers=headers, params=params)
            emby_response.raise_for_status()
            status = "available" if emby_response.json().get("Items") else "missing"
        return status, True, False
    except Exception as exc:
        logger.warning("module=media provider=emby operation=library_status error_type=%s reason=%s", type(exc).__name__, exc)
        return None, True, True


async def _emby_episode_status(tmdb_id: int, season: int) -> tuple[bool, bool, set[tuple[int, int]]]:
    cfg = _emby_settings()
    configured = _emby_configured(cfg)
    if not configured:
        return False, False, set()
    emby_episode_keys: set[tuple[int, int]] = set()
    try:
        headers = {"X-Emby-Token": cfg["api_key"]}
        params = {"Recursive": "true", "IncludeItemTypes": "Series", "AnyProviderIdEquals": f"tmdb.{tmdb_id}", "Fields": "ProviderIds", "Limit": 5}
        async with httpx.AsyncClient(timeout=EMBY_TIMEOUT) as client:
            series_response = await client.get(f"{cfg['url']}{_emby_items_path(cfg)}", headers=headers, params=params)
            series_response.raise_for_status()
            series_items = series_response.json().get("Items", [])
            if series_items:
                episode_params = {"Season": season, "Fields": "IndexNumber,ParentIndexNumber"}
                if cfg.get("user_id"):
                    episode_params["UserId"] = cfg["user_id"]
                episode_response = await client.get(f"{cfg['url']}/Shows/{series_items[0]['Id']}/Episodes", headers=headers, params=episode_params)
                episode_response.raise_for_status()
                for emby_item in episode_response.json().get("Items", []):
                    parent = emby_item.get("ParentIndexNumber")
                    index = emby_item.get("IndexNumber")
                    emby_episode_keys.add((int(season if parent is None else parent), int(index if index is not None else 0)))
        return True, False, emby_episode_keys
    except Exception as exc:
        logger.warning("module=media provider=emby operation=episode_status error_type=%s reason=%s", type(exc).__name__, exc)
        return True, True, emby_episode_keys


@app.get("/api/web/media/detail")
async def web_media_detail(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(0, ge=0), title: str = Query("", max_length=200), year: str = Query("", max_length=4)) -> dict[str, Any]:
    resolution: dict[str, Any] = {}
    if not tmdb_id and title.strip():
        resolution = await resolve_tmdb_id(media_type, title, year)
        tmdb_id = int(resolution.get("tmdb_id") or 0)
    if not tmdb_id:
        return {"data": None, "seasons": [], "configured": False, "resolution": resolution}
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {"data": None, "seasons": [], "configured": False, "resolution": resolution}
    payload, cached = await tmdb.request(f"/{media_type}/{tmdb_id}", {"append_to_response": "credits"}, ttl=3600)
    item = tmdb.normalize(payload, media_type)
    item.update({
        "genres": [x.get("name") for x in payload.get("genres", []) if x.get("name")],
        "countries": [x.get("name") for x in payload.get("production_countries", []) if x.get("name")],
        "status": payload.get("status") or "",
        "actors": [x.get("name") for x in payload.get("credits", {}).get("cast", [])[:10] if x.get("name")],
        "directors": [x.get("name") for x in payload.get("credits", {}).get("crew", []) if x.get("job") in ("Director", "Series Director") and x.get("name")][:4],
    })
    seasons = [{"season_number": x.get("season_number"), "name": x.get("name"), "episode_count": x.get("episode_count") or 0, "air_date": x.get("air_date") or "", "poster": f"https://image.tmdb.org/t/p/w342{x.get('poster_path')}" if x.get("poster_path") else ""} for x in payload.get("seasons", []) if int(x.get("season_number") or 0) >= 0]
    emby_status, emby_configured, emby_error = await _emby_library_status(media_type, tmdb_id)
    return {"data": item, "seasons": seasons, "configured": True, "cached": cached, "resolved_tmdb_id": tmdb_id, "resolution": resolution, "emby_status": emby_status, "emby_configured": emby_configured, "emby_error": emby_error}

@app.get("/api/web/media/season")
async def web_media_season(tmdb_id: int = Query(..., gt=0), season: int = Query(..., ge=0)) -> dict[str, Any]:
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        raise HTTPException(409, "TMDB 尚未配置")
    payload, cached = await tmdb.request(f"/tv/{tmdb_id}/season/{season}", ttl=3600)
    emby_configured, emby_error, emby_episode_keys = await _emby_episode_status(tmdb_id, season)
    episodes = []
    for x in payload.get("episodes", []):
        key = (int(x.get("season_number") or season), int(x.get("episode_number") or 0))
        if not emby_configured:
            emby_status = "unconfigured"
        elif emby_error:
            emby_status = "unknown"
        else:
            emby_status = "available" if key in emby_episode_keys else "missing"
        episodes.append({"episode_number": x.get("episode_number"), "season_number": x.get("season_number", season), "name": x.get("name") or "未命名", "overview": x.get("overview") or "", "air_date": x.get("air_date") or "", "still": f"https://image.tmdb.org/t/p/w500{x.get('still_path')}" if x.get("still_path") else "", "emby_status": emby_status})
    total = len(episodes)
    available = sum(1 for e in episodes if e["emby_status"] == "available")
    stats: dict[str, Any] | None = None
    if emby_configured and not emby_error:
        stats = {"total": total, "available": available, "missing": total - available, "completed": total > 0 and available == total}
    return {"items": episodes, "cached": cached, "emby_configured": emby_configured, "emby_error": emby_error, "stats": stats}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Small personal dashboard; the full MoviePilot UI remains separate."""
    try:
        row = load_installation(WEB_INSTALLATION_ID)
        user = json.loads(row["user_json"] or "{}")
        account = html.escape(str(user.get("nickname") or user.get("username") or "已授权"))
        points = html.escape(str(user.get("points") or "未知"))
        state = f"<div class='ok'>已授权：{account}　积分：{points}</div>"
    except HTTPException:
        state = "<div class='warn'>尚未授权影巢账号</div>"
    expires = int(time.time()) + 600
    signature = sign_start(WEB_INSTALLATION_ID, expires)
    login_url = f"/oauth/start?installation_id={quote(WEB_INSTALLATION_ID)}&expires={expires}&signature={quote(signature)}"
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>115 网盘转存助手</title><head>{theme_head()}</head>
<style>body{{margin:0;background:var(--bg-primary);color:var(--text-primary);font:16px system-ui}}main{{max-width:760px;margin:8vh auto;padding:36px;background:var(--card-bg);border:1px solid var(--border-primary);border-radius:18px}}h1{{color:var(--gold)}}.ok,.warn{{padding:16px;border-radius:10px;margin:20px 0}}.ok{{background:var(--success-bg);color:var(--success)}}.warn{{background:var(--warning-bg);color:var(--warning)}}a{{display:inline-block;background:var(--accent-primary);color:var(--button-primary-text);padding:12px 18px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0}}small{{color:var(--text-muted)}}</style>
<nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a></nav><main><h1>115 网盘转存助手</h1><p>个人版影巢授权中心</p>{state}<a href='{login_url}'>授权影巢账号</a><a href='/health'>检查服务</a>
<h2>资源查询</h2><form method='post' action='/web/query'><input name='media_type' value='movie' placeholder='movie 或 tv'><input name='tmdb_id' type='number' placeholder='TMDB ID' required><button>查询影巢资源</button></form>
<h2>资源解锁</h2><form method='post' action='/web/resolve'><input name='slug' placeholder='影巢资源 slug' required><input name='max_unlock_points' type='number' value='0' min='0'><button>解锁并获取 115 链接</button></form>
<p><small>当前仅供个人使用；授权 Token 保存在服务器，不会显示或提交到 GitHub。解锁后请在 MoviePilot 中执行现有 115 转存。</small></p></main>""")


def _web_layout(title: str, content: str) -> HTMLResponse:
    nav = "<nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a><a href='/web/logs'>日志中心</a></nav>"
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><head>{theme_head()}</head><style>
body{{margin:0;background:var(--bg-primary);color:var(--text-primary);font:15px system-ui}}nav{{padding:18px 28px;background:var(--sidebar-bg);border-bottom:1px solid var(--border-primary)}}nav a{{color:var(--gold);margin-right:22px;text-decoration:none}}main{{max-width:1250px;margin:28px auto;padding:28px;background:var(--card-bg);border:1px solid var(--border-primary);border-radius:16px}}h1{{color:var(--gold)}}h2{{color:var(--gold)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}.card{{padding:20px;background:var(--surface-secondary);border:1px solid var(--border-primary);border-radius:12px}}.muted{{color:var(--text-muted)}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;border-bottom:1px solid var(--border-secondary);text-align:left}}input,select,button{{padding:10px;margin:5px;background:var(--input-bg);color:var(--text-primary);border:1px solid var(--input-border);border-radius:6px}}button{{color:var(--accent-primary);cursor:pointer}}a.btn{{display:inline-block;padding:10px 14px;background:var(--accent-primary);color:var(--button-primary-text);border-radius:6px;text-decoration:none}}</style>{nav}<main><h1>{html.escape(title)}</h1>{content}</main>""")


def _web_account_card() -> str:
    try:
        row = load_installation(WEB_INSTALLATION_ID)
        user = json.loads(row["user_json"] or "{}")
        return f"<div class='card'>Authorized: <b>{html.escape(str(user.get('nickname') or user.get('username') or 'account'))}</b>　Points: <b>{html.escape(str(user.get('points') or 'unknown'))}</b></div>"
    except HTTPException:
        return "<div class='card'>Not authorized. <a class='btn' href='/oauth/login'>Authorize HDHive</a></div>"


@app.get("/rankings", response_class=HTMLResponse)
def rankings_page() -> HTMLResponse:
    return HTMLResponse(rankings_html())


@app.get("/web/rankings", response_class=HTMLResponse)
async def web_rankings() -> HTMLResponse:
    try:
        data = await fetch_tmdb_rankings(WEB_INSTALLATION_ID)
        items = data.get("items") or []
        ranking_html = "<div class='grid'>" + "".join(
            f"<div class='card'><img src='{html.escape(str(item.get('poster') or ''))}' style='width:100%;max-height:330px;object-fit:cover;border-radius:8px' loading='lazy'><h2>{index + 1}. {html.escape(str(item.get('title') or '未命名'))}</h2><p>评分：{html.escape(str(item.get('rating') or '-'))}　年份：{html.escape(str(item.get('year') or '-'))}</p><form method='post' action='/web/query'><input type='hidden' name='media_type' value='{item.get('media_type')}'><input type='hidden' name='tmdb_id' value='{item.get('tmdb_id')}'><button>查看影巢资源</button></form></div>"
            for index, item in enumerate(items[:20])
        ) + "</div>"
    except HTTPException as exc:
        ranking_html = f"<p class='muted'>无法读取 TMDB 榜单：{html.escape(str(exc.detail))}</p><a class='btn' href='/web/settings'>前往设置 TMDB API Key</a>"
    content = _web_account_card() + f"<div class='card'><h2>TMDB 热门影视榜单</h2>{ranking_html}</div><div class='card'><h2>按名称搜索影巢资源</h2><form method='post' action='/web/search'><input name='keyword' placeholder='例如：庆余年' required><select name='media_type'><option value='movie'>电影</option><option value='tv'>电视剧</option></select><button>搜索</button></form></div>"
    return _web_layout("热门榜单与资源搜索", content)


@app.get("/web/discover", response_class=HTMLResponse)
def web_discover() -> HTMLResponse:
    content = _web_account_card() + "<div class='card'><h2>汇影资源发现</h2><p>按影视名称直接搜索，接口返回匹配的影巢资源。</p><form method='post' action='/web/search'><input name='keyword' placeholder='影视名称' required><select name='media_type'><option value='movie'>电影</option><option value='tv'>电视剧</option></select><button>搜索影巢资源</button></form><hr><p class='muted'>进阶：需要精确匹配时可使用 TMDB ID 查询。</p><form method='post' action='/web/query'><select name='media_type'><option value='movie'>电影</option><option value='tv'>电视剧</option></select><input name='tmdb_id' type='number' placeholder='TMDB ID' required><button>精确查询</button></form></div>"
    return _web_layout("资源发现", content)


@app.get("/subscriptions", response_class=HTMLResponse)
@app.get("/web/subscriptions", response_class=HTMLResponse)
def web_subscriptions() -> HTMLResponse:
    return HTMLResponse(subscriptions_html())


@app.get("/unlocks", response_class=HTMLResponse)
@app.get("/web/unlocks", response_class=HTMLResponse)
def web_unlocks() -> HTMLResponse:
    return HTMLResponse(unlocks_html())


@app.get("/tasks", response_class=HTMLResponse)
@app.get("/web/tasks", response_class=HTMLResponse)
def web_tasks() -> HTMLResponse:
    return HTMLResponse(tasks_html())


@app.get("/logs", response_class=HTMLResponse)
@app.get("/web/logs", response_class=HTMLResponse)
def web_logs_page() -> HTMLResponse:
    return HTMLResponse(logs_html())


@app.get("/web/settings-legacy", response_class=HTMLResponse)
def web_settings() -> HTMLResponse:
    with database() as conn:
        row = conn.execute("SELECT moviepilot_url, save_directory, offline_enabled, tmdb_api_key, tmdb_language, tmdb_region FROM web_settings WHERE installation_id = ?", (WEB_INSTALLATION_ID,)).fetchone()
    settings = dict(row) if row else {"moviepilot_url": "", "save_directory": "", "offline_enabled": 1, "tmdb_api_key": "", "tmdb_language": "zh-CN", "tmdb_region": "CN"}
    key_hint = "已配置，留空表示不修改" if settings.get("tmdb_api_key") else "填写 TMDB API Read Access Token 或 API Key"
    content = _web_account_card() + f"<div class='card'><h2>TMDB 榜单配置</h2><form method='post' action='/web/settings'><label>TMDB API Key</label><br><input type='password' name='tmdb_api_key' value='' size='60' placeholder='{key_hint}'><br><label>语言</label><input name='tmdb_language' value='{html.escape(str(settings['tmdb_language']))}'><label>地区</label><input name='tmdb_region' value='{html.escape(str(settings['tmdb_region']))}'><hr><h2>转存配置</h2><label>MoviePilot 地址</label><br><input name='moviepilot_url' value='{html.escape(str(settings['moviepilot_url']))}' size='60'><br><label>115 保存目录</label><br><input name='save_directory' value='{html.escape(str(settings['save_directory']))}' size='60'><br><label><input type='checkbox' name='offline_enabled' {'checked' if settings['offline_enabled'] else ''}> 启用磁力和 ed2k 离线下载</label><br><button>保存全部设置</button></form></div>"
    return _web_layout("设置", content)


@app.get("/settings", response_class=HTMLResponse)
@app.get("/web/settings", response_class=HTMLResponse)
def business_settings_page() -> HTMLResponse:
    return HTMLResponse(settings_html())


@app.get("/authorizations", response_class=HTMLResponse)
def authorizations_page() -> HTMLResponse:
    return HTMLResponse(authorizations_html())


@app.get("/telegram", response_class=HTMLResponse)
def telegram_page() -> HTMLResponse:
    return HTMLResponse(telegram_html())


@app.post("/web/settings", response_class=HTMLResponse)
def web_settings_save(moviepilot_url: str = Form(""), save_directory: str = Form(""), offline_enabled: str = Form(""), tmdb_api_key: str = Form(""), tmdb_language: str = Form("zh-CN"), tmdb_region: str = Form("CN")) -> HTMLResponse:
    put_settings(WebSettings(moviepilot_url=moviepilot_url, save_directory=save_directory, offline_enabled=bool(offline_enabled), tmdb_api_key=tmdb_api_key, tmdb_language=tmdb_language, tmdb_region=tmdb_region), WEB_INSTALLATION_ID)
    return RedirectResponse("/web/settings", status_code=303)


@app.get("/web/{section}", response_class=HTMLResponse)
def web_section(section: str) -> HTMLResponse:
    labels = {
        "rankings": ("榜单", "后续接入 TMDB、豆瓣及流媒体榜单。"),
        "discover": ("资源发现", "按类型、风格、地区和年份筛选电影与电视剧。"),
        "subscriptions": ("订阅列表", "管理 MoviePilot 和影巢订阅。"),
        "tasks": ("订阅任务", "查看定时任务、成功、失败和跳过记录。"),
        "unlocks": ("解锁记录", "查看影巢解锁、积分和 115 保存结果。"),
        "settings": ("设置", "配置影巢、115、MoviePilot、Emby、TMDB 和转存策略。"),
    }
    title, description = labels.get(section, ("页面不存在", ""))
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>{title}</title><head>{theme_head()}</head><style>body{{margin:0;background:var(--bg-primary);color:var(--text-primary);font:16px system-ui}}nav{{padding:14px;background:var(--sidebar-bg);border-bottom:1px solid var(--border-primary)}}nav a{{color:var(--gold);margin-right:18px;text-decoration:none}}main{{max-width:1100px;margin:40px auto;padding:32px;background:var(--card-bg);border:1px solid var(--border-primary);border-radius:18px}}h1{{color:var(--gold)}}.card{{padding:24px;background:var(--surface-secondary);border-radius:12px}}</style><nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a></nav><main><h1>{html.escape(title)}</h1><div class='card'>{html.escape(description)}<br><br>该模块正在接入现有 MoviePilot、影巢和 115 数据。</div></main>""")


@app.get("/oauth/login")
def oauth_login() -> RedirectResponse:
    expires = int(time.time()) + 600
    signature = sign_start(WEB_INSTALLATION_ID, expires)
    return RedirectResponse(
        f"/oauth/start?installation_id={quote(WEB_INSTALLATION_ID)}&expires={expires}&signature={quote(signature)}"
    )


def _result_page(title: str, payload: Any, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<meta charset='utf-8'><head>{theme_head()}</head><style>body{{background:var(--bg-primary);color:var(--text-primary);font:16px system-ui;padding:30px}}pre{{white-space:pre-wrap;background:var(--surface-secondary);padding:18px;border-radius:10px}}a{{color:var(--gold)}}</style><h2>{html.escape(title)}</h2><pre>{html.escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))}</pre><a href='/'>返回首页</a>",
        status_code=status_code,
    )


def _resource_items(payload: Any) -> list[dict[str, Any]]:
    """Accept the response envelopes used by different HDHive API versions."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "resources", "list", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = payload.get("data")
    return _resource_items(nested) if nested is not payload else []


def _resource_page(title: str, payload: Any, status_code: int = 200, media_type: str = "", tmdb_id: int = 0, year: str = "", poster: str = "") -> HTMLResponse:
    safe_title = html.escape(title or "该影视")
    items = _resource_items(payload)
    cards: list[str] = []
    for item in items:
        name = html.escape(str(item.get("title") or item.get("name") or title or "影巢资源"))
        slug = str(item.get("slug") or item.get("resource_slug") or "").strip()
        quality = html.escape(str(item.get("quality") or item.get("resolution") or ""))
        size = html.escape(str(item.get("size") or item.get("file_size") or ""))
        points = html.escape(str(item.get("unlock_points") or item.get("points") or ""))
        details = " · ".join(value for value in (quality, size, f"{points} 积分" if points else "") if value)
        button = ""
        if re.fullmatch(r"[A-Za-z0-9-]+", slug):
            button = (
                "<form method='post' action='/web/resolve'>"
                f"<input type='hidden' name='slug' value='{html.escape(slug, quote=True)}'>"
                "<input type='hidden' name='max_unlock_points' value='0'>"
                "<button>解锁并获取 115 链接</button></form>"
            )
        cards.append(f"<article><h3>{name}</h3><p>{details or '影巢资源'}</p>{button}</article>")
    if cards:
        body = f"<p>已找到 {len(cards)} 条影巢资源，选择需要的版本：</p>" + "".join(cards)
    else:
        body = f"<div class='empty'><h3>影巢暂时没有《{safe_title}》的可用资源</h3><p>这不是程序报错，只是影巢当前没有返回匹配链接。可以稍后再试，或返回汇影选择其他影片。</p></div>"
    subscribe = ""
    if media_type in ("movie", "tv") and tmdb_id > 0:
        subscribe = ("<form method='post' action='/web/subscribe' style='display:inline-block;margin-right:10px'>"
                     f"<input type='hidden' name='title' value='{html.escape(title, quote=True)}'><input type='hidden' name='media_type' value='{media_type}'>"
                     f"<input type='hidden' name='tmdb_id' value='{tmdb_id}'><input type='hidden' name='year' value='{html.escape(year, quote=True)}'>"
                     f"<input type='hidden' name='poster' value='{html.escape(poster, quote=True)}'><button>＋ 订阅这部影视</button></form>")
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{safe_title} - 影巢资源</title><head>{theme_head()}</head><style>body{{margin:0;background:var(--bg-primary);color:var(--text-primary);font:16px system-ui,'Microsoft YaHei';padding:28px}}main{{max-width:920px;margin:auto}}h1,h3{{color:var(--gold)}}article,.empty{{background:var(--card-bg);border:1px solid var(--border-primary);border-radius:14px;padding:18px;margin:14px 0}}button,.back{{display:inline-block;border:1px solid var(--accent-primary);border-radius:9px;background:var(--button-secondary-bg);color:var(--accent-primary);padding:10px 16px;text-decoration:none;cursor:pointer}}p{{color:var(--text-secondary);line-height:1.7}}</style>"
        f"<main><h1>《{safe_title}》影巢资源</h1>{body}{subscribe}<a class='back' href='/explore'>返回汇影</a></main></html>",
        status_code=status_code,
    )


@app.post("/web/query", response_class=HTMLResponse)
async def web_query(media_type: str = Form(...), tmdb_id: int = Form(...), title: str = Form(""), year: str = Form(""), poster: str = Form("")) -> HTMLResponse:
    try:
        data = await query_resources(ResourceQuery(media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        if not _resource_items(data) and title.strip():
            try:
                searched = await search_resources(SearchRequest(keyword=title.strip(), media_type=media_type), WEB_INSTALLATION_ID)
                if _resource_items(searched):
                    data = searched
            except HTTPException:
                pass
        return _resource_page(title.strip() or f"TMDB {tmdb_id}", data, media_type=media_type, tmdb_id=tmdb_id, year=year, poster=poster)
    except HTTPException as exc:
        return _resource_page(title.strip() or f"TMDB {tmdb_id}", {}, exc.status_code, media_type, tmdb_id, year, poster)


@app.post("/web/search", response_class=HTMLResponse)
async def web_search(keyword: str = Form(...), media_type: str = Form("movie")) -> HTMLResponse:
    try:
        data = await search_resources(SearchRequest(keyword=keyword, media_type=media_type), WEB_INSTALLATION_ID)
        return _result_page("HDHive name search", data)
    except HTTPException as exc:
        return _result_page("Search failed", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)


@app.post("/web/subscribe")
def web_subscribe(title: str = Form(...), media_type: str = Form(...), tmdb_id: int = Form(...), year: str = Form(""), poster: str = Form("")) -> RedirectResponse:
    try:
        parsed_year = int(year) if year.isdigit() else None
        create_subscription(SubscriptionRequest(title=title, media_type=media_type, tmdb_id=tmdb_id, year=parsed_year, poster=poster), WEB_INSTALLATION_ID)
        return RedirectResponse("/subscriptions", status_code=303)
    except HTTPException as exc:
        raise exc


@app.post("/web/resolve", response_class=HTMLResponse)
async def web_resolve(slug: str = Form(...), max_unlock_points: int = Form(0)) -> HTMLResponse:
    try:
        data = await resolve_resource(ResolveRequest(slug=slug, max_unlock_points=max_unlock_points), WEB_INSTALLATION_ID)
        return _result_page("解锁结果（请回到 MoviePilot 转存）", data)
    except HTTPException as exc:
        return _result_page("解锁失败", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)


@app.get("/oauth/start")
def oauth_start(
    installation_id: str = Query(...),
    expires: int = Query(...),
    signature: str = Query(...),
):
    installation_id = valid_installation_id(installation_id)
    now = int(time.time())
    if expires < now or expires > now + 900:
        raise HTTPException(400, "Authorization link expired")
    if not hmac.compare_digest(signature, sign_start(installation_id, expires)):
        raise HTTPException(401, "Invalid authorization link")
    state = secrets.token_urlsafe(32)
    with database() as conn:
        conn.execute(
            "INSERT INTO oauth_states(state, installation_id, expires_at) VALUES (?, ?, ?)",
            (state, installation_id, now + STATE_TTL),
        )
        conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": OAUTH_SCOPE,
        "state": state,
        "response_mode": "redirect",
    }
    return RedirectResponse(f"{BASE_URL}/openapi/authorize?{urlencode(params)}")


@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(
    code: str = Query(""), state: str = Query(""), error: str = Query("")
):
    if error:
        return HTMLResponse(f"<h2>影巢授权失败</h2><p>{html.escape(error)}</p>", 400)
    now = int(time.time())
    with database() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_states WHERE state = ?", (state,)
        ).fetchone()
        if not row or row["used_at"] or int(row["expires_at"]) < now:
            return HTMLResponse("<h2>授权链接无效或已经使用</h2>", 400)
        conn.execute(
            "UPDATE oauth_states SET used_at = ? WHERE state = ?", (now, state)
        )
    try:
        token_data = await hdhive_request(
            "POST",
            "/api/public/openapi/oauth/token",
            body={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
        user = await save_tokens(row["installation_id"], token_data)
    except HDHiveAPIError as exc:
        return HTMLResponse(
            f"<h2>影巢授权失败</h2><p>{html.escape(exc.code)}: {html.escape(exc.message)}</p>",
            exc.status,
        )
    name = html.escape(str(user.get("nickname") or user.get("username") or "当前账号"))
    return HTMLResponse(
        f"<h2>影巢授权成功</h2><p>已授权账号：{name}</p><p>可以关闭此页面，返回MoviePilot。</p>"
    )


@app.get("/v1/status")
def status(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    try:
        row = load_installation(installation_id)
    except HTTPException:
        return {"authorized": False}
    user = json.loads(row["user_json"] or "{}")
    return {
        "authorized": True,
        "user": {
            "id": user.get("id"),
            "nickname": user.get("nickname") or user.get("username"),
            "points": user.get("points"),
        },
        "updated_at": row["updated_at"],
    }


@app.get("/v1/dashboard")
def dashboard_status(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    """Return the small set of data needed by a personal web dashboard."""
    try:
        row = load_installation(installation_id)
    except HTTPException:
        return {"authorized": False, "installation_id": installation_id}
    user = json.loads(row["user_json"] or "{}")
    with database() as conn:
        transfers = conn.execute(
            "SELECT COUNT(*) FROM transfer_records WHERE installation_id = ?", (installation_id,)
        ).fetchone()[0]
        subscriptions = conn.execute(
            "SELECT COUNT(*) FROM web_subscriptions WHERE installation_id = ? AND status = 'active'",
            (installation_id,),
        ).fetchone()[0]
    return {
        "authorized": True,
        "installation_id": installation_id,
        "user": {"id": user.get("id"), "nickname": user.get("nickname") or user.get("username"), "points": user.get("points")},
        "updated_at": row["updated_at"],
        "transfer_count": transfers,
        "subscription_count": subscriptions,
    }


class ResourceQuery(BaseModel):
    media_type: str = Field(pattern="^(movie|tv)$")
    tmdb_id: int = Field(gt=0)


@app.post("/v1/resources/query")
async def query_resources(
    request: ResourceQuery,
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    try:
        return await authorized_request(
            installation_id,
            "GET",
            f"/api/open/resources/{quote(request.media_type)}/{request.tmdb_id}",
        )
    except HDHiveAPIError as exc:
        raise HTTPException(exc.status, f"{exc.code}: {exc.message}") from exc


class ResolveRequest(BaseModel):
    resource_url: str = ""
    slug: str = ""
    max_unlock_points: int | None = Field(default=None, ge=0)


@app.post("/v1/resources/resolve")
async def resolve_resource(
    request: ResolveRequest,
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    slug = request.slug.strip()
    if not slug and request.resource_url:
        match = RESOURCE_RE.match(request.resource_url.strip())
        slug = match.group(1) if match else ""
    if not re.fullmatch(r"[A-Za-z0-9-]+", slug):
        raise HTTPException(400, "Invalid HDHive resource slug")
    detail: dict[str, Any] = {}
    url = ""
    password = ""
    files: list[Any] = []
    already_owned = False
    try:
        detail = await authorized_request(
            installation_id, "GET", f"/api/open/shares/{quote(slug)}"
        )
        unlocked = bool(detail.get("is_unlocked"))
        points = int(
            detail.get("actual_unlock_points")
            if detail.get("actual_unlock_points") is not None
            else detail.get("unlock_points") or 0
        )
        if request.max_unlock_points is not None and not unlocked and points > request.max_unlock_points:
            raise HTTPException(
                409,
                f"UNLOCK_BUDGET_EXCEEDED: need {points}, limit {request.max_unlock_points}",
            )
        # 已解锁且本地已保存真实链接 → 直接复用，不再调用解锁接口，避免重复扣积分
        reuse_url = ""
        with database() as conn:
            row = conn.execute(
                "SELECT share_url FROM transfer_records WHERE installation_id=? AND slug=? AND share_url!='' ORDER BY id DESC LIMIT 1",
                (installation_id, slug),
            ).fetchone()
            if row:
                reuse_url = str(row["share_url"] or "")
        if unlocked and reuse_url:
            url = reuse_url
            already_owned = True
            logger.info("module=resources provider=hdhive operation=resolve reuse=true already_unlocked=true slug=%s", slug)
        else:
            data = await authorized_request(
                installation_id,
                "POST",
                "/api/open/resources/unlock",
                body={"slug": slug},
            )
            url = str(data.get("full_url") or data.get("url") or "").strip()
            password = str(data.get("access_code") or "")
            files = data.get("files") if isinstance(data.get("files"), list) else []
            already_owned = bool(data.get("already_owned"))
            logger.info("module=resources provider=hdhive operation=unlock success slug=%s link_type=%s already_owned=%s", slug, classify_share_link(url), already_owned)
    except HDHiveAPIError as exc:
        logger.warning("module=resources provider=hdhive operation=resolve error_type=HDHiveAPIError code=%s reason=%s", exc.code, exc.message)
        raise HTTPException(exc.status, f"{exc.code}: {exc.message}") from exc
    name = str(detail.get("title") or detail.get("name") or slug)
    user = detail.get("user") if isinstance(detail.get("user"), dict) else {}
    uploader = str(user.get("nickname") or user.get("username") or user.get("name") or "")
    now = int(time.time())
    source_type = humanize_pan_type(detail.get("pan_type")) or "其他来源"
    with database() as conn:
        existing = conn.execute(
            "SELECT id,transfer_status FROM transfer_records WHERE installation_id=? AND slug=? ORDER BY id DESC LIMIT 1",
            (installation_id, slug),
        ).fetchone()
        if existing:
            prev_transfer = str(existing["transfer_status"] or "pending")
            transfer_s = prev_transfer if prev_transfer in ("success", "processing", "failed") else "pending"
            status_s = "completed" if transfer_s == "success" else "resolved"
            conn.execute(
                """UPDATE transfer_records SET name=?,share_url=?,status=?,resolution=?,quality=?,size=?,uploader=?,points=?,action=?,processing_status=?,source_type=?,unlock_status='unlocked',transfer_status=?,updated_at=? WHERE id=?""",
                (name, url, status_s,
                 _list_text(detail.get("video_resolution")), _list_text(detail.get("source")),
                 str(detail.get("share_size") or detail.get("size") or ""), uploader,
                 int(detail.get("unlock_points") or 0), "资源解锁",
                 "completed" if transfer_s == "success" else "resolved",
                 source_type, transfer_s, now, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO transfer_records
                   (installation_id,slug,name,share_url,status,resolution,quality,size,uploader,points,action,processing_status,source_type,unlock_status,transfer_status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (installation_id, slug, name, url, "resolved",
                 _list_text(detail.get("video_resolution")), _list_text(detail.get("source")),
                 str(detail.get("share_size") or detail.get("size") or ""), uploader,
                 int(detail.get("unlock_points") or 0), "资源解锁", "resolved", source_type,
                 "unlocked", "pending", now, now),
            )
    return {
        "name": name,
        "share_url": url,
        "password": password,
        "files": files,
        "slug": slug,
        "already_owned": already_owned,
        "is_unlocked": bool(detail.get("is_unlocked")),
        "link_type": classify_share_link(url),
        "pan_type": str(detail.get("pan_type") or ""),
        "uploader": uploader,
        "points": int(detail.get("unlock_points") or 0),
    }


class SubscriptionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    media_type: str = Field(pattern="^(movie|tv)$")
    tmdb_id: int = Field(default=0, ge=0)
    year: int | None = Field(default=None, ge=1880, le=2200)
    poster: str = Field(default="", max_length=1000)
    season: str = Field(default="", max_length=30)
    subscription_scope: str = Field(default="普通订阅", max_length=50)
    category: str = Field(default="", max_length=100)
    save_path: str = Field(default="", max_length=500)
    douban_id: str = Field(default="", max_length=50)
    moviepilot_id: str = Field(default="", max_length=100)


class SearchRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    media_type: str = Field(pattern="^(movie|tv)$")


async def fetch_rankings(installation_id: str) -> dict[str, Any]:
    last: HDHiveAPIError | None = None
    for path in ("/api/open/rankings", "/api/open/resources/rankings", "/api/open/ranking"):
        try:
            return await authorized_request(installation_id, "GET", path)
        except HDHiveAPIError as exc:
            last = exc
            if exc.status not in (404, 405):
                raise
    if last:
        raise HTTPException(last.status, f"{last.code}: {last.message}")
    return {"items": []}


async def fetch_tmdb_rankings(installation_id: str) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT tmdb_api_key, tmdb_language, tmdb_region FROM web_settings WHERE installation_id = ?", (installation_id,)).fetchone()
    if not row or not row["tmdb_api_key"]:
        raise HTTPException(409, "请先在设置页面填写 TMDB API Key")
    api_key = decrypt(row["tmdb_api_key"])
    language = row["tmdb_language"] or "zh-CN"
    region = row["tmdb_region"] or "CN"
    headers = {"Accept": "application/json"}
    params: dict[str, Any] = {"language": language, "region": region}
    if api_key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key
    items: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        for media_type, path in (("movie", "/3/trending/movie/week"), ("tv", "/3/trending/tv/week")):
            try:
                response = await client.get(f"https://api.themoviedb.org{path}", headers=headers, params=params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise HTTPException(502, f"TMDB 请求失败：{exc}") from exc
            for item in payload.get("results", [])[:10]:
                date = str(item.get("release_date") or item.get("first_air_date") or "")
                items.append({"tmdb_id": item.get("id"), "media_type": media_type, "title": item.get("title") or item.get("name"), "rating": item.get("vote_average"), "year": date[:4], "poster": f"https://image.tmdb.org/t/p/w500{item.get('poster_path')}" if item.get("poster_path") else ""})
    items.sort(key=lambda item: float(item.get("rating") or 0), reverse=True)
    return {"items": items}


@app.get("/v1/rankings")
async def rankings(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    return await fetch_rankings(installation_id)


@app.post("/v1/resources/search")
async def search_resources(
    request: SearchRequest,
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    """Fuzzy resource search by title; HDHive owns the matching/indexing logic."""
    try:
        return await authorized_request(
            installation_id,
            "POST",
            "/api/open/resources/search",
            body={"keyword": request.keyword.strip(), "media_type": request.media_type},
        )
    except HDHiveAPIError as exc:
        raise HTTPException(exc.status, f"{exc.code}: {exc.message}") from exc


def resource_registry() -> ResourceProviderRegistry:
    with database() as conn:
        configured = bool(conn.execute("SELECT 1 FROM installations WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone())

    async def hdhive_query(media_type: str, tmdb_id: int, title: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
        data: dict[str, Any] = {"items": []}
        errors: list[dict[str, str]] = []
        if tmdb_id > 0:
            try:
                data = await query_resources(ResourceQuery(media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
            except HTTPException as exc:
                errors.append({"provider": "hdhive", "error": f"按 TMDB ID 查询失败：{exc.detail}"})
                logger.warning("module=resources provider=hdhive operation=tmdb_query error_type=%s reason=%s; falling_back=title", type(exc).__name__, exc.detail)
        if not _resource_items(data) and title.strip():
            try:
                searched = await search_resources(SearchRequest(keyword=title.strip(), media_type=media_type), WEB_INSTALLATION_ID)
                if _resource_items(searched):
                    return searched, errors
            except HTTPException as exc:
                errors.append({"provider": "hdhive", "error": f"标题搜索失败：{exc.detail}"})
                logger.warning("module=resources provider=hdhive operation=title_search error_type=%s reason=%s", type(exc).__name__, exc.detail)
        return data, errors

    return ResourceProviderRegistry([HDHiveResourceProvider(hdhive_query, configured=configured)])


_CACHE_MATCH_SQL = """( (?!=0 AND tmdb_id=?) OR (?=0 AND title=? AND COALESCE(year,'')=COALESCE(?,'')) )"""


def _save_search_cache(media_type: str, tmdb_id: int, douban_id: str, title: str, year: str, result_json: str) -> None:
    now = int(time.time())
    with database() as conn:
        conn.execute(
            f"DELETE FROM resource_search_cache WHERE installation_id=? AND media_type=? AND {_CACHE_MATCH_SQL}",
            (WEB_INSTALLATION_ID, media_type, tmdb_id, tmdb_id, tmdb_id, title, year),
        )
        conn.execute(
            "INSERT INTO resource_search_cache (installation_id,media_type,tmdb_id,douban_id,title,year,result_json,searched_at) VALUES (?,?,?,?,?,?,?,?)",
            (WEB_INSTALLATION_ID, media_type, tmdb_id, douban_id, title, year, result_json, now),
        )
        conn.execute(
            """DELETE FROM resource_search_cache WHERE installation_id=? AND id NOT IN (
                SELECT id FROM resource_search_cache WHERE installation_id=? ORDER BY searched_at DESC LIMIT 30)""",
            (WEB_INSTALLATION_ID, WEB_INSTALLATION_ID),
        )


def _save_search_history(media_type: str, tmdb_id: int, douban_id: str, title: str, year: str, poster: str, result_count: int) -> None:
    now = int(time.time())
    history_title = title or str(tmdb_id)
    with database() as conn:
        row = conn.execute(
            "SELECT id FROM search_history WHERE installation_id=? AND media_type=? AND title=? AND COALESCE(year,'')=? ORDER BY id DESC LIMIT 1",
            (WEB_INSTALLATION_ID, media_type, history_title, year),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE search_history SET tmdb_id=?,douban_id=?,poster=?,result_count=?,searched_at=? WHERE id=?",
                (tmdb_id, douban_id, poster, result_count, now, row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO search_history (installation_id,title,media_type,year,tmdb_id,douban_id,poster,result_count,searched_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (WEB_INSTALLATION_ID, history_title, media_type, year, tmdb_id, douban_id, poster, result_count, now),
            )
        conn.execute(
            """DELETE FROM search_history WHERE installation_id=? AND id NOT IN (
                SELECT id FROM search_history WHERE installation_id=? ORDER BY searched_at DESC LIMIT 20)""",
            (WEB_INSTALLATION_ID, WEB_INSTALLATION_ID),
        )


@app.get("/api/web/resources/search")
async def web_resource_search(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(0, ge=0), title: str = Query("", max_length=200), douban_id: str = Query("", max_length=50), year: str = Query("", max_length=4), poster: str = Query("", max_length=1000)) -> dict[str, Any]:
    if not tmdb_id and not title.strip():
        raise HTTPException(400, "缺少可用于搜索的影视名称")
    resolved_tmdb_id = tmdb_id
    resolution: dict[str, Any] = {}
    if not tmdb_id and title.strip():
        resolution = await resolve_tmdb_id(media_type, title, year)
        resolved_tmdb_id = int(resolution.get("tmdb_id") or 0)
    providers = resource_registry()
    items, errors = await providers.search(media_type, resolved_tmdb_id, title)
    for error in errors:
        logger.error("module=resources provider=%s operation=search error_type=ProviderError reason=%s", error["provider"], error["error"])
    source_counts: dict[str, int] = {}
    for item in items:
        source = item.source_type or "其他来源"
        source_counts[source] = source_counts.get(source, 0) + 1
    with database() as conn:
        settings = conn.execute("SELECT save_directory,save_folder_id FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    save_path = str(settings["save_directory"] or "") if settings else ""
    save_folder_id = str(settings["save_folder_id"] or "") if settings else ""
    payload = {"items": [item.model_dump() for item in items], "filters": filter_options(items), "providers": providers.infos(), "errors": errors, "total": len(items), "source_counts": source_counts, "save_path": save_path, "save_folder_id": save_folder_id, "resolved_tmdb_id": resolved_tmdb_id or None, "resolution": resolution, "match": {"media_type": media_type, "tmdb_id": tmdb_id or None, "douban_id": douban_id or None, "title": title, "year": year}, "searched_at": int(time.time()), "cached": False}
    _save_search_cache(media_type, resolved_tmdb_id, douban_id, title, year, json.dumps(payload, ensure_ascii=False, default=str))
    poster = ""
    for item in items:
        if item.uploader_avatar:
            poster = item.uploader_avatar
    _save_search_history(media_type, resolved_tmdb_id, douban_id, title, year, poster, len(items))
    return payload


@app.get("/api/web/resources/search/cached")
def web_resource_search_cached(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(0, ge=0), douban_id: str = Query("", max_length=50), title: str = Query("", max_length=200), year: str = Query("", max_length=4)) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute(
            f"SELECT * FROM resource_search_cache WHERE installation_id=? AND media_type=? AND {_CACHE_MATCH_SQL} ORDER BY searched_at DESC, id DESC LIMIT 1",
            (WEB_INSTALLATION_ID, media_type, tmdb_id, tmdb_id, tmdb_id, title, year),
        ).fetchone()
    if not row:
        return {"cached": False}
    payload = json.loads(row["result_json"])
    payload["cached"] = True
    payload["searched_at"] = int(row["searched_at"])
    return payload


@app.get("/api/web/search-history")
def web_search_history(limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
    with database() as conn:
        rows = conn.execute(
            "SELECT id,title,media_type,year,tmdb_id,douban_id,poster,result_count,searched_at FROM search_history WHERE installation_id=? ORDER BY searched_at DESC, id DESC LIMIT ?",
            (WEB_INSTALLATION_ID, limit),
        ).fetchall()
    return {"items": [dict(x) for x in rows]}


@app.delete("/api/web/search-history/{history_id}")
def web_search_history_delete(history_id: int) -> dict[str, Any]:
    with database() as conn:
        cur = conn.execute("DELETE FROM search_history WHERE installation_id=? AND id=?", (WEB_INSTALLATION_ID, history_id))
    if not cur.rowcount:
        raise HTTPException(404, "搜索记录不存在")
    return {"ok": True}


@app.delete("/api/web/search-history")
def web_search_history_clear() -> dict[str, Any]:
    with database() as conn:
        cur = conn.execute("DELETE FROM search_history WHERE installation_id=?", (WEB_INSTALLATION_ID,))
    return {"ok": True, "deleted": cur.rowcount}


class WebResourceTransfer(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    resource_id: str = Field(min_length=1, max_length=200)


async def _execute_transfer(result: dict[str, Any], resource_id: str) -> dict[str, Any]:
    """解锁已完成，执行真实115转存并更新 unlock/transfer 状态。"""
    share_url = str(result.get("share_url") or "").strip()
    link_type = classify_share_link(share_url)
    if link_type == "empty":
        raise HTTPException(502, "影巢解锁成功但未返回分享链接，请稍后重试")
    if link_type == "unsupported":
        logger.info("module=resources provider=hdhive operation=transfer unsupported_link=true slug=%s link_domain=%s", resource_id, _link_domain(share_url))
        raise HTTPException(400, "当前来源暂不支持直接转存115（影巢已解锁并保留原链接，可在解锁记录中查看）")
    with database() as conn:
        settings = conn.execute("SELECT p115_cookie,save_directory,save_folder_id,ed2k_directory FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not settings or not settings["p115_cookie"]:
        raise HTTPException(409, "影巢资源已解锁，但115尚未授权；请到授权中心配置 Cookie 后重试")
    client = P115Client(decrypt(settings["p115_cookie"]), REQUEST_TIMEOUT)
    logger.info("module=resources provider=115 operation=transfer_start link_type=%s slug=%s", link_type, resource_id)
    target_cid = str(settings["save_folder_id"] or "")
    save_path = ""
    transferred: dict[str, Any] = {}
    try:
        if link_type == "115":
            transferred = await client.transfer(share_url, target_cid)
        else:
            save_path = settings["ed2k_directory"] or ""
            offline = await client.offline(share_url, save_path)
            transferred = {**offline, "message": "已提交115离线下载任务" if offline.get("ok") else offline.get("message")}
    except P115Error as exc:
        logger.error("module=resources provider=115 operation=transfer error_type=P115Error reason=%s", exc)
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='failed',processing_status='failed',unlock_status='unlocked',transfer_status='failed',error=?,updated_at=? WHERE installation_id=? AND slug=? AND id=(SELECT MAX(id) FROM transfer_records WHERE installation_id=? AND slug=?)",
                         (str(exc), int(time.time()), WEB_INSTALLATION_ID, resource_id, WEB_INSTALLATION_ID, resource_id))
        try:
            if _telegram_settings(False)["enabled"]:
                await notification_service().notify("transfer_failed", {"title": result.get("name", resource_id), "status": "转存失败", "resource": resource_id, "resolution": "", "size": "", "save_path": "", "error": str(exc)})
        except Exception:
            logger.exception("module=notifications provider=telegram operation=transfer_failed")
        raise
    logger.info("module=resources provider=115 operation=transfer_success link_type=%s slug=%s", link_type, resource_id)
    if link_type == "115":
        save_path = target_cid or settings["save_directory"] or ""
    with database() as conn:
        conn.execute("UPDATE transfer_records SET status='completed',processing_status='completed',unlock_status='unlocked',transfer_status='success',save_path=?,error='',updated_at=? WHERE installation_id=? AND slug=? AND id=(SELECT MAX(id) FROM transfer_records WHERE installation_id=? AND slug=?)",
                     (save_path, int(time.time()), WEB_INSTALLATION_ID, resource_id, WEB_INSTALLATION_ID, resource_id))
    try:
        service = notification_service()
        config = _telegram_settings(False)
        if config["enabled"]:
            await service.notify("transfer_success", {"title": result["name"], "status": "转存成功", "resource": resource_id, "resolution": "", "size": "", "save_path": save_path or "115根目录", "error": ""})
    except Exception:
        logger.exception("module=notifications provider=telegram operation=transfer_success")
    return {**result, **transferred, "provider": "hdhive", "transfer_status": "success", "link_type": link_type, "save_path": save_path}


@app.post("/api/web/resources/transfer")
async def web_resource_transfer(request: WebResourceTransfer) -> dict[str, Any]:
    if request.provider != "hdhive":
        raise HTTPException(400, "该资源来源暂不支持转存")
    try:
        result = await resolve_resource(ResolveRequest(slug=request.resource_id, max_unlock_points=None), WEB_INSTALLATION_ID)
        return await _execute_transfer(result, request.resource_id)
    except P115Error as exc:
        raise HTTPException(502, str(exc)) from exc
    except HTTPException as exc:
        logger.error("module=resources provider=%s operation=transfer error_type=%s reason=%s", request.provider, type(exc).__name__, exc.detail)
        raise


@app.get("/api/web/subscription-status")
def web_subscription_status(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(0, ge=0), douban_id: str = Query("", max_length=50), title: str = Query("", max_length=200), year: int | None = Query(None, ge=1880, le=2200)) -> dict[str, Any]:
    with database() as conn:
        if tmdb_id:
            row = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND tmdb_id=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (WEB_INSTALLATION_ID, media_type, tmdb_id)).fetchone()
        elif douban_id.strip():
            row = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND douban_id=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (WEB_INSTALLATION_ID, media_type, douban_id.strip())).fetchone()
        else:
            row = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND title=? AND COALESCE(year,0)=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (WEB_INSTALLATION_ID, media_type, title.strip(), year or 0)).fetchone()
    return {"subscribed": bool(row), "id": row["id"] if row else None, "status": row["status"] if row else None}


@app.get("/api/web/subscription-statuses")
def web_subscription_statuses(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_ids: str = Query("", max_length=4000)) -> dict[str, Any]:
    ids = sorted({int(x) for x in tmdb_ids.split(",") if x.isdigit() and int(x) > 0})[:100]
    if not ids:
        return {"items": {}}
    marks = ",".join("?" for _ in ids)
    with database() as conn:
        rows = conn.execute(f"SELECT id,tmdb_id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND tmdb_id IN ({marks}) AND status NOT IN ('cancelled','expired') ORDER BY id DESC", [WEB_INSTALLATION_ID, media_type, *ids]).fetchall()
    items: dict[str, Any] = {}
    for row in rows:
        items.setdefault(str(row["tmdb_id"]), {"id": row["id"], "status": row["status"]})
    return {"items": items}


@app.post("/v1/subscriptions")
def create_subscription(
    request: SubscriptionRequest,
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    now = int(time.time())
    with database() as conn:
        if request.tmdb_id:
            existing = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND tmdb_id=? AND COALESCE(season,'')=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (installation_id, request.media_type, request.tmdb_id, request.season.strip())).fetchone()
        elif request.douban_id.strip():
            existing = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND douban_id=? AND COALESCE(season,'')=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (installation_id, request.media_type, request.douban_id.strip(), request.season.strip())).fetchone()
        else:
            existing = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND title=? AND COALESCE(year,0)=? AND COALESCE(season,'')=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (installation_id, request.media_type, request.title.strip(), request.year or 0, request.season.strip())).fetchone()
        if existing:
            return {"id": existing["id"], "status": existing["status"], "duplicate": True, **request.model_dump()}
        cur = conn.execute(
            """INSERT INTO web_subscriptions
               (installation_id,title,media_type,tmdb_id,year,poster,season,subscription_scope,category,save_path,douban_id,moviepilot_id,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (installation_id, request.title.strip(), request.media_type, request.tmdb_id, request.year,
             request.poster.strip(), request.season.strip(), request.subscription_scope.strip() or "普通订阅",
             request.category.strip(), request.save_path.strip(), request.douban_id.strip(),
             request.moviepilot_id.strip(), "active", now, now),
        )
        return {"id": cur.lastrowid, "status": "active", **request.model_dump()}


@app.post("/api/web/subscriptions")
async def web_create_subscription(request: SubscriptionRequest) -> dict[str, Any]:
    result = create_subscription(request, WEB_INSTALLATION_ID)
    try:
        if _telegram_settings(False)["enabled"]:
            await notification_service().notify("subscription", {"title": request.title, "status": "订阅成功" if not result.get("duplicate") else "订阅已存在", "resource": f"TMDB {request.tmdb_id}", "resolution": "", "size": "", "save_path": request.save_path, "error": ""})
    except Exception:
        logger.exception("module=notifications provider=telegram operation=subscription")
    return result


@app.get("/v1/subscriptions")
def list_subscriptions(
    tab: str = Query("current", pattern="^(current|history|all)$"),
    search: str = Query("", max_length=100), media_type: str = Query("", pattern="^(|movie|tv)$"),
    year: int | None = Query(None, ge=1880, le=2200), status: str = Query("", max_length=40),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|updated_desc|title_asc)$"),
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    return subscription_page(installation_id, tab, search, media_type, year, status, page, page_size, sort)


TERMINAL_SUBSCRIPTION_STATUSES = ("completed", "cancelled", "failed", "expired")


def subscription_page(installation_id: str, tab: str = "current", search: str = "", media_type: str = "", year: int | None = None, status: str = "", page: int = 1, page_size: int = 20, sort: str = "created_desc") -> dict[str, Any]:
    conditions = ["s.installation_id = ?"]
    params: list[Any] = [installation_id]
    marks = ",".join("?" for _ in TERMINAL_SUBSCRIPTION_STATUSES)
    if tab == "current":
        conditions.append(f"s.status NOT IN ({marks})")
        params.extend(TERMINAL_SUBSCRIPTION_STATUSES)
    elif tab == "history":
        conditions.append(f"s.status IN ({marks})")
        params.extend(TERMINAL_SUBSCRIPTION_STATUSES)
    if search.strip():
        conditions.append("(s.title LIKE ? OR s.original_title LIKE ?)")
        needle = f"%{search.strip()}%"; params.extend((needle, needle))
    if media_type:
        conditions.append("s.media_type = ?"); params.append(media_type)
    if year:
        conditions.append("s.year = ?"); params.append(year)
    if status:
        conditions.append("s.status = ?"); params.append(status)
    order = {"created_desc": "s.created_at DESC", "created_asc": "s.created_at ASC", "updated_desc": "s.updated_at DESC", "title_asc": "s.title COLLATE NOCASE ASC"}.get(sort, "s.created_at DESC")
    where = " AND ".join(conditions)
    with database() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM web_subscriptions s WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""SELECT s.*,
            (SELECT COUNT(*) FROM subscription_runs r WHERE r.subscription_id=s.id) AS unlock_count,
            (SELECT COUNT(*) FROM transfer_records t WHERE t.installation_id=s.installation_id AND (t.subscription_id=s.id OR (t.subscription_id IS NULL AND t.name=s.title))) AS transfer_count,
            (SELECT COUNT(*) FROM transfer_records t WHERE t.installation_id=s.installation_id AND (t.subscription_id=s.id OR (t.subscription_id IS NULL AND t.name=s.title)) AND t.status='resolved') AS saved_count
            FROM web_subscriptions s WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
        counts = conn.execute("SELECT SUM(status NOT IN ('completed','cancelled','failed','expired')), SUM(status IN ('completed','cancelled','failed','expired')) FROM web_subscriptions WHERE installation_id=?", (installation_id,)).fetchone()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "has_more": page < total_pages, "current_count": counts[0] or 0, "history_count": counts[1] or 0}


def subscription_detail(installation_id: str, subscription_id: int) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM web_subscriptions WHERE id=? AND installation_id=?", (subscription_id, installation_id)).fetchone()
        if not row:
            raise HTTPException(404, "订阅不存在")
        runs = conn.execute("SELECT id,status,resource_count,transfer_count,error,created_at FROM subscription_runs WHERE subscription_id=? ORDER BY id DESC LIMIT 20", (subscription_id,)).fetchall()
        transfers = conn.execute("SELECT id,slug,name,share_url,status,error,created_at FROM transfer_records WHERE installation_id=? AND (subscription_id=? OR (subscription_id IS NULL AND name=?)) ORDER BY id DESC LIMIT 50", (installation_id, subscription_id, row["title"])).fetchall()
    return {"subscription": dict(row), "runs": [dict(x) for x in runs], "transfers": [dict(x) for x in transfers]}


@app.get("/v1/subscriptions/{subscription_id}")
def get_subscription(subscription_id: int, installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    return subscription_detail(installation_id, subscription_id)


@app.delete("/v1/subscriptions/{subscription_id}")
def delete_subscription(subscription_id: int, installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        cur = conn.execute("UPDATE web_subscriptions SET status='cancelled', updated_at=? WHERE id=? AND installation_id=?", (int(time.time()), subscription_id, installation_id))
        if not cur.rowcount:
            raise HTTPException(404, "订阅不存在")
    return {"ok": True, "deleted_files": False, "status": "cancelled"}


async def execute_subscription(installation_id: str, subscription_id: int) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM web_subscriptions WHERE id=? AND installation_id=?", (subscription_id, installation_id)).fetchone()
        if not row:
            raise HTTPException(404, "订阅不存在")
        conn.execute("UPDATE web_subscriptions SET status='running', error='', updated_at=?, last_run_at=? WHERE id=?", (int(time.time()), int(time.time()), subscription_id))
    try:
        payload = await query_resources(ResourceQuery(media_type=row["media_type"], tmdb_id=row["tmdb_id"]), installation_id)
        resources = _resource_items(payload)
        count = len(resources)
        transfer_count = 0
        settings = get_business_settings()
        with database() as conn:
            transfer_ready = installation_id == WEB_INSTALLATION_ID and bool(conn.execute("SELECT 1 FROM installations WHERE installation_id=?", (installation_id,)).fetchone()) and bool(conn.execute("SELECT p115_cookie FROM web_settings WHERE installation_id=? AND COALESCE(p115_cookie,'')<>''", (installation_id,)).fetchone())
        if resources and settings["subscription_auto_transfer"] and transfer_ready:
            first = resources[0]
            slug = str(first.get("slug") or first.get("resource_slug") or first.get("id") or "")
            if slug:
                await web_resource_transfer(WebResourceTransfer(provider="hdhive", resource_id=slug))
                transfer_count = 1
        new_status = "completed" if transfer_count else ("resource_found" if count else "waiting_output")
        error = ""
    except HTTPException as exc:
        count = locals().get("count", 0); transfer_count = 0; new_status = "failed"; error = str(exc.detail)
    except Exception as exc:
        logger.exception("module=subscriptions operation=execute subscription=%s", subscription_id)
        count = locals().get("count", 0); transfer_count = 0; new_status = "failed"; error = str(exc)
    now = int(time.time())
    with database() as conn:
        cur = conn.execute("INSERT INTO subscription_runs (installation_id,subscription_id,status,resource_count,transfer_count,error,created_at) VALUES (?,?,?,?,?,?,?)", (installation_id, subscription_id, new_status, count, transfer_count, error, now))
        conn.execute("UPDATE web_subscriptions SET status=?, error=?, updated_at=? WHERE id=?", (new_status, error, now, subscription_id))
    return {"ok": new_status != "failed", "run_id": cur.lastrowid, "status": new_status, "resource_count": count, "transfer_count": transfer_count, "error": error}


@app.post("/v1/subscriptions/{subscription_id}/run")
async def run_subscription(subscription_id: int, installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    return await execute_subscription(installation_id, subscription_id)


@app.get("/api/web/subscriptions")
def web_subscription_list(
    tab: str = Query("current", pattern="^(current|history|all)$"), search: str = Query("", max_length=100),
    media_type: str = Query("", pattern="^(|movie|tv)$"), year: int | None = Query(None, ge=1880, le=2200),
    status: str = Query("", max_length=40), page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100),
    sort: str = Query("created_desc", pattern="^(created_desc|created_asc|updated_desc|title_asc)$"),
) -> dict[str, Any]:
    return subscription_page(WEB_INSTALLATION_ID, tab, search, media_type, year, status, page, page_size, sort)


@app.get("/api/web/subscriptions/{subscription_id}")
def web_subscription_detail(subscription_id: int) -> dict[str, Any]:
    return subscription_detail(WEB_INSTALLATION_ID, subscription_id)


@app.post("/api/web/subscriptions/{subscription_id}/run")
async def web_subscription_run(subscription_id: int) -> dict[str, Any]:
    return await execute_subscription(WEB_INSTALLATION_ID, subscription_id)


@app.delete("/api/web/subscriptions/{subscription_id}")
def web_subscription_delete(subscription_id: int) -> dict[str, Any]:
    return delete_subscription(subscription_id, WEB_INSTALLATION_ID)


def task_page(installation_id: str, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    with database() as conn:
        total = conn.execute("SELECT COUNT(*) FROM subscription_runs WHERE installation_id=?", (installation_id,)).fetchone()[0]
        rows = conn.execute("""SELECT r.id,r.subscription_id,r.status,r.resource_count,r.transfer_count,r.error,r.created_at,
            s.title,s.media_type,s.tmdb_id FROM subscription_runs r
            LEFT JOIN web_subscriptions s ON s.id=r.subscription_id
            WHERE r.installation_id=? ORDER BY r.id DESC LIMIT ? OFFSET ?""",
            (installation_id, page_size, (page - 1) * page_size),
        ).fetchall()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "has_more": page < total_pages}


@app.get("/api/web/tasks")
def web_task_list(page: int = Query(1, ge=1), page_size: int = Query(10, ge=1, le=100)) -> dict[str, Any]:
    return task_page(WEB_INSTALLATION_ID, page, page_size)


@app.get("/api/web/tasks/{task_id}")
def web_task_detail(task_id: int) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("""SELECT r.id,r.subscription_id,r.status,r.resource_count,r.transfer_count,r.error,r.created_at,
            s.title,s.media_type,s.tmdb_id,s.save_path FROM subscription_runs r
            LEFT JOIN web_subscriptions s ON s.id=r.subscription_id
            WHERE r.id=? AND r.installation_id=?""", (task_id, WEB_INSTALLATION_ID)).fetchone()
    if not row:
        raise HTTPException(404, "任务记录不存在")
    return {"task": dict(row)}


def unlock_page(installation_id: str, page: int = 1, page_size: int = 15, search: str = "", action: str = "", status: str = "", processing_status: str = "", unlock_status: str = "", transfer_status: str = "") -> dict[str, Any]:
    conditions = ["installation_id=?"]
    params: list[Any] = [installation_id]
    if search.strip():
        conditions.append("name LIKE ?"); params.append(f"%{search.strip()}%")
    if action:
        conditions.append("action=?"); params.append(action)
    if status:
        conditions.append("status=?"); params.append(status)
    if processing_status:
        conditions.append("processing_status=?"); params.append(processing_status)
    if unlock_status:
        conditions.append("unlock_status=?"); params.append(unlock_status)
    if transfer_status:
        conditions.append("transfer_status=?"); params.append(transfer_status)
    where = " AND ".join(conditions)
    with database() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM transfer_records WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""SELECT id,subscription_id,slug,name,share_url,status,error,media_type,tmdb_id,resolution,quality,size,uploader,points,action,save_path,processing_status,source_type,unlock_status,transfer_status,created_at,updated_at
            FROM transfer_records WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?""", [*params, page_size, (page - 1) * page_size]).fetchall()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(x) for x in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "has_more": page < total_pages}


@app.get("/api/web/unlocks")
def web_unlock_list(page: int = Query(1, ge=1), page_size: int = Query(15, ge=1, le=100), search: str = Query("", max_length=100), action: str = Query("", max_length=50), status: str = Query("", max_length=40), processing_status: str = Query("", max_length=40), unlock_status: str = Query("", max_length=40), transfer_status: str = Query("", max_length=40)) -> dict[str, Any]:
    return unlock_page(WEB_INSTALLATION_ID, page, page_size, search, action, status, processing_status, unlock_status, transfer_status)


@app.get("/api/web/unlocks/{record_id}")
def web_unlock_detail(record_id: int) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM transfer_records WHERE id=? AND installation_id=?", (record_id, WEB_INSTALLATION_ID)).fetchone()
    if not row:
        raise HTTPException(404, "解锁记录不存在")
    return {"item": dict(row)}


@app.post("/api/web/unlocks/{record_id}/retry")
async def web_unlock_retry(record_id: int) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT slug,unlock_status FROM transfer_records WHERE id=? AND installation_id=?", (record_id, WEB_INSTALLATION_ID)).fetchone()
    if not row:
        raise HTTPException(404, "解锁记录不存在")
    try:
        result = await resolve_resource(ResolveRequest(slug=row["slug"], max_unlock_points=None), WEB_INSTALLATION_ID)
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='resolved',processing_status='resolved',unlock_status='unlocked',share_url=?,error='',updated_at=? WHERE id=?", (result.get("share_url", ""), int(time.time()), record_id))
        transferred = await _execute_transfer(result, row["slug"])
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='completed',processing_status='completed',unlock_status='unlocked',transfer_status='success',share_url=?,save_path=?,error='',updated_at=? WHERE id=?",
                         (result.get("share_url", ""), transferred.get("save_path", ""), int(time.time()), record_id))
        return {"ok": True, **transferred}
    except P115Error as exc:
        with database() as conn:
            conn.execute("UPDATE transfer_records SET unlock_status='unlocked',transfer_status='failed',error=?,updated_at=? WHERE id=?", (str(exc), int(time.time()), record_id))
        raise HTTPException(502, str(exc)) from exc
    except HTTPException as exc:
        with database() as conn:
            conn.execute("UPDATE transfer_records SET error=?,updated_at=? WHERE id=?", (str(exc.detail), int(time.time()), record_id))
        raise


class UnlockBatchDelete(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)


@app.delete("/api/web/unlocks")
def web_unlock_delete(request: UnlockBatchDelete) -> dict[str, Any]:
    ids = sorted({value for value in request.ids if value > 0})
    if not ids:
        raise HTTPException(400, "未选择有效记录")
    marks = ",".join("?" for _ in ids)
    with database() as conn:
        cur = conn.execute(f"DELETE FROM transfer_records WHERE installation_id=? AND id IN ({marks})", [WEB_INSTALLATION_ID, *ids])
    return {"ok": True, "deleted": cur.rowcount, "deleted_files": False}


class WebSettings(BaseModel):
    moviepilot_url: str = Field(default="", max_length=500)
    save_directory: str = Field(default="", max_length=500)
    offline_enabled: bool = True
    tmdb_api_key: str = Field(default="", max_length=1000)
    tmdb_language: str = Field(default="zh-CN", max_length=16)
    tmdb_region: str = Field(default="CN", max_length=8)


class BusinessSettings(BaseModel):
    root_directory: str = Field(default="", max_length=500)
    save_directory: str = Field(default="", max_length=500)
    save_folder_id: str = Field(default="", max_length=64)
    scrape_directory: str = Field(default="", max_length=500)
    save_wait_seconds: int = Field(default=30, ge=0, le=3600)
    retry_count: int = Field(default=3, ge=0, le=20)
    duplicate_policy: str = Field(default="skip", pattern="^(skip|rename|overwrite)$")
    subscription_auto_transfer: bool = True
    subscription_interval: int = Field(default=1800, ge=300, le=86400)
    offline_enabled: bool = True
    ed2k_directory: str = Field(default="", max_length=500)
    ed2k_poll_interval: int = Field(default=60, ge=10, le=3600)
    ed2k_retry_count: int = Field(default=3, ge=0, le=20)
    ed2k_auto_archive: bool = True
    p115_security_key: str = Field(default="", max_length=64)


BUSINESS_SETTING_COLUMNS = (
    "root_directory,save_directory,save_folder_id,scrape_directory,save_wait_seconds,retry_count,"
    "duplicate_policy,subscription_auto_transfer,subscription_interval,offline_enabled,ed2k_directory,ed2k_poll_interval,"
    "ed2k_retry_count,ed2k_auto_archive,p115_security_key"
)


@app.get("/api/web/settings/business")
def get_business_settings() -> dict[str, Any]:
    init_database()
    with database() as conn:
        row = conn.execute(
            f"SELECT {BUSINESS_SETTING_COLUMNS} FROM web_settings WHERE installation_id=?",
            (WEB_INSTALLATION_ID,),
        ).fetchone()
    if not row:
        return BusinessSettings().model_dump()
    result = dict(row)
    result["offline_enabled"] = bool(result["offline_enabled"])
    result["ed2k_auto_archive"] = bool(result["ed2k_auto_archive"])
    result["subscription_auto_transfer"] = bool(result["subscription_auto_transfer"])
    result["p115_security_key"] = _decrypt_optional(result["p115_security_key"])
    return result


@app.put("/api/web/settings/business")
def put_business_settings(request: BusinessSettings) -> dict[str, Any]:
    values = request.model_dump()
    with database() as conn:
        current = conn.execute(
            "SELECT p115_security_key FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)
        ).fetchone()
        security_key = values["p115_security_key"].strip()
        if security_key:
            secret = encrypt(security_key)
        else:
            secret = current["p115_security_key"] if current and current["p115_security_key"] else ""
        conn.execute(
            """INSERT INTO web_settings
            (installation_id,root_directory,save_directory,save_folder_id,scrape_directory,save_wait_seconds,
             retry_count,duplicate_policy,subscription_auto_transfer,subscription_interval,offline_enabled,ed2k_directory,ed2k_poll_interval,
             ed2k_retry_count,ed2k_auto_archive,p115_security_key,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(installation_id) DO UPDATE SET
             root_directory=excluded.root_directory,save_directory=excluded.save_directory,
             save_folder_id=excluded.save_folder_id,
             scrape_directory=excluded.scrape_directory,save_wait_seconds=excluded.save_wait_seconds,
             retry_count=excluded.retry_count,duplicate_policy=excluded.duplicate_policy,
             subscription_auto_transfer=excluded.subscription_auto_transfer,subscription_interval=excluded.subscription_interval,
             offline_enabled=excluded.offline_enabled,ed2k_directory=excluded.ed2k_directory,
             ed2k_poll_interval=excluded.ed2k_poll_interval,
             ed2k_retry_count=excluded.ed2k_retry_count,
             ed2k_auto_archive=excluded.ed2k_auto_archive,p115_security_key=excluded.p115_security_key,updated_at=excluded.updated_at""",
            (WEB_INSTALLATION_ID, values["root_directory"].strip(), values["save_directory"].strip(),
             values["save_folder_id"].strip(), values["scrape_directory"].strip(), values["save_wait_seconds"], values["retry_count"],
             values["duplicate_policy"], int(values["subscription_auto_transfer"]), values["subscription_interval"], int(values["offline_enabled"]), values["ed2k_directory"].strip(),
             values["ed2k_poll_interval"], values["ed2k_retry_count"],
             int(values["ed2k_auto_archive"]), secret, int(time.time())),
        )
    return {"ok": True, **request.model_dump()}


@app.post("/api/web/settings/ed2k-test")
def test_ed2k_settings() -> dict[str, Any]:
    settings = get_business_settings()
    if not settings["offline_enabled"]:
        raise HTTPException(409, "请先启用 ED2K / 磁力云下载")
    if not settings["ed2k_directory"]:
        raise HTTPException(409, "请先填写 115 云下载保存目录")
    return {"ok": True, "message": "ED2K 参数有效；实际下载将使用授权中心的 115 凭据"}


class OfflineTaskRequest(BaseModel):
    url: str = Field(min_length=10, max_length=10000)


@app.post("/api/web/offline")
async def add_offline_task(request: OfflineTaskRequest) -> dict[str, Any]:
    settings = get_business_settings()
    if not settings["offline_enabled"]:
        raise HTTPException(409, "ED2K / 磁力云下载未启用")
    with database() as conn:
        row = conn.execute("SELECT p115_cookie FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not row or not row["p115_cookie"]:
        raise HTTPException(409, "请先在授权中心配置115 Cookie")
    try:
        result = await P115Client(decrypt(row["p115_cookie"]), REQUEST_TIMEOUT).offline(request.url, settings["ed2k_directory"])
        now = int(time.time())
        with database() as conn:
            cur = conn.execute("INSERT INTO offline_tasks (installation_id,task_url,provider_task_id,status,save_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (WEB_INSTALLATION_ID, request.url, result.get("task_id", ""), result.get("status", "submitted"), settings["ed2k_directory"], now, now))
        return {**result, "id": cur.lastrowid, "save_path": settings["ed2k_directory"]}
    except P115Error as exc:
        logger.error("module=offline provider=115 operation=add_task error_type=P115Error reason=%s", exc)
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/web/offline")
def list_offline_tasks(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    offset = (page - 1) * page_size
    with database() as conn:
        total = conn.execute("SELECT COUNT(*) FROM offline_tasks WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()[0]
        rows = conn.execute("SELECT id,task_url,provider_task_id,status,save_path,error,retry_count,created_at,updated_at FROM offline_tasks WHERE installation_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (WEB_INSTALLATION_ID, page_size, offset)).fetchall()
    pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total, "total_pages": pages, "has_more": page < pages}


@app.get("/api/web/115/folders")
async def web_115_folders(cid: str = Query("", max_length=32)) -> dict[str, Any]:
    """读取当前115账号的网盘目录，cid 为空表示根目录。"""
    with database() as conn:
        row = conn.execute("SELECT p115_cookie,p115_security_key FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not row or not row["p115_cookie"]:
        raise HTTPException(409, "115 尚未授权，请先到授权中心扫码或配置 Cookie")
    try:
        result = await P115Client(decrypt(row["p115_cookie"]), REQUEST_TIMEOUT).folders(cid.strip(), _decrypt_optional(row["p115_security_key"]))
        return {**result, "cookie_ok": True}
    except P115Error as exc:
        if "Cookie" in str(exc) or "失效" in str(exc) or "UID" in str(exc):
            raise HTTPException(401, "115 授权已失效，请重新扫码授权") from exc
        raise HTTPException(502, str(exc)) from exc


class AuthorizationUpdate(BaseModel):
    api_key: str = Field(default="", max_length=4000)
    cookie: str = Field(default="", max_length=16000)
    url: str = Field(default="", max_length=500)
    user_id: str = Field(default="", max_length=200)
    language: str = Field(default="zh-CN", max_length=16)


class ProviderTestRequest(BaseModel):
    """Current form values used by 测试连接; empty fields fall back to saved config."""
    url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=4000)
    user_id: str = Field(default="", max_length=200)
    cookie: str = Field(default="", max_length=16000)
    language: str = Field(default="", max_length=16)


def _decrypt_optional(value: str | None) -> str:
    return decrypt(value) if value else ""


def _authorization_rows() -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    init_database()
    with database() as conn:
        settings = conn.execute("SELECT * FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        installation = conn.execute("SELECT * FROM installations WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    return settings, installation


def _provider_states() -> dict[str, dict[str, Any]]:
    with database() as conn:
        rows = conn.execute("SELECT provider,state_json FROM provider_state WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            result[row["provider"]] = json.loads(row["state_json"] or "{}")
        except ValueError:
            result[row["provider"]] = {}
    return result


def _save_provider_state(provider: str, state: dict[str, Any]) -> None:
    # Store only public account/server metadata, never credentials.
    with database() as conn:
        conn.execute("INSERT INTO provider_state (installation_id,provider,state_json,updated_at) VALUES (?,?,?,?) ON CONFLICT(installation_id,provider) DO UPDATE SET state_json=excluded.state_json,updated_at=excluded.updated_at",
                     (WEB_INSTALLATION_ID, provider, json.dumps(state, ensure_ascii=False), int(time.time())))


@app.get("/api/web/authorizations")
def get_authorizations() -> dict[str, Any]:
    settings, installation = _authorization_rows()
    states = _provider_states()
    user: dict[str, Any] = {}
    if installation:
        try:
            user = json.loads(installation["user_json"] or "{}")
        except ValueError:
            user = {}
    return {"definitions": AUTHORIZATION_PROVIDERS.infos(), "providers": {
        "hdhive": {"configured": True, "authorized": bool(installation),
                    "summary": str(user.get("nickname") or user.get("name") or user.get("username") or "等待授权")},
        "p115": {"configured": bool(settings and settings["p115_cookie"]), "authorized": bool(settings and settings["p115_cookie"]),
                 "summary": states.get("p115", {}).get("summary") or ("Cookie 已安全保存" if settings and settings["p115_cookie"] else "未配置 Cookie")},
        "emby": {"configured": bool(settings and settings["emby_url"] and settings["emby_api_key"]),
                 "authorized": False, "url": settings["emby_url"] if settings else "", "user_id": settings["emby_user_id"] if settings else "",
                 "available": bool(states.get("emby", {}).get("available")),
                 "summary": states.get("emby", {}).get("summary") or (settings["emby_url"] if settings and settings["emby_url"] else "未配置服务器")},
        "tmdb": {"configured": bool(settings and settings["tmdb_api_key"]), "authorized": False,
                 "language": settings["tmdb_language"] if settings else "zh-CN", "summary": states.get("tmdb", {}).get("summary") or ("密钥已安全保存" if settings and settings["tmdb_api_key"] else "未配置 API Key")},
    }}


@app.put("/api/web/authorizations/{provider}")
def put_authorization(provider: str, request: AuthorizationUpdate) -> dict[str, Any]:
    if not AUTHORIZATION_PROVIDERS.get(provider):
        raise HTTPException(404, "Provider 不存在")
    if provider not in {"p115", "emby", "tmdb"}:
        raise HTTPException(400, "该 Provider 使用独立 OAuth 授权")
    init_database()
    with database() as conn:
        current = conn.execute("SELECT * FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        if not current:
            conn.execute("INSERT INTO web_settings (installation_id,updated_at) VALUES (?,?)", (WEB_INSTALLATION_ID, int(time.time())))
            current = conn.execute("SELECT * FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        if provider == "p115":
            secret = encrypt(request.cookie.strip()) if request.cookie.strip() else current["p115_cookie"]
            conn.execute("UPDATE web_settings SET p115_cookie=?,updated_at=? WHERE installation_id=?", (secret, int(time.time()), WEB_INSTALLATION_ID))
        elif provider == "emby":
            secret = encrypt(request.api_key.strip()) if request.api_key.strip() else current["emby_api_key"]
            conn.execute("UPDATE web_settings SET emby_url=?,emby_api_key=?,emby_user_id=?,updated_at=? WHERE installation_id=?",
                         (request.url.strip().rstrip("/"), secret, request.user_id.strip(), int(time.time()), WEB_INSTALLATION_ID))
        else:
            secret = encrypt(request.api_key.strip()) if request.api_key.strip() else current["tmdb_api_key"]
            conn.execute("UPDATE web_settings SET tmdb_api_key=?,tmdb_language=?,updated_at=? WHERE installation_id=?",
                         (secret, request.language.strip() or "zh-CN", int(time.time()), WEB_INSTALLATION_ID))
    # 保存新 Emby 配置后立即作废旧连接状态，详情页下一次读取就是最新配置（无缓存可残留）。
    if provider == "emby":
        _save_provider_state("emby", {"configured": True, "available": False, "summary": request.url.strip().rstrip("/"), "server_name": "", "version": "", "user_name": ""})
    return {"ok": True, "configured": bool(secret)}


@app.post("/api/web/authorizations/{provider}/test")
async def test_authorization(provider: str, request: ProviderTestRequest | None = None) -> dict[str, Any]:
    settings, installation = _authorization_rows()
    if provider == "hdhive":
        if not installation:
            raise HTTPException(409, "影巢尚未授权，请先打开授权页面")
        token = await access_for(WEB_INSTALLATION_ID)
        user = await hdhive_request("GET", "/api/open/me", access_token=token)
        return {"ok": True, "message": f"影巢连接正常：{user.get('nickname') or user.get('name') or '已授权'}"}
    if not settings:
        raise HTTPException(409, "Provider 尚未配置")
    if provider == "p115":
        cookie = request.cookie.strip() if request and request.cookie.strip() else _decrypt_optional(settings["p115_cookie"])
        if not cookie:
            raise HTTPException(409, "请先配置 115 Cookie")
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.get("https://my.115.com/?ct=ajax&ac=get_user_aq", headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(502, "115 返回了无法识别的数据") from exc
        if response.status_code >= 400 or payload.get("state") is False:
            raise HTTPException(401, "115 Cookie 已失效")
        account = payload.get("data") or {}
        uid = str(account.get("uid") or account.get("user_id") or "")
        name = str(account.get("user_name") or account.get("nickname") or "")
        summary = " · ".join(value for value in (name, f"UID {uid}" if uid else "") if value) or "115 Cookie 有效"
        _save_provider_state("p115", {"summary": summary, "uid": uid, "name": name})
        return {"ok": True, "message": f"115 连接正常：{summary}"}
    if provider == "emby":
        # 测试连接使用表单里当前填写的内容；留空的字段回退到已保存配置。
        url = (request.url.strip().rstrip("/") if request and request.url.strip() else (settings["emby_url"] or "").strip().rstrip("/"))
        key = (request.api_key.strip() if request and request.api_key.strip() else _decrypt_optional(settings["emby_api_key"]))
        user_id = (request.user_id.strip() if request and request.user_id.strip() else (settings["emby_user_id"] or "").strip())
        if not url:
            raise HTTPException(409, "请先填写 Emby 地址")
        if not key:
            raise HTTPException(409, "请先填写 Emby API Key")
        if not user_id:
            raise HTTPException(409, "请先填写 Emby User ID")
        headers = {"X-Emby-Token": key}
        try:
            async with httpx.AsyncClient(timeout=EMBY_TIMEOUT) as client:
                try:
                    info_response = await client.get(f"{url}/System/Info", headers=headers)
                except httpx.TimeoutException as exc:
                    raise HTTPException(502, "无法访问 Emby 服务器：连接超时，请检查地址与网络") from exc
                except httpx.HTTPError as exc:
                    raise HTTPException(502, f"无法访问 Emby 服务器：{type(exc).__name__}") from exc
                if info_response.status_code in (401, 403):
                    raise HTTPException(401, "Emby API Key 无效（HTTP 401/403）")
                if info_response.status_code >= 400:
                    raise HTTPException(502, f"Emby 连接失败（HTTP {info_response.status_code}）")
                info = info_response.json()
                try:
                    user_response = await client.get(f"{url}/Users/{user_id}", headers=headers)
                except httpx.TimeoutException as exc:
                    raise HTTPException(502, "无法访问 Emby 服务器：连接超时") from exc
                except httpx.HTTPError as exc:
                    raise HTTPException(502, f"无法访问 Emby 服务器：{type(exc).__name__}") from exc
                if user_response.status_code in (401, 403):
                    raise HTTPException(401, "Emby API Key 无效（HTTP 401/403）")
                if user_response.status_code == 404:
                    raise HTTPException(400, "Emby User ID 无效：未找到对应用户")
                if user_response.status_code >= 400:
                    raise HTTPException(502, f"Emby 用户信息读取失败（HTTP {user_response.status_code}）")
                user_info = user_response.json()
        except HTTPException:
            raise
        server_name = str(info.get("ServerName") or "未知服务器")
        version = str(info.get("Version") or "")
        user_name = str(user_info.get("Name") or user_id)
        summary = " · ".join(x for x in (server_name, f"v{version}" if version else "", f"用户 {user_name}" if user_name else "") if x)
        _save_provider_state("emby", {"configured": True, "available": True, "summary": summary, "server_name": server_name, "version": version, "user_name": user_name})
        return {"ok": True, "message": f"连接成功\nServer: {server_name}\nVersion: {version}\nUser: {user_name}"}
    if provider == "tmdb":
        token = _decrypt_optional(settings["tmdb_api_key"])
        if not token:
            raise HTTPException(409, "请先配置 TMDB API Key / Token")
        headers = {"Authorization": f"Bearer {token}"} if len(token) > 40 else {}
        params = {} if headers else {"api_key": token}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get("https://api.themoviedb.org/3/configuration", headers=headers, params=params)
        if response.status_code >= 400:
            raise HTTPException(401, "TMDB 凭据无效")
        _save_provider_state("tmdb", {"summary": "TMDB 连接正常"})
        return {"ok": True, "message": "TMDB 连接正常"}
    raise HTTPException(404, "Provider 不存在")


DEFAULT_TELEGRAM_EVENTS = {"transfer_success": True, "transfer_failed": True, "subscription": True, "manual_review": True}
CHANNEL_MONITOR = ChannelMonitor(REQUEST_TIMEOUT)
_CHANNEL_WORKER_STARTED = False
_TELEGRAM_BOT_WORKER_STARTED = False
_NEXT_SUBSCRIPTION_CHECK = 0
_NEXT_OFFLINE_CHECK = 0
P115_QR_SESSIONS: dict[str, dict[str, Any]] = {}
P115_QR_APPS = {
    "web": "115生活_网页端",
    "ios": "115生活_苹果端",
    "115ios": "115管理_苹果端",
    "android": "115生活_安卓端",
    "115android": "115管理_安卓端",
    "ipad": "115生活_苹果平板",
    "115ipad": "115管理_苹果平板",
    "qandroid": "115管理_安卓端(Q)",
    "qios": "115管理_苹果端(Q)",
    "qipad": "115管理_苹果平板(Q)",
    "os_windows": "115生活_Windows端",
    "os_mac": "115生活_macOS端",
    "os_linux": "115生活_Linux端",
    "wechatmini": "115生活_微信小程序",
    "alipaymini": "115生活_支付宝小程序",
    "harmony": "115鸿蒙端",
}


class P115QRStartRequest(BaseModel):
    app: str = Field(default="web", max_length=30)


class TelegramSettings(BaseModel):
    bot_token: str = Field(default="", max_length=4000)
    chat_id: str = Field(default="", max_length=200)
    api_id: str = Field(default="", max_length=50)
    api_hash: str = Field(default="", max_length=500)
    authorized_user_id: str = Field(default="", max_length=100)
    enabled: bool = False
    events: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_TELEGRAM_EVENTS))
    template: str = Field(default="", max_length=5000)
    channel_enabled: bool = False
    channel_name: str = Field(default="oneonefivewpfx", max_length=200)
    channel_interval: int = Field(default=600, ge=60, le=86400)


class NotificationEventRequest(BaseModel):
    event: str = Field(pattern="^(transfer_success|transfer_failed|subscription|manual_review)$")
    title: str = Field(default="", max_length=500)
    status: str = Field(default="", max_length=200)
    resource: str = Field(default="", max_length=1000)
    resolution: str = Field(default="", max_length=100)
    size: str = Field(default="", max_length=100)
    save_path: str = Field(default="", max_length=1000)
    error: str = Field(default="", max_length=2000)


def _telegram_settings(include_secret: bool = False) -> dict[str, Any]:
    init_database()
    with database() as conn:
        row = conn.execute("SELECT telegram_bot_token,telegram_chat_id,telegram_api_id,telegram_api_hash,telegram_user_id,telegram_enabled,telegram_events,telegram_template,channel_enabled,channel_name,channel_interval FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not row:
        return {"bot_token": "", "bot_token_configured": False, "chat_id": "", "api_id": "", "api_hash_configured": False, "authorized_user_id": "", "enabled": False, "events": dict(DEFAULT_TELEGRAM_EVENTS), "template": "", "channel_enabled": False, "channel_name": "oneonefivewpfx", "channel_interval": 600}
    try:
        events = {**DEFAULT_TELEGRAM_EVENTS, **json.loads(row["telegram_events"] or "{}")}
    except ValueError:
        events = dict(DEFAULT_TELEGRAM_EVENTS)
    token = _decrypt_optional(row["telegram_bot_token"])
    api_hash = _decrypt_optional(row["telegram_api_hash"])
    return {"bot_token": token if include_secret else "", "bot_token_configured": bool(token), "chat_id": row["telegram_chat_id"], "api_id": row["telegram_api_id"], "api_hash": api_hash if include_secret else "", "api_hash_configured": bool(api_hash), "authorized_user_id": row["telegram_user_id"], "enabled": bool(row["telegram_enabled"]), "events": events, "template": row["telegram_template"], "channel_enabled": bool(row["channel_enabled"]), "channel_name": row["channel_name"], "channel_interval": row["channel_interval"] or 600}


@app.get("/api/web/telegram/settings")
def get_telegram_settings() -> dict[str, Any]:
    return _telegram_settings(False)


@app.put("/api/web/telegram/settings")
def put_telegram_settings(request: TelegramSettings) -> dict[str, Any]:
    init_database()
    with database() as conn:
        row = conn.execute("SELECT telegram_bot_token,telegram_api_hash FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        token = encrypt(request.bot_token.strip()) if request.bot_token.strip() else (row["telegram_bot_token"] if row else "")
        api_hash = encrypt(request.api_hash.strip()) if request.api_hash.strip() else (row["telegram_api_hash"] if row else "")
        conn.execute("""INSERT INTO web_settings (installation_id,telegram_bot_token,telegram_chat_id,telegram_api_id,telegram_api_hash,telegram_user_id,telegram_enabled,telegram_events,telegram_template,channel_enabled,channel_name,channel_interval,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET telegram_bot_token=excluded.telegram_bot_token,telegram_chat_id=excluded.telegram_chat_id,telegram_api_id=excluded.telegram_api_id,telegram_api_hash=excluded.telegram_api_hash,telegram_user_id=excluded.telegram_user_id,telegram_enabled=excluded.telegram_enabled,telegram_events=excluded.telegram_events,telegram_template=excluded.telegram_template,channel_enabled=excluded.channel_enabled,channel_name=excluded.channel_name,channel_interval=excluded.channel_interval,updated_at=excluded.updated_at""",
            (WEB_INSTALLATION_ID, token, request.chat_id.strip(), request.api_id.strip(), api_hash, request.authorized_user_id.strip(), int(request.enabled), json.dumps(request.events), request.template, int(request.channel_enabled), request.channel_name.strip(), request.channel_interval, int(time.time())))
    return {"ok": True, "bot_token_configured": bool(token)}


def notification_service() -> NotificationService:
    config = _telegram_settings(True)
    events = config["events"] if config["enabled"] else {name: False for name in DEFAULT_TELEGRAM_EVENTS}
    return NotificationService(TelegramProvider(config["bot_token"], config["chat_id"], REQUEST_TIMEOUT), events, config["template"])


@app.post("/api/web/telegram/test")
async def test_telegram() -> dict[str, Any]:
    config = _telegram_settings(True)
    if not config["bot_token"] or not config["chat_id"]:
        raise HTTPException(409, "请先配置 Bot Token 和 Chat ID")
    try:
        await TelegramProvider(config["bot_token"], config["chat_id"], REQUEST_TIMEOUT).send("影舟 MovieArk Telegram 通知测试成功")
    except NotificationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "message": "测试消息已发送"}


@app.post("/v1/notifications/event")
async def send_business_notification(request: NotificationEventRequest, _: str = Depends(require_installation)) -> dict[str, Any]:
    """Allow trusted MoviePilot installations to emit Telegram business events."""
    try:
        result = await notification_service().notify(request.event, request.model_dump(exclude={"event"}))
    except NotificationError as exc:
        logger.warning("notification provider=telegram operation=%s error=%s", request.event, type(exc).__name__)
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, **result}


def _channel_status() -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM channel_state WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    return dict(row) if row else {"last_message_id": 0, "last_check": 0, "next_check": 0, "last_status": "等待运行", "last_error": "", "processed": 0}


@app.get("/api/web/telegram/channel/status")
def channel_status() -> dict[str, Any]:
    # 兼容旧接口：返回第一个频道（迁移后即原单频道）的状态。
    rows = _channel_rows()
    if rows:
        return {"last_message_id": rows[0]["last_message_id"], "last_check": rows[0]["last_check_at"], "next_check": rows[0]["next_check_at"],
                "last_status": rows[0]["last_status"], "last_error": rows[0]["last_error"], "processed": rows[0]["processed_count"]}
    return _channel_status()


def _normalize_channel_username(value: str) -> str:
    name = str(value or "").strip().rstrip("/").split("/")[-1].lstrip("@").strip()
    return name


def _channel_rows() -> list[sqlite3.Row]:
    with database() as conn:
        return conn.execute("SELECT * FROM telegram_channels WHERE installation_id=? ORDER BY id ASC", (WEB_INSTALLATION_ID,)).fetchall()


def _get_channel_row(channel_id: int) -> sqlite3.Row | None:
    with database() as conn:
        return conn.execute("SELECT * FROM telegram_channels WHERE id=? AND installation_id=?", (channel_id, WEB_INSTALLATION_ID)).fetchone()


def _channel_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["name"] = result.get("name") or result.get("username") or ""
    result["enabled"] = bool(result.get("enabled"))
    try:
        result["rules"] = json.loads(result.get("rules_json") or "{}")
    except ValueError:
        result["rules"] = {}
    for key in ("last_message_id", "last_check_at", "next_check_at", "processed_count", "created_at", "updated_at", "check_interval"):
        result[key] = int(result.get(key) or 0)
    return result


class TelegramChannelUpdate(BaseModel):
    name: str = Field(default="", max_length=200)
    username: str = Field(default="", max_length=300)
    enabled: bool = True
    check_interval: int = Field(default=600, ge=60, le=86400)
    rules: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/web/telegram/channels")
def list_telegram_channels() -> dict[str, Any]:
    return {"items": [_channel_dict(row) for row in _channel_rows()]}


@app.post("/api/web/telegram/channels")
def create_telegram_channel(request: TelegramChannelUpdate) -> dict[str, Any]:
    username = _normalize_channel_username(request.username)
    if not username:
        raise HTTPException(400, "请填写频道用户名或链接")
    if len(username) < 5:
        raise HTTPException(400, "频道用户名过短")
    now = int(time.time())
    with database() as conn:
        cur = conn.execute("INSERT INTO telegram_channels (installation_id,name,username,enabled,check_interval,last_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                           (WEB_INSTALLATION_ID, request.name.strip(), username, int(request.enabled), request.check_interval, "等待运行", now, now))
        channel_id = int(cur.lastrowid)
    logger.info("module=telegram_channel operation=created channel=@%s name=%s", username, request.name.strip())
    return {"ok": True, "channel": _channel_dict(_get_channel_row(channel_id))}


@app.put("/api/web/telegram/channels/{channel_id}")
def update_telegram_channel(channel_id: int, request: TelegramChannelUpdate) -> dict[str, Any]:
    row = _get_channel_row(channel_id)
    if not row:
        raise HTTPException(404, "频道不存在")
    username = _normalize_channel_username(request.username)
    if not username:
        raise HTTPException(400, "请填写频道用户名或链接")
    if len(username) < 5:
        raise HTTPException(400, "频道用户名过短")
    now = int(time.time())
    with database() as conn:
        conn.execute("UPDATE telegram_channels SET name=?,username=?,enabled=?,check_interval=?,rules_json=?,updated_at=? WHERE id=? AND installation_id=?",
                     (request.name.strip(), username, int(request.enabled), request.check_interval, json.dumps(request.rules, ensure_ascii=False), now, channel_id, WEB_INSTALLATION_ID))
    logger.info("module=telegram_channel operation=updated channel=@%s", username)
    return {"ok": True, "channel": _channel_dict(_get_channel_row(channel_id))}


@app.delete("/api/web/telegram/channels/{channel_id}")
def delete_telegram_channel(channel_id: int) -> dict[str, Any]:
    row = _get_channel_row(channel_id)
    if not row:
        raise HTTPException(404, "频道不存在")
    with database() as conn:
        conn.execute("DELETE FROM telegram_channels WHERE id=? AND installation_id=?", (channel_id, WEB_INSTALLATION_ID))
    # 只删除监控配置；历史解锁/转存记录保留，去重记录保留以防重新添加后误处理旧消息。
    logger.info("module=telegram_channel operation=deleted channel=@%s", row["username"])
    return {"ok": True}


async def _process_channel_message(message, channel_id: int, username: str) -> int:
    """处理单条频道消息的链接；返回成功处理的资源数。"""
    processed = 0
    for link in message.links:
        if "hdhive.com/resource/" in link:
            slug = link.split("/resource/", 1)[1].split("?", 1)[0]
            await web_resource_transfer(WebResourceTransfer(provider="hdhive", resource_id=slug))
        else:
            with database() as conn:
                settings = conn.execute("SELECT p115_cookie,save_directory,save_folder_id FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
            if not settings or not settings["p115_cookie"]:
                raise P115Error("115 Cookie 未配置")
            await P115Client(decrypt(settings["p115_cookie"]), REQUEST_TIMEOUT).transfer(link, str(settings["save_folder_id"] or "") or settings["save_directory"] or "")
        processed += 1
    return processed


async def _check_one_channel(row: sqlite3.Row) -> dict[str, Any]:
    channel_id = int(row["id"])
    username = _normalize_channel_username(row["username"])
    interval = int(row["check_interval"] or 600)
    now = int(time.time())
    latest = int(row["last_message_id"] or 0)
    processed = failed = 0
    error = ""
    status = ""
    logger.info("module=telegram_channel channel=@%s check started", username)
    try:
        messages = await CHANNEL_MONITOR.fetch(username, latest)
        new_messages = [message for message in messages if message.message_id > latest]
        if latest == 0 and new_messages:
            latest = max(message.message_id for message in new_messages)
            status = "首次检查已建立频道游标，只处理之后的新消息"
            logger.info("module=telegram_channel channel=@%s first cursor=%s", username, latest)
        else:
            with database() as conn:
                seen_ids = {r["message_id"] for r in conn.execute(
                    "SELECT message_id FROM telegram_channel_messages WHERE installation_id=? AND channel_id=?", (WEB_INSTALLATION_ID, channel_id)).fetchall()}
            pending = [message for message in new_messages if message.message_id not in seen_ids]
            logger.info("module=telegram_channel channel=@%s new messages=%s", username, len(pending))
            for message in pending:
                latest = max(latest, message.message_id)
                try:
                    processed += await _process_channel_message(message, channel_id, username)
                except Exception as exc:  # 单条消息失败只记录，不中断后续消息
                    failed += 1
                    error = str(exc)
                    logger.exception("module=telegram_channel channel=@%s message=%s error_type=%s reason=%s", username, message.message_id, type(exc).__name__, exc)
                finally:
                    with database() as conn:
                        conn.execute("INSERT OR IGNORE INTO telegram_channel_messages (installation_id,channel_id,message_id,processed_at) VALUES (?,?,?,?)",
                                     (WEB_INSTALLATION_ID, channel_id, message.message_id, int(time.time())))
            if pending:
                status = f"发现 {len(pending)} 条新消息，处理 {processed} 个资源，失败 {failed} 条"
            else:
                status = "未发现新资源"
            logger.info("module=telegram_channel channel=@%s processed=%s failed=%s", username, processed, failed)
        with database() as conn:
            conn.execute("UPDATE telegram_channels SET last_message_id=?,last_check_at=?,next_check_at=?,last_status=?,last_error=?,processed_count=processed_count+?,updated_at=? WHERE id=? AND installation_id=?",
                         (latest, now, now + interval, status, error[:1000], processed, now, channel_id, WEB_INSTALLATION_ID))
        return {"ok": not bool(error), "message": status if not error else f"{status}：{error}", "processed": processed, "failed": failed}
    except Exception as exc:
        logger.exception("module=telegram_channel channel=@%s error_type=%s reason=%s", username, type(exc).__name__, exc)
        with database() as conn:
            conn.execute("UPDATE telegram_channels SET last_check_at=?,next_check_at=?,last_status='检查失败',last_error=?,updated_at=? WHERE id=? AND installation_id=?",
                         (now, now + interval, str(exc)[:1000], now, channel_id, WEB_INSTALLATION_ID))
        return {"ok": False, "message": f"检查失败：{exc}", "processed": 0, "failed": 0}


async def run_channel_check(channel_id: int | None = None) -> dict[str, Any]:
    """立即检查单个频道；channel_id 为空时检查全部启用频道（互相隔离）。"""
    if channel_id is not None:
        row = _get_channel_row(channel_id)
        if not row:
            raise HTTPException(404, "频道不存在")
        return await _check_one_channel(row)
    rows = [row for row in _channel_rows() if row["enabled"]]
    if not rows:
        return {"ok": True, "message": "没有启用的频道", "processed": 0, "failed": 0}
    results = []
    for row in rows:
        try:
            results.append(await _check_one_channel(row))
        except Exception as exc:  # 一个频道失败不能影响其他频道
            logger.exception("module=telegram_channel channel_id=%s check_all failed", row["id"])
            results.append({"ok": False, "message": f"检查失败：{exc}", "processed": 0, "failed": 0})
    return {"ok": True, "results": results, "checked": len(rows)}


@app.post("/api/web/telegram/channels/{channel_id}/check")
async def channel_check_one(channel_id: int) -> dict[str, Any]:
    return await run_channel_check(channel_id)


@app.post("/api/web/telegram/channels/check")
async def channel_check_all() -> dict[str, Any]:
    return await run_channel_check(None)


@app.post("/api/web/telegram/channel/check")
async def channel_check() -> dict[str, Any]:
    # 兼容旧接口：等价于全部检查。
    return await run_channel_check(None)


def _channel_worker() -> None:
    global _NEXT_SUBSCRIPTION_CHECK, _NEXT_OFFLINE_CHECK
    while True:
        try:
            now = int(time.time())
            for row in _channel_rows():
                if not row["enabled"]:
                    continue
                if int(row.get("next_check_at") or 0) <= now:
                    try:
                        asyncio.run(run_channel_check(int(row["id"])))
                    except Exception:
                        logger.exception("module=telegram_channel channel_id=%s scheduled check failed", row["id"])
            business = get_business_settings()
            if business["subscription_auto_transfer"] and int(time.time()) >= _NEXT_SUBSCRIPTION_CHECK:
                with database() as conn:
                    auth = conn.execute("SELECT p115_cookie FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
                    subscriptions = conn.execute("SELECT id FROM web_subscriptions WHERE installation_id=? AND status IN ('active','waiting_output','resource_found','failed') ORDER BY COALESCE(last_run_at,0) ASC LIMIT 25", (WEB_INSTALLATION_ID,)).fetchall() if auth and auth["p115_cookie"] else []
                for subscription in subscriptions:
                    try:
                        asyncio.run(execute_subscription(WEB_INSTALLATION_ID, int(subscription["id"])))
                    except Exception:
                        logger.exception("module=subscriptions operation=scheduled_run subscription=%s", subscription["id"])
                _NEXT_SUBSCRIPTION_CHECK = int(time.time()) + int(business["subscription_interval"])
            if int(time.time()) >= _NEXT_OFFLINE_CHECK:
                with database() as conn:
                    auth = conn.execute("SELECT p115_cookie FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
                if auth and auth["p115_cookie"] and business["offline_enabled"]:
                    try:
                        remote = asyncio.run(P115Client(decrypt(auth["p115_cookie"]), REQUEST_TIMEOUT).offline_tasks())
                        now = int(time.time())
                        for task in remote:
                            remote_id = str(task.get("info_hash") or task.get("task_id") or task.get("id") or "")
                            if not remote_id:
                                continue
                            raw_status = task.get("status")
                            percent = float(task.get("percentDone") or task.get("percent") or 0)
                            status = "completed" if percent >= 100 or raw_status in (2, "completed", "success") else ("failed" if raw_status in (-1, "failed", "error") else "downloading")
                            with database() as conn:
                                conn.execute("UPDATE offline_tasks SET status=?,error=?,updated_at=? WHERE installation_id=? AND provider_task_id=?", (status, str(task.get("error") or ""), now, WEB_INSTALLATION_ID, remote_id))
                    except Exception:
                        logger.exception("module=offline provider=115 operation=poll")
                _NEXT_OFFLINE_CHECK = int(time.time()) + int(business["ed2k_poll_interval"])
            intervals = [int(row["check_interval"] or 600) for row in _channel_rows() if row["enabled"]] or [600]
            time.sleep(min(30, max(5, min(intervals) // 10)))
        except Exception:
            logger.exception("channel worker loop failed")
            time.sleep(30)


def start_channel_worker() -> None:
    global _CHANNEL_WORKER_STARTED, _TELEGRAM_BOT_WORKER_STARTED
    if _CHANNEL_WORKER_STARTED:
        return
    _CHANNEL_WORKER_STARTED = True
    threading.Thread(target=_channel_worker, name="moon-channel-monitor", daemon=True).start()
    if not _TELEGRAM_BOT_WORKER_STARTED:
        _TELEGRAM_BOT_WORKER_STARTED = True
        threading.Thread(target=_telegram_bot_worker, name="moon-telegram-bot", daemon=True).start()


async def _telegram_command(text: str) -> str:
    command, _, argument = text.strip().partition(" ")
    if command in {"/start", "/help"}:
        return "影舟 MovieArk 命令：\n/search 片名 - 搜索影视\n/subscribe movie|tv TMDB_ID 标题 - 建立订阅\n/status - 查看授权状态"
    if command == "/status":
        providers = get_authorizations()["providers"]
        return "\n".join(f"{key}: {'可用' if value.get('configured') or value.get('authorized') else '未配置'}" for key, value in providers.items())
    if command == "/search":
        keyword = argument.strip()
        if not keyword:
            return "用法：/search 影片名称"
        provider = explore_tmdb_provider()
        if not provider.configured:
            return "TMDB 尚未配置，请先到授权中心配置。"
        payload, _ = await provider.request("/search/multi", {"query": keyword, "include_adult": "false"}, ttl=120)
        lines = [f"“{keyword}”搜索结果："]
        for item in payload.get("results", [])[:8]:
            media_type = item.get("media_type")
            if media_type not in {"movie", "tv"}:
                continue
            title = str(item.get("title") or item.get("name") or "未命名")
            date = str(item.get("release_date") or item.get("first_air_date") or "")
            lines.append(f"{title} {date[:4]}\n/subscribe {media_type} {item.get('id')} {title}")
        return "\n\n".join(lines) if len(lines) > 1 else "没有找到匹配影视。"
    if command == "/subscribe":
        parts = argument.split(maxsplit=2)
        if len(parts) < 2 or parts[0] not in {"movie", "tv"} or not parts[1].isdigit():
            return "用法：/subscribe movie|tv TMDB_ID 标题"
        media_type, tmdb_id = parts[0], int(parts[1])
        title = parts[2] if len(parts) > 2 else f"TMDB {tmdb_id}"
        result = create_subscription(SubscriptionRequest(title=title, media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        return f"{'已存在' if result.get('duplicate') else '订阅成功'}：{title}"
    return "无法识别命令。发送 /help 查看可用功能。"


async def _poll_telegram_bot_once() -> None:
    config = _telegram_settings(True)
    if not config["enabled"] or not config["bot_token"]:
        return
    with database() as conn:
        row = conn.execute("SELECT telegram_update_offset FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    offset = int(row["telegram_update_offset"] or 0) if row else 0
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(f"https://api.telegram.org/bot{config['bot_token']}/getUpdates", params={"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])})
        payload = response.json()
    if not payload.get("ok"):
        raise NotificationError(str(payload.get("description") or "Telegram 更新读取失败"))
    latest = offset
    for update in payload.get("result", []):
        latest = max(latest, int(update.get("update_id", 0)) + 1)
        message = update.get("message") or {}
        sender = str((message.get("from") or {}).get("id") or "")
        if config["authorized_user_id"] and sender != str(config["authorized_user_id"]):
            logger.warning("module=telegram operation=command reason=unauthorized_user user=%s", sender)
            continue
        text = str(message.get("text") or "")
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if not text.startswith("/") or not chat_id:
            continue
        try:
            reply = await _telegram_command(text)
        except Exception as exc:
            logger.exception("module=telegram operation=command error_type=%s", type(exc).__name__)
            reply = f"命令执行失败：{type(exc).__name__}"
        await TelegramProvider(config["bot_token"], chat_id, REQUEST_TIMEOUT).send(reply)
    if latest != offset:
        with database() as conn:
            conn.execute("UPDATE web_settings SET telegram_update_offset=? WHERE installation_id=?", (latest, WEB_INSTALLATION_ID))


def _telegram_bot_worker() -> None:
    while True:
        try:
            asyncio.run(_poll_telegram_bot_once())
        except Exception:
            logger.exception("module=telegram operation=poll")
        time.sleep(5)


@app.get("/api/web/logs/modules")
def web_log_modules() -> dict[str, Any]:
    with database() as conn:
        rows = conn.execute("SELECT DISTINCT module FROM app_logs WHERE module!='' ORDER BY module").fetchall()
    return {"modules": [row["module"] for row in rows]}


@app.get("/api/web/logs")
def web_log_list(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), level: str = Query("", max_length=20), module: str = Query("", max_length=80), keyword: str = Query("", max_length=200), range_seconds: int = Query(0, ge=0)) -> dict[str, Any]:
    conditions: list[str] = []
    params: list[Any] = []
    if level.strip():
        conditions.append("level=?"); params.append(level.strip().upper())
    if module.strip():
        conditions.append("module=?"); params.append(module.strip())
    if keyword.strip():
        conditions.append("message LIKE ?"); params.append(f"%{keyword.strip()}%")
    if range_seconds and range_seconds > 0:
        conditions.append("ts>=?"); params.append(int(time.time()) - range_seconds)
    where = " AND ".join(conditions) if conditions else "1=1"
    with database() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM app_logs WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT id,ts,level,module,message FROM app_logs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size]).fetchall()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(x) for x in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "has_more": page < total_pages}


@app.get("/api/web/authorizations/p115/qr/apps")
def p115_qr_apps() -> dict[str, Any]:
    return {"items": [{"id": key, "name": value} for key, value in P115_QR_APPS.items()], "default": "web"}


@app.post("/api/web/authorizations/p115/qr/start")
async def p115_qr_start(request: P115QRStartRequest) -> dict[str, Any]:
    app_name = request.app.strip().lower()
    if app_name not in P115_QR_APPS:
        raise HTTPException(400, "不支持的115扫码设备类型")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/", params={"app": app_name})
    payload = response.json()
    data = payload.get("data") or {}
    uid = str(data.get("uid") or "")
    if not uid:
        raise HTTPException(502, "115 暂时无法生成二维码")
    qr_content = str(data.get("qrcode") or f"https://115.com/scan/dg-{uid}")
    image = qrcode.make(qr_content, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=3)
    buffer = io.BytesIO()
    image.save(buffer)
    qr_image = "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()
    P115_QR_SESSIONS[uid] = {"time": data.get("time"), "sign": data.get("sign"), "app": app_name, "created": int(time.time())}
    return {"ok": True, "session_id": uid, "app": app_name, "device": P115_QR_APPS[app_name], "qrcode": qr_content, "qr_image": qr_image}


@app.get("/api/web/authorizations/p115/qr/{session_id}")
async def p115_qr_status(session_id: str) -> dict[str, Any]:
    session = P115_QR_SESSIONS.get(session_id)
    if not session or int(time.time()) - int(session["created"]) > 600:
        raise HTTPException(410, "二维码已过期，请重新扫码")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get("https://qrcodeapi.115.com/get/status/", params={"uid": session_id, "time": session["time"], "sign": session["sign"]})
        payload = response.json()
        status = int((payload.get("data") or {}).get("status", 0))
        if status != 2:
            labels = {-2: "二维码已过期", -1: "已取消", 0: "等待扫码", 1: "已扫码，请在手机确认"}
            return {"ok": True, "completed": False, "status": status, "message": labels.get(status, "等待确认")}
        app_name = str(session.get("app") or "web")
        login = await client.post(f"https://passportapi.115.com/app/1.0/{app_name}/1.0/login/qrcode/", data={"account": session_id, "app": app_name})
    result = login.json()
    data = result.get("data") or {}
    cookie_data = data.get("cookie") or {}
    if isinstance(cookie_data, dict):
        cookie = "; ".join(f"{key}={value}" for key, value in cookie_data.items())
    else:
        cookie = str(cookie_data or "")
    if not cookie:
        raise HTTPException(502, str(result.get("message") or "115 登录成功但未返回 Cookie"))
    with database() as conn:
        conn.execute("UPDATE web_settings SET p115_cookie=?,updated_at=? WHERE installation_id=?", (encrypt(cookie), int(time.time()), WEB_INSTALLATION_ID))
    P115_QR_SESSIONS.pop(session_id, None)
    return {"ok": True, "completed": True, "message": "115 扫码授权成功"}


@app.get("/v1/settings")
def get_settings(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT moviepilot_url, save_directory, offline_enabled, tmdb_api_key, tmdb_language, tmdb_region, updated_at FROM web_settings WHERE installation_id = ?", (installation_id,)).fetchone()
    if not row:
        return {"moviepilot_url": "", "save_directory": "", "offline_enabled": True, "tmdb_api_key_configured": False, "tmdb_language": "zh-CN", "tmdb_region": "CN"}
    result = dict(row)
    result["tmdb_api_key_configured"] = bool(result.pop("tmdb_api_key", ""))
    return result


@app.put("/v1/settings")
def put_settings(request: WebSettings, installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        current = conn.execute("SELECT tmdb_api_key FROM web_settings WHERE installation_id = ?", (installation_id,)).fetchone()
        stored_tmdb_key = encrypt(request.tmdb_api_key.strip()) if request.tmdb_api_key.strip() else (current["tmdb_api_key"] if current else "")
        conn.execute("""INSERT INTO web_settings (installation_id, moviepilot_url, save_directory, offline_enabled, tmdb_api_key, tmdb_language, tmdb_region, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(installation_id) DO UPDATE SET moviepilot_url=excluded.moviepilot_url,
            save_directory=excluded.save_directory, offline_enabled=excluded.offline_enabled, tmdb_api_key=excluded.tmdb_api_key,
            tmdb_language=excluded.tmdb_language, tmdb_region=excluded.tmdb_region, updated_at=excluded.updated_at""",
            (installation_id, request.moviepilot_url.strip().rstrip("/"), request.save_directory.strip(), int(request.offline_enabled), stored_tmdb_key, request.tmdb_language.strip() or "zh-CN", request.tmdb_region.strip().upper() or "CN", int(time.time())))
    return {"ok": True, "tmdb_api_key_configured": bool(stored_tmdb_key), "moviepilot_url": request.moviepilot_url, "save_directory": request.save_directory, "offline_enabled": request.offline_enabled}


@app.get("/v1/transfers")
def list_transfers(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        rows = conn.execute("SELECT id, slug, name, share_url, status, error, created_at FROM transfer_records WHERE installation_id = ? ORDER BY id DESC LIMIT 100", (installation_id,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.delete("/v1/authorization")
def revoke(installation_id: str = Depends(require_installation)) -> dict[str, bool]:
    with database() as conn:
        conn.execute(
            "DELETE FROM installations WHERE installation_id = ?", (installation_id,)
        )
    return {"ok": True}

"""Small self-hosted HDHive OAuth and OpenAPI gateway.

App Secret and OAuth tokens never leave this service. MoviePilot authenticates
with an installation id/key and receives only normalized resource metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .explore import RANKINGS, TMDBProvider, filter_metadata, registry
from .explore_ui import explore_html
from .subscriptions_ui import subscriptions_html
from .tasks_ui import tasks_html
from .resource_ui import resource_detail_html
from .resources import HDHiveResourceProvider, ResourceProviderRegistry, filter_options
from .unlocks_ui import unlocks_html
from .settings_ui import settings_html
from .authorizations_ui import authorizations_html
from .telegram_ui import telegram_html
from .notifications import ChannelMonitor, NotificationError, NotificationService, TelegramProvider
from .p115 import P115Client, P115Error


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


BASE_URL = os.getenv("HDHIVE_BASE_URL", "https://hdhive.com").rstrip("/")
CLIENT_ID = required_env("HDHIVE_CLIENT_ID")
APP_SECRET = required_env("HDHIVE_APP_SECRET")
REDIRECT_URI = required_env("HDHIVE_REDIRECT_URI")
INSTALLATION_KEY = required_env("INSTALLATION_KEY")
WEB_INSTALLATION_ID = os.getenv("WEB_INSTALLATION_ID", "personal-web")
TOKEN_ENCRYPTION_KEY = required_env("TOKEN_ENCRYPTION_KEY")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/data/hdhive-oauth.db"))
OAUTH_SCOPE = os.getenv("HDHIVE_OAUTH_SCOPE", "meta query unlock").strip()
REQUEST_TIMEOUT = max(5, min(int(os.getenv("REQUEST_TIMEOUT", "30")), 120))
STATE_TTL = max(120, min(int(os.getenv("OAUTH_STATE_TTL", "600")), 3600))

try:
    CIPHER = Fernet(TOKEN_ENCRYPTION_KEY.encode("ascii"))
except (ValueError, TypeError) as exc:
    raise RuntimeError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

RESOURCE_RE = re.compile(
    r"^https?://(?:[A-Za-z0-9-]+\.)?hdhive\.com/resource/([A-Za-z0-9-]+)/?$",
    re.IGNORECASE,
)

app = FastAPI(
    title="HDHive OAuth Gateway",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
logger = logging.getLogger("hdhive-oauth")


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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_settings)")}
        for name, definition in (
            ("tmdb_api_key", "TEXT DEFAULT ''"),
            ("tmdb_language", "TEXT DEFAULT 'zh-CN'"),
            ("tmdb_region", "TEXT DEFAULT 'CN'"),
            ("root_directory", "TEXT DEFAULT ''"),
            ("scrape_directory", "TEXT DEFAULT ''"),
            ("save_wait_seconds", "INTEGER DEFAULT 30"),
            ("retry_count", "INTEGER DEFAULT 3"),
            ("duplicate_policy", "TEXT DEFAULT 'skip'"),
            ("ed2k_directory", "TEXT DEFAULT ''"),
            ("ed2k_poll_interval", "INTEGER DEFAULT 60"),
            ("ed2k_retry_count", "INTEGER DEFAULT 3"),
            ("ed2k_auto_archive", "INTEGER DEFAULT 1"),
            ("emby_url", "TEXT DEFAULT ''"),
            ("emby_api_key", "TEXT DEFAULT ''"),
            ("emby_user_id", "TEXT DEFAULT ''"),
            ("p115_cookie", "TEXT DEFAULT ''"),
            ("telegram_bot_token", "TEXT DEFAULT ''"),
            ("telegram_chat_id", "TEXT DEFAULT ''"),
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
        ):
            if name not in transfer_columns:
                conn.execute(f"ALTER TABLE transfer_records ADD COLUMN {name} {definition}")


@app.on_event("startup")
def startup() -> None:
    init_database()
    start_channel_worker()


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


@app.get("/explore", response_class=HTMLResponse)
def explore_page() -> HTMLResponse:
    return HTMLResponse(explore_html())


@app.get("/resources", response_class=HTMLResponse)
def resource_detail_page() -> HTMLResponse:
    return HTMLResponse(resource_detail_html())


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
    return {"data": filter_metadata(genres), "configured": tmdb.configured}


@app.get("/api/explore/discover")
async def explore_discover(
    provider: str = Query("tmdb"), media_type: str = Query("movie", pattern="^(movie|tv)$"),
    region: str = Query(""), language: str = Query(""), genre: str = Query(""), year: str = Query(""),
    sort: str = Query("popularity.desc"), rating: float = Query(0, ge=0, le=10), page: int = Query(1, ge=1),
) -> dict[str, Any]:
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
async def explore_rankings() -> dict[str, Any]:
    return {"items": [{"id": key, "name": name} for key, name in RANKINGS.items()]}


@app.get("/api/explore/ranking/{provider}/{ranking}")
async def explore_ranking(provider: str, ranking: str, page: int = Query(1, ge=1)) -> dict[str, Any]:
    tmdb = explore_tmdb_provider()
    if not tmdb.configured:
        return {"items": [], "configured": False, "error": "TMDB 尚未配置。"}
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
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>115 网盘转存助手</title>
<style>body{{margin:0;background:#111;color:#eee;font:16px system-ui}}main{{max-width:760px;margin:8vh auto;padding:36px;background:#1b1b1b;border:1px solid #55401b;border-radius:18px}}h1{{color:#d8b56a}}.ok,.warn{{padding:16px;border-radius:10px;margin:20px 0}}.ok{{background:#18351f;color:#8ee6a0}}.warn{{background:#3b2a13;color:#ffd27a}}a{{display:inline-block;background:#b99245;color:#fff;padding:12px 18px;border-radius:8px;text-decoration:none;margin:8px 8px 8px 0}}small{{color:#aaa}}</style>
<nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a></nav><main><h1>115 网盘转存助手</h1><p>个人版影巢授权中心</p>{state}<a href='{login_url}'>授权影巢账号</a><a href='/health'>检查服务</a>
<h2>资源查询</h2><form method='post' action='/web/query'><input name='media_type' value='movie' placeholder='movie 或 tv'><input name='tmdb_id' type='number' placeholder='TMDB ID' required><button>查询影巢资源</button></form>
<h2>资源解锁</h2><form method='post' action='/web/resolve'><input name='slug' placeholder='影巢资源 slug' required><input name='max_unlock_points' type='number' value='0' min='0'><button>解锁并获取 115 链接</button></form>
<p><small>当前仅供个人使用；授权 Token 保存在服务器，不会显示或提交到 GitHub。解锁后请在 MoviePilot 中执行现有 115 转存。</small></p></main>""")


def _web_layout(title: str, content: str) -> HTMLResponse:
    nav = "<nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a></nav>"
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><style>
body{{margin:0;background:#0d0b08;color:#eee;font:15px system-ui}}nav{{padding:18px 28px;background:#17120b;border-bottom:1px solid #654b20}}nav a{{color:#e2bd73;margin-right:22px;text-decoration:none}}main{{max-width:1250px;margin:28px auto;padding:28px;background:#17130d;border:1px solid #604821;border-radius:16px}}h1{{color:#e5c47d}}h2{{color:#d5b16b}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}.card{{padding:20px;background:#211a11;border:1px solid #4a381e;border-radius:12px}}.muted{{color:#aaa}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;border-bottom:1px solid #3e301d;text-align:left}}input,select,button{{padding:10px;margin:5px;background:#0f0d0a;color:#eee;border:1px solid #896b36;border-radius:6px}}button{{color:#f1cd80;cursor:pointer}}a.btn{{display:inline-block;padding:10px 14px;background:#76531f;color:#fff;border-radius:6px;text-decoration:none}}</style>{nav}<main><h1>{html.escape(title)}</h1>{content}</main>""")


def _web_account_card() -> str:
    try:
        row = load_installation(WEB_INSTALLATION_ID)
        user = json.loads(row["user_json"] or "{}")
        return f"<div class='card'>Authorized: <b>{html.escape(str(user.get('nickname') or user.get('username') or 'account'))}</b>　Points: <b>{html.escape(str(user.get('points') or 'unknown'))}</b></div>"
    except HTTPException:
        return "<div class='card'>Not authorized. <a class='btn' href='/oauth/login'>Authorize HDHive</a></div>"


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
    content = _web_account_card() + "<div class='card'><h2>Resource discovery</h2><p>Search directly by title; the API returns matching HDHive resources.</p><form method='post' action='/web/search'><input name='keyword' placeholder='Resource name' required><select name='media_type'><option value='movie'>Movie</option><option value='tv'>TV</option></select><button>Search HDHive</button></form><hr><p class='muted'>Advanced: use a TMDB ID when you need an exact match.</p><form method='post' action='/web/query'><select name='media_type'><option value='movie'>Movie</option><option value='tv'>TV</option></select><input name='tmdb_id' type='number' placeholder='TMDB ID' required><button>Exact query</button></form></div>"
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
    return HTMLResponse(f"""<!doctype html><meta charset='utf-8'><title>{title}</title><style>body{{margin:0;background:#111;color:#eee;font:16px system-ui}}nav{{padding:14px;background:#191919;border-bottom:1px solid #55401b}}nav a{{color:#d8b56a;margin-right:18px;text-decoration:none}}main{{max-width:1100px;margin:40px auto;padding:32px;background:#1b1b1b;border:1px solid #55401b;border-radius:18px}}h1{{color:#d8b56a}}.card{{padding:24px;background:#242019;border-radius:12px}}</style><nav><a href='/'>首页</a><a href='/web/rankings'>榜单</a><a href='/web/discover'>资源发现</a><a href='/web/subscriptions'>订阅列表</a><a href='/web/tasks'>订阅任务</a><a href='/web/unlocks'>解锁记录</a><a href='/web/settings'>设置</a></nav><main><h1>{html.escape(title)}</h1><div class='card'>{html.escape(description)}<br><br>该模块正在接入现有 MoviePilot、影巢和 115 数据。</div></main>""")


@app.get("/oauth/login")
def oauth_login() -> RedirectResponse:
    expires = int(time.time()) + 600
    signature = sign_start(WEB_INSTALLATION_ID, expires)
    return RedirectResponse(
        f"/oauth/start?installation_id={quote(WEB_INSTALLATION_ID)}&expires={expires}&signature={quote(signature)}"
    )


def _result_page(title: str, payload: Any, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<meta charset='utf-8'><style>body{{background:#111;color:#eee;font:16px system-ui;padding:30px}}pre{{white-space:pre-wrap;background:#222;padding:18px;border-radius:10px}}a{{color:#e0bd70}}</style><h2>{html.escape(title)}</h2><pre>{html.escape(json.dumps(payload, ensure_ascii=False, indent=2, default=str))}</pre><a href='/'>返回首页</a>",
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
        body = f"<div class='empty'><h3>影巢暂时没有《{safe_title}》的可用资源</h3><p>这不是程序报错，只是影巢当前没有返回匹配链接。可以稍后再试，或返回遨游选择其他影片。</p></div>"
    subscribe = ""
    if media_type in ("movie", "tv") and tmdb_id > 0:
        subscribe = ("<form method='post' action='/web/subscribe' style='display:inline-block;margin-right:10px'>"
                     f"<input type='hidden' name='title' value='{html.escape(title, quote=True)}'><input type='hidden' name='media_type' value='{media_type}'>"
                     f"<input type='hidden' name='tmdb_id' value='{tmdb_id}'><input type='hidden' name='year' value='{html.escape(year, quote=True)}'>"
                     f"<input type='hidden' name='poster' value='{html.escape(poster, quote=True)}'><button>＋ 订阅这部影视</button></form>")
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{safe_title} - 影巢资源</title><style>body{{margin:0;background:#0b0906;color:#eee;font:16px system-ui,'Microsoft YaHei';padding:28px}}main{{max-width:920px;margin:auto}}h1,h3{{color:#e1bd70}}article,.empty{{background:#1d1912;border:1px solid #59401e;border-radius:14px;padding:18px;margin:14px 0}}button,.back{{display:inline-block;border:1px solid #b28036;border-radius:9px;background:#2a1d0d;color:#ffd98d;padding:10px 16px;text-decoration:none;cursor:pointer}}p{{color:#bdb3a3;line-height:1.7}}</style>"
        f"<main><h1>《{safe_title}》影巢资源</h1>{body}{subscribe}<a class='back' href='/explore'>返回遨游</a></main></html>",
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
    try:
        if request.max_unlock_points is not None:
            detail = await authorized_request(
                installation_id, "GET", f"/api/open/shares/{quote(slug)}"
            )
            unlocked = bool(detail.get("is_unlocked") or detail.get("is_free_for_user"))
            points = int(
                detail.get("actual_unlock_points") or detail.get("unlock_points") or 0
            )
            if not unlocked and points > request.max_unlock_points:
                raise HTTPException(
                    409,
                    f"UNLOCK_BUDGET_EXCEEDED: need {points}, limit {request.max_unlock_points}",
                )
        data = await authorized_request(
            installation_id,
            "POST",
            "/api/open/resources/unlock",
            body={"slug": slug},
        )
    except HDHiveAPIError as exc:
        raise HTTPException(exc.status, f"{exc.code}: {exc.message}") from exc
    url = str(data.get("full_url") or data.get("url") or "").strip()
    name = str(data.get("title") or data.get("name") or slug)
    now = int(time.time())
    with database() as conn:
        conn.execute(
            """INSERT INTO transfer_records
               (installation_id,slug,name,share_url,status,resolution,quality,size,uploader,points,action,processing_status,source_type,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (installation_id, slug, name, url, "resolved", str(data.get("resolution") or ""),
             str(data.get("quality") or ""), str(data.get("size") or data.get("file_size") or ""),
             str(data.get("uploader") or data.get("author") or ""), int(data.get("unlock_points") or 0),
             "资源解锁", "resolved", "115网盘", now, now),
        )
    return {
        "name": name,
        "share_url": url,
        "password": str(data.get("access_code") or ""),
        "files": data.get("files") if isinstance(data.get("files"), list) else [],
        "slug": slug,
        "already_owned": bool(data.get("already_owned")),
    }


class SubscriptionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    media_type: str = Field(pattern="^(movie|tv)$")
    tmdb_id: int = Field(gt=0)
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

    async def hdhive_query(media_type: str, tmdb_id: int, title: str) -> dict[str, Any]:
        data = await query_resources(ResourceQuery(media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        if not _resource_items(data) and title.strip():
            try:
                searched = await search_resources(SearchRequest(keyword=title.strip(), media_type=media_type), WEB_INSTALLATION_ID)
                if _resource_items(searched):
                    return searched
            except HTTPException as exc:
                logger.warning("module=resources provider=hdhive operation=title_search error_type=%s reason=%s", type(exc).__name__, exc.detail)
        return data

    return ResourceProviderRegistry([HDHiveResourceProvider(hdhive_query, configured=configured)])


@app.get("/api/web/resources/search")
async def web_resource_search(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(..., gt=0), title: str = Query("", max_length=200)) -> dict[str, Any]:
    providers = resource_registry()
    items, errors = await providers.search(media_type, tmdb_id, title)
    for error in errors:
        logger.error("module=resources provider=%s operation=search error_type=ProviderError reason=%s", error["provider"], error["error"])
    return {"items": [item.model_dump() for item in items], "filters": filter_options(items), "providers": providers.infos(), "errors": errors, "total": len(items)}


class WebResourceTransfer(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    resource_id: str = Field(min_length=1, max_length=200)


@app.post("/api/web/resources/transfer")
async def web_resource_transfer(request: WebResourceTransfer) -> dict[str, Any]:
    if request.provider != "hdhive":
        raise HTTPException(400, "该资源来源暂不支持转存")
    try:
        result = await resolve_resource(ResolveRequest(slug=request.resource_id, max_unlock_points=None), WEB_INSTALLATION_ID)
        with database() as conn:
            settings = conn.execute("SELECT p115_cookie,save_directory FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        if not settings or not settings["p115_cookie"]:
            raise HTTPException(409, "影巢资源已解锁，但115尚未授权；请到授权中心配置 Cookie 后重试")
        client = P115Client(decrypt(settings["p115_cookie"]), REQUEST_TIMEOUT)
        transferred = await client.transfer(result["share_url"], settings["save_directory"] or "")
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='completed',processing_status='completed',save_path=?,updated_at=? WHERE installation_id=? AND slug=? AND id=(SELECT MAX(id) FROM transfer_records WHERE installation_id=? AND slug=?)",
                         (settings["save_directory"] or "", int(time.time()), WEB_INSTALLATION_ID, request.resource_id, WEB_INSTALLATION_ID, request.resource_id))
        try:
            service = notification_service()
            config = _telegram_settings(False)
            if config["enabled"]:
                await service.notify("transfer_success", {"title": result["name"], "status": "转存成功", "resource": request.resource_id, "resolution": "", "size": "", "save_path": settings["save_directory"] or "115根目录", "error": ""})
        except Exception:
            logger.exception("module=notifications provider=telegram operation=transfer_success")
        return {**result, **transferred, "provider": request.provider, "transfer_status": "completed"}
    except P115Error as exc:
        logger.error("module=resources provider=115 operation=transfer error_type=P115Error reason=%s", exc)
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='failed',processing_status='failed',error=?,updated_at=? WHERE installation_id=? AND slug=?", (str(exc), int(time.time()), WEB_INSTALLATION_ID, request.resource_id))
        raise HTTPException(502, str(exc)) from exc
    except HTTPException as exc:
        logger.error("module=resources provider=%s operation=transfer error_type=%s reason=%s", request.provider, type(exc).__name__, exc.detail)
        raise


@app.get("/api/web/subscription-status")
def web_subscription_status(media_type: str = Query(..., pattern="^(movie|tv)$"), tmdb_id: int = Query(..., gt=0)) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT id,status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND tmdb_id=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1", (WEB_INSTALLATION_ID, media_type, tmdb_id)).fetchone()
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
        existing = conn.execute(
            "SELECT id, status FROM web_subscriptions WHERE installation_id=? AND media_type=? AND tmdb_id=? AND COALESCE(season,'')=? AND status NOT IN ('cancelled','expired') ORDER BY id DESC LIMIT 1",
            (installation_id, request.media_type, request.tmdb_id, request.season.strip()),
        ).fetchone()
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
def web_create_subscription(request: SubscriptionRequest) -> dict[str, Any]:
    return create_subscription(request, WEB_INSTALLATION_ID)


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
        count = len(_resource_items(payload))
        new_status = "resource_found" if count else "waiting_output"
        error = ""
    except HTTPException as exc:
        count = 0; new_status = "failed"; error = str(exc.detail)
    now = int(time.time())
    with database() as conn:
        cur = conn.execute("INSERT INTO subscription_runs (installation_id,subscription_id,status,resource_count,error,created_at) VALUES (?,?,?,?,?,?)", (installation_id, subscription_id, new_status, count, error, now))
        conn.execute("UPDATE web_subscriptions SET status=?, error=?, updated_at=? WHERE id=?", (new_status, error, now, subscription_id))
    return {"ok": new_status != "failed", "run_id": cur.lastrowid, "status": new_status, "resource_count": count, "error": error}


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


def unlock_page(installation_id: str, page: int = 1, page_size: int = 15, search: str = "", action: str = "", status: str = "", processing_status: str = "") -> dict[str, Any]:
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
    where = " AND ".join(conditions)
    with database() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM transfer_records WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"""SELECT id,subscription_id,slug,name,share_url,status,error,media_type,tmdb_id,resolution,quality,size,uploader,points,action,save_path,processing_status,source_type,created_at,updated_at
            FROM transfer_records WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?""", [*params, page_size, (page - 1) * page_size]).fetchall()
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {"items": [dict(x) for x in rows], "page": page, "page_size": page_size, "total": total, "total_pages": total_pages, "has_more": page < total_pages}


@app.get("/api/web/unlocks")
def web_unlock_list(page: int = Query(1, ge=1), page_size: int = Query(15, ge=1, le=100), search: str = Query("", max_length=100), action: str = Query("", max_length=50), status: str = Query("", max_length=40), processing_status: str = Query("", max_length=40)) -> dict[str, Any]:
    return unlock_page(WEB_INSTALLATION_ID, page, page_size, search, action, status, processing_status)


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
        row = conn.execute("SELECT slug FROM transfer_records WHERE id=? AND installation_id=?", (record_id, WEB_INSTALLATION_ID)).fetchone()
    if not row:
        raise HTTPException(404, "解锁记录不存在")
    try:
        result = await resolve_resource(ResolveRequest(slug=row["slug"], max_unlock_points=None), WEB_INSTALLATION_ID)
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='resolved',processing_status='resolved',share_url=?,error='',updated_at=? WHERE id=?", (result.get("share_url", ""), int(time.time()), record_id))
        return {"ok": True, "share_url": result.get("share_url", "")}
    except HTTPException as exc:
        with database() as conn:
            conn.execute("UPDATE transfer_records SET status='failed',processing_status='failed',error=?,updated_at=? WHERE id=?", (str(exc.detail), int(time.time()), record_id))
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
    scrape_directory: str = Field(default="", max_length=500)
    save_wait_seconds: int = Field(default=30, ge=0, le=3600)
    retry_count: int = Field(default=3, ge=0, le=20)
    duplicate_policy: str = Field(default="skip", pattern="^(skip|rename|overwrite)$")
    offline_enabled: bool = True
    ed2k_directory: str = Field(default="", max_length=500)
    ed2k_poll_interval: int = Field(default=60, ge=10, le=3600)
    ed2k_retry_count: int = Field(default=3, ge=0, le=20)
    ed2k_auto_archive: bool = True


BUSINESS_SETTING_COLUMNS = (
    "root_directory,save_directory,scrape_directory,save_wait_seconds,retry_count,"
    "duplicate_policy,offline_enabled,ed2k_directory,ed2k_poll_interval,"
    "ed2k_retry_count,ed2k_auto_archive"
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
    return result


@app.put("/api/web/settings/business")
def put_business_settings(request: BusinessSettings) -> dict[str, Any]:
    values = request.model_dump()
    with database() as conn:
        conn.execute(
            """INSERT INTO web_settings
            (installation_id,root_directory,save_directory,scrape_directory,save_wait_seconds,
             retry_count,duplicate_policy,offline_enabled,ed2k_directory,ed2k_poll_interval,
             ed2k_retry_count,ed2k_auto_archive,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(installation_id) DO UPDATE SET
             root_directory=excluded.root_directory,save_directory=excluded.save_directory,
             scrape_directory=excluded.scrape_directory,save_wait_seconds=excluded.save_wait_seconds,
             retry_count=excluded.retry_count,duplicate_policy=excluded.duplicate_policy,
             offline_enabled=excluded.offline_enabled,ed2k_directory=excluded.ed2k_directory,
             ed2k_poll_interval=excluded.ed2k_poll_interval,
             ed2k_retry_count=excluded.ed2k_retry_count,
             ed2k_auto_archive=excluded.ed2k_auto_archive,updated_at=excluded.updated_at""",
            (WEB_INSTALLATION_ID, values["root_directory"].strip(), values["save_directory"].strip(),
             values["scrape_directory"].strip(), values["save_wait_seconds"], values["retry_count"],
             values["duplicate_policy"], int(values["offline_enabled"]), values["ed2k_directory"].strip(),
             values["ed2k_poll_interval"], values["ed2k_retry_count"],
             int(values["ed2k_auto_archive"]), int(time.time())),
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
        return await P115Client(decrypt(row["p115_cookie"]), REQUEST_TIMEOUT).offline(request.url, settings["ed2k_directory"])
    except P115Error as exc:
        logger.error("module=offline provider=115 operation=add_task error_type=P115Error reason=%s", exc)
        raise HTTPException(502, str(exc)) from exc


class AuthorizationUpdate(BaseModel):
    api_key: str = Field(default="", max_length=4000)
    cookie: str = Field(default="", max_length=16000)
    url: str = Field(default="", max_length=500)
    user_id: str = Field(default="", max_length=200)
    language: str = Field(default="zh-CN", max_length=16)


def _decrypt_optional(value: str | None) -> str:
    return decrypt(value) if value else ""


def _authorization_rows() -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    init_database()
    with database() as conn:
        settings = conn.execute("SELECT * FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        installation = conn.execute("SELECT * FROM installations WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    return settings, installation


@app.get("/api/web/authorizations")
def get_authorizations() -> dict[str, Any]:
    settings, installation = _authorization_rows()
    user: dict[str, Any] = {}
    if installation:
        try:
            user = json.loads(installation["user_json"] or "{}")
        except ValueError:
            user = {}
    return {"providers": {
        "hdhive": {"configured": True, "authorized": bool(installation),
                    "summary": str(user.get("nickname") or user.get("name") or user.get("username") or "等待授权")},
        "p115": {"configured": bool(settings and settings["p115_cookie"]), "authorized": bool(settings and settings["p115_cookie"]),
                 "summary": "Cookie 已安全保存" if settings and settings["p115_cookie"] else "未配置 Cookie"},
        "emby": {"configured": bool(settings and settings["emby_url"] and settings["emby_api_key"]),
                 "authorized": False, "url": settings["emby_url"] if settings else "", "user_id": settings["emby_user_id"] if settings else "",
                 "summary": settings["emby_url"] if settings and settings["emby_url"] else "未配置服务器"},
        "tmdb": {"configured": bool(settings and settings["tmdb_api_key"]), "authorized": False,
                 "language": settings["tmdb_language"] if settings else "zh-CN", "summary": "密钥已安全保存" if settings and settings["tmdb_api_key"] else "未配置 API Key"},
    }}


@app.put("/api/web/authorizations/{provider}")
def put_authorization(provider: str, request: AuthorizationUpdate) -> dict[str, Any]:
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
    return {"ok": True, "configured": bool(secret)}


@app.post("/api/web/authorizations/{provider}/test")
async def test_authorization(provider: str) -> dict[str, Any]:
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
        cookie = _decrypt_optional(settings["p115_cookie"])
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
        return {"ok": True, "message": "115 Cookie 有效"}
    if provider == "emby":
        url, key = settings["emby_url"], _decrypt_optional(settings["emby_api_key"])
        if not url or not key:
            raise HTTPException(409, "请先配置 Emby 地址和 API Key")
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(f"{url.rstrip('/')}/System/Info", headers={"X-Emby-Token": key})
        if response.status_code >= 400:
            raise HTTPException(502, f"Emby 连接失败（HTTP {response.status_code}）")
        info = response.json()
        return {"ok": True, "message": f"Emby 连接正常：{info.get('ServerName','服务器')} {info.get('Version','')}"}
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
        return {"ok": True, "message": "TMDB 连接正常"}
    raise HTTPException(404, "Provider 不存在")


DEFAULT_TELEGRAM_EVENTS = {"transfer_success": True, "transfer_failed": True, "subscription": True, "manual_review": True}
CHANNEL_MONITOR = ChannelMonitor(REQUEST_TIMEOUT)
_CHANNEL_WORKER_STARTED = False
P115_QR_SESSIONS: dict[str, dict[str, Any]] = {}


class TelegramSettings(BaseModel):
    bot_token: str = Field(default="", max_length=4000)
    chat_id: str = Field(default="", max_length=200)
    enabled: bool = False
    events: dict[str, bool] = Field(default_factory=lambda: dict(DEFAULT_TELEGRAM_EVENTS))
    template: str = Field(default="", max_length=5000)
    channel_enabled: bool = False
    channel_name: str = Field(default="oneonefivewpfx", max_length=200)
    channel_interval: int = Field(default=600, ge=60, le=86400)


def _telegram_settings(include_secret: bool = False) -> dict[str, Any]:
    init_database()
    with database() as conn:
        row = conn.execute("SELECT telegram_bot_token,telegram_chat_id,telegram_enabled,telegram_events,telegram_template,channel_enabled,channel_name,channel_interval FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    if not row:
        return {"bot_token": "", "bot_token_configured": False, "chat_id": "", "enabled": False, "events": dict(DEFAULT_TELEGRAM_EVENTS), "template": "", "channel_enabled": False, "channel_name": "oneonefivewpfx", "channel_interval": 600}
    try:
        events = {**DEFAULT_TELEGRAM_EVENTS, **json.loads(row["telegram_events"] or "{}")}
    except ValueError:
        events = dict(DEFAULT_TELEGRAM_EVENTS)
    token = _decrypt_optional(row["telegram_bot_token"])
    return {"bot_token": token if include_secret else "", "bot_token_configured": bool(token), "chat_id": row["telegram_chat_id"], "enabled": bool(row["telegram_enabled"]), "events": events, "template": row["telegram_template"], "channel_enabled": bool(row["channel_enabled"]), "channel_name": row["channel_name"], "channel_interval": row["channel_interval"] or 600}


@app.get("/api/web/telegram/settings")
def get_telegram_settings() -> dict[str, Any]:
    return _telegram_settings(False)


@app.put("/api/web/telegram/settings")
def put_telegram_settings(request: TelegramSettings) -> dict[str, Any]:
    init_database()
    with database() as conn:
        row = conn.execute("SELECT telegram_bot_token FROM web_settings WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
        token = encrypt(request.bot_token.strip()) if request.bot_token.strip() else (row["telegram_bot_token"] if row else "")
        conn.execute("""INSERT INTO web_settings (installation_id,telegram_bot_token,telegram_chat_id,telegram_enabled,telegram_events,telegram_template,channel_enabled,channel_name,channel_interval,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET telegram_bot_token=excluded.telegram_bot_token,telegram_chat_id=excluded.telegram_chat_id,telegram_enabled=excluded.telegram_enabled,telegram_events=excluded.telegram_events,telegram_template=excluded.telegram_template,channel_enabled=excluded.channel_enabled,channel_name=excluded.channel_name,channel_interval=excluded.channel_interval,updated_at=excluded.updated_at""",
            (WEB_INSTALLATION_ID, token, request.chat_id.strip(), int(request.enabled), json.dumps(request.events), request.template, int(request.channel_enabled), request.channel_name.strip(), request.channel_interval, int(time.time())))
    return {"ok": True, "bot_token_configured": bool(token)}


def notification_service() -> NotificationService:
    config = _telegram_settings(True)
    return NotificationService(TelegramProvider(config["bot_token"], config["chat_id"], REQUEST_TIMEOUT), config["events"], config["template"])


@app.post("/api/web/telegram/test")
async def test_telegram() -> dict[str, Any]:
    config = _telegram_settings(True)
    if not config["bot_token"] or not config["chat_id"]:
        raise HTTPException(409, "请先配置 Bot Token 和 Chat ID")
    try:
        await TelegramProvider(config["bot_token"], config["chat_id"], REQUEST_TIMEOUT).send("Moon Dream Telegram 通知测试成功")
    except NotificationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "message": "测试消息已发送"}


def _channel_status() -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT * FROM channel_state WHERE installation_id=?", (WEB_INSTALLATION_ID,)).fetchone()
    return dict(row) if row else {"last_message_id": 0, "last_check": 0, "next_check": 0, "last_status": "等待运行", "last_error": "", "processed": 0}


@app.get("/api/web/telegram/channel/status")
def channel_status() -> dict[str, Any]:
    return _channel_status()


async def run_channel_check() -> dict[str, Any]:
    config = _telegram_settings(False)
    state = _channel_status()
    now = int(time.time())
    processed, latest, error = 0, int(state.get("last_message_id") or 0), ""
    try:
        messages = await CHANNEL_MONITOR.fetch(config["channel_name"], latest)
        for message in messages:
            latest = max(latest, message.message_id)
            for link in message.links:
                try:
                    if "hdhive.com/resource/" in link:
                        slug = link.split("/resource/", 1)[1].split("?", 1)[0]
                        await resolve_resource(ResolveRequest(slug=slug, max_unlock_points=None), WEB_INSTALLATION_ID)
                    else:
                        logger.info("channel direct share detected message=%s", message.message_id)
                    processed += 1
                except Exception as exc:  # one bad message must not stop monitoring
                    logger.exception("channel item failed message=%s", message.message_id)
                    error = str(exc)
        status = f"检查完成，发现 {len(messages)} 条新消息，处理 {processed} 个资源"
    except Exception as exc:
        logger.exception("channel monitor failed")
        status, error = "检查失败", str(exc)
    with database() as conn:
        conn.execute("""INSERT INTO channel_state (installation_id,last_message_id,last_check,next_check,last_status,last_error,processed)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET last_message_id=excluded.last_message_id,last_check=excluded.last_check,next_check=excluded.next_check,last_status=excluded.last_status,last_error=excluded.last_error,processed=channel_state.processed+excluded.processed""",
            (WEB_INSTALLATION_ID, latest, now, now + int(config["channel_interval"]), status, error[:1000], processed))
    return {"ok": not bool(error), "message": status if not error else f"{status}：{error}", "processed": processed}


@app.post("/api/web/telegram/channel/check")
async def channel_check() -> dict[str, Any]:
    return await run_channel_check()


def _channel_worker() -> None:
    while True:
        try:
            config = _telegram_settings(False)
            state = _channel_status()
            if config["channel_enabled"] and int(state.get("next_check") or 0) <= int(time.time()):
                asyncio.run(run_channel_check())
            time.sleep(min(30, max(5, int(config["channel_interval"]) // 10)))
        except Exception:
            logger.exception("channel worker loop failed")
            time.sleep(30)


def start_channel_worker() -> None:
    global _CHANNEL_WORKER_STARTED
    if _CHANNEL_WORKER_STARTED:
        return
    _CHANNEL_WORKER_STARTED = True
    threading.Thread(target=_channel_worker, name="moon-channel-monitor", daemon=True).start()


@app.post("/api/web/authorizations/p115/qr/start")
async def p115_qr_start() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get("https://qrcodeapi.115.com/api/1.0/web/1.0/token/", params={"app": "web"})
    payload = response.json()
    data = payload.get("data") or {}
    uid = str(data.get("uid") or "")
    if not uid:
        raise HTTPException(502, "115 暂时无法生成二维码")
    P115_QR_SESSIONS[uid] = {"time": data.get("time"), "sign": data.get("sign"), "created": int(time.time())}
    return {"ok": True, "session_id": uid, "qrcode": data.get("qrcode") or f"https://115.com/scan/dg-{uid}"}


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
        login = await client.post("https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/", data={"account": session_id, "app": "web"})
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

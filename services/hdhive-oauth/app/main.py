"""Small self-hosted HDHive OAuth and OpenAPI gateway.

App Secret and OAuth tokens never leave this service. MoviePilot authenticates
with an installation id/key and receives only normalized resource metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(web_settings)")}
        for name, definition in (
            ("tmdb_api_key", "TEXT DEFAULT ''"),
            ("tmdb_language", "TEXT DEFAULT 'zh-CN'"),
            ("tmdb_region", "TEXT DEFAULT 'CN'"),
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
        if "subscription_id" not in transfer_columns:
            conn.execute("ALTER TABLE transfer_records ADD COLUMN subscription_id INTEGER")


@app.on_event("startup")
def startup() -> None:
    init_database()


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


@app.get("/web/unlocks", response_class=HTMLResponse)
def web_unlocks() -> HTMLResponse:
    with database() as conn:
        rows = conn.execute("SELECT name, slug, share_url, status, error, created_at FROM transfer_records WHERE installation_id = ? ORDER BY id DESC LIMIT 100", (WEB_INSTALLATION_ID,)).fetchall()
    table = "".join(f"<tr><td>{html.escape(str(r['name']))}</td><td>{html.escape(str(r['slug']))}</td><td>{html.escape(str(r['status']))}</td><td>{html.escape(str(r['share_url'] or r['error']))}</td><td>{r['created_at']}</td></tr>" for r in rows) or "<tr><td colspan='5'>No unlock records yet</td></tr>"
    return _web_layout("解锁与转存记录", _web_account_card() + "<div class='card'><table><tr><th>名称</th><th>资源标识</th><th>状态</th><th>115链接/错误</th><th>时间</th></tr>" + table + "</table></div>")


@app.get("/web/tasks", response_class=HTMLResponse)
def web_tasks() -> HTMLResponse:
    with database() as conn:
        transfers = conn.execute("SELECT COUNT(*), SUM(status='resolved'), SUM(status='failed') FROM transfer_records WHERE installation_id = ?", (WEB_INSTALLATION_ID,)).fetchone()
        subs = conn.execute("SELECT COUNT(*) FROM web_subscriptions WHERE installation_id = ? AND status = 'active'", (WEB_INSTALLATION_ID,)).fetchone()[0]
    content = _web_account_card() + f"<div class='grid'><div class='card'><h2>Subscription tasks</h2><p>Active subscriptions: <b>{subs}</b></p><p class='muted'>MoviePilot performs scheduled subscription matching and 115 saving.</p></div><div class='card'><h2>Transfer execution</h2><p>Total: {transfers[0] or 0}</p><p>Success: {transfers[1] or 0}　Failed: {transfers[2] or 0}</p><a class='btn' href='/web/unlocks'>View execution details</a></div></div>"
    return _web_layout("订阅任务", content)


@app.get("/web/settings", response_class=HTMLResponse)
def web_settings() -> HTMLResponse:
    with database() as conn:
        row = conn.execute("SELECT moviepilot_url, save_directory, offline_enabled, tmdb_api_key, tmdb_language, tmdb_region FROM web_settings WHERE installation_id = ?", (WEB_INSTALLATION_ID,)).fetchone()
    settings = dict(row) if row else {"moviepilot_url": "", "save_directory": "", "offline_enabled": 1, "tmdb_api_key": "", "tmdb_language": "zh-CN", "tmdb_region": "CN"}
    key_hint = "已配置，留空表示不修改" if settings.get("tmdb_api_key") else "填写 TMDB API Read Access Token 或 API Key"
    content = _web_account_card() + f"<div class='card'><h2>TMDB 榜单配置</h2><form method='post' action='/web/settings'><label>TMDB API Key</label><br><input type='password' name='tmdb_api_key' value='' size='60' placeholder='{key_hint}'><br><label>语言</label><input name='tmdb_language' value='{html.escape(str(settings['tmdb_language']))}'><label>地区</label><input name='tmdb_region' value='{html.escape(str(settings['tmdb_region']))}'><hr><h2>转存配置</h2><label>MoviePilot 地址</label><br><input name='moviepilot_url' value='{html.escape(str(settings['moviepilot_url']))}' size='60'><br><label>115 保存目录</label><br><input name='save_directory' value='{html.escape(str(settings['save_directory']))}' size='60'><br><label><input type='checkbox' name='offline_enabled' {'checked' if settings['offline_enabled'] else ''}> 启用磁力和 ed2k 离线下载</label><br><button>保存全部设置</button></form></div>"
    return _web_layout("设置", content)


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


def _resource_page(title: str, payload: Any, status_code: int = 200) -> HTMLResponse:
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
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{safe_title} - 影巢资源</title><style>body{{margin:0;background:#0b0906;color:#eee;font:16px system-ui,'Microsoft YaHei';padding:28px}}main{{max-width:920px;margin:auto}}h1,h3{{color:#e1bd70}}article,.empty{{background:#1d1912;border:1px solid #59401e;border-radius:14px;padding:18px;margin:14px 0}}button,.back{{display:inline-block;border:1px solid #b28036;border-radius:9px;background:#2a1d0d;color:#ffd98d;padding:10px 16px;text-decoration:none;cursor:pointer}}p{{color:#bdb3a3;line-height:1.7}}</style>"
        f"<main><h1>《{safe_title}》影巢资源</h1>{body}<a class='back' href='/explore'>返回遨游</a></main></html>",
        status_code=status_code,
    )


@app.post("/web/query", response_class=HTMLResponse)
async def web_query(media_type: str = Form(...), tmdb_id: int = Form(...), title: str = Form("")) -> HTMLResponse:
    try:
        data = await query_resources(ResourceQuery(media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        if not _resource_items(data) and title.strip():
            try:
                searched = await search_resources(SearchRequest(keyword=title.strip(), media_type=media_type), WEB_INSTALLATION_ID)
                if _resource_items(searched):
                    data = searched
            except HTTPException:
                pass
        return _resource_page(title.strip() or f"TMDB {tmdb_id}", data)
    except HTTPException as exc:
        return _resource_page(title.strip() or f"TMDB {tmdb_id}", {}, exc.status_code)


@app.post("/web/search", response_class=HTMLResponse)
async def web_search(keyword: str = Form(...), media_type: str = Form("movie")) -> HTMLResponse:
    try:
        data = await search_resources(SearchRequest(keyword=keyword, media_type=media_type), WEB_INSTALLATION_ID)
        return _result_page("HDHive name search", data)
    except HTTPException as exc:
        return _result_page("Search failed", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)


@app.post("/web/subscribe", response_class=HTMLResponse)
def web_subscribe(title: str = Form(...), media_type: str = Form(...), tmdb_id: int = Form(...)) -> HTMLResponse:
    try:
        result = create_subscription(SubscriptionRequest(title=title, media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        return _result_page("Subscription added", result)
    except HTTPException as exc:
        return _result_page("Subscription failed", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)


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
    with database() as conn:
        conn.execute(
            "INSERT INTO transfer_records (installation_id, slug, name, share_url, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (installation_id, slug, name, url, "resolved", int(time.time())),
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


class WebSettings(BaseModel):
    moviepilot_url: str = Field(default="", max_length=500)
    save_directory: str = Field(default="", max_length=500)
    offline_enabled: bool = True
    tmdb_api_key: str = Field(default="", max_length=1000)
    tmdb_language: str = Field(default="zh-CN", max_length=16)
    tmdb_region: str = Field(default="CN", max_length=8)


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

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
        conn.execute("""CREATE TABLE IF NOT EXISTS web_settings (
            installation_id TEXT PRIMARY KEY,
            moviepilot_url TEXT DEFAULT '',
            save_directory TEXT DEFAULT '',
            offline_enabled INTEGER DEFAULT 1,
            updated_at INTEGER NOT NULL
        )""")


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


@app.post("/web/query", response_class=HTMLResponse)
async def web_query(media_type: str = Form(...), tmdb_id: int = Form(...)) -> HTMLResponse:
    try:
        data = await query_resources(ResourceQuery(media_type=media_type, tmdb_id=tmdb_id), WEB_INSTALLATION_ID)
        return _result_page("影巢资源查询结果", data)
    except HTTPException as exc:
        return _result_page("查询失败", {"status": exc.status_code, "detail": exc.detail}, exc.status_code)


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


@app.post("/v1/subscriptions")
def create_subscription(
    request: SubscriptionRequest,
    installation_id: str = Depends(require_installation),
) -> dict[str, Any]:
    with database() as conn:
        cur = conn.execute(
            "INSERT INTO web_subscriptions (installation_id, title, media_type, tmdb_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (installation_id, request.title.strip(), request.media_type, request.tmdb_id, int(time.time())),
        )
        return {"id": cur.lastrowid, "status": "active", **request.model_dump()}


@app.get("/v1/subscriptions")
def list_subscriptions(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        rows = conn.execute(
            "SELECT id, title, media_type, tmdb_id, status, created_at FROM web_subscriptions WHERE installation_id = ? ORDER BY id DESC",
            (installation_id,),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


class WebSettings(BaseModel):
    moviepilot_url: str = Field(default="", max_length=500)
    save_directory: str = Field(default="", max_length=500)
    offline_enabled: bool = True


@app.get("/v1/settings")
def get_settings(installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        row = conn.execute("SELECT moviepilot_url, save_directory, offline_enabled, updated_at FROM web_settings WHERE installation_id = ?", (installation_id,)).fetchone()
    if not row:
        return {"moviepilot_url": "", "save_directory": "", "offline_enabled": True}
    return dict(row)


@app.put("/v1/settings")
def put_settings(request: WebSettings, installation_id: str = Depends(require_installation)) -> dict[str, Any]:
    with database() as conn:
        conn.execute("""INSERT INTO web_settings (installation_id, moviepilot_url, save_directory, offline_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(installation_id) DO UPDATE SET moviepilot_url=excluded.moviepilot_url,
            save_directory=excluded.save_directory, offline_enabled=excluded.offline_enabled, updated_at=excluded.updated_at""",
            (installation_id, request.moviepilot_url.strip().rstrip("/"), request.save_directory.strip(), int(request.offline_enabled), int(time.time())))
    return {"ok": True, **request.model_dump()}


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

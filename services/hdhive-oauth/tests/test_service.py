import asyncio
import importlib
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

TEMP_DIR = tempfile.TemporaryDirectory()
os.environ.update(
    {
        "HDHIVE_CLIENT_ID": "app_test",
        "HDHIVE_APP_SECRET": "app-secret",
        "HDHIVE_REDIRECT_URI": "https://oauth.example.com/oauth/callback",
        "INSTALLATION_KEY": "installation-secret",
        "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DATABASE_PATH": str(pathlib.Path(TEMP_DIR.name) / "test.db"),
        "DISABLE_BACKGROUND_WORKERS": "1",
    }
)
SERVICE_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(SERVICE_ROOT))
main = importlib.import_module("app.main")
from app.notifications import ChannelMessage


class OAuthServiceTests(unittest.TestCase):
    def setUp(self):
        main.init_database()
        self.client = TestClient(main.app)
        self.installation_id = "installation_12345"
        self.headers = {
            "X-Installation-ID": self.installation_id,
            "X-Installation-Key": "installation-secret",
        }
        with main.database() as conn:
            conn.execute("DELETE FROM subscription_runs WHERE installation_id=?", (self.installation_id,))
            conn.execute("DELETE FROM transfer_records WHERE installation_id=?", (self.installation_id,))
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=?", (self.installation_id,))
            conn.execute("DELETE FROM search_history")
            conn.execute("DELETE FROM resource_search_cache")

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)

    def test_movieark_detail_and_search_pages(self):
        explore = self.client.get("/explore").text
        detail = self.client.get("/resources").text
        search = self.client.get("/search").text
        self.assertIn("data-action=\"search\"", explore)
        self.assertIn("data-action=\"subscribe\"", explore)
        self.assertIn("cover-actions", explore)
        self.assertIn("返回汇影", detail)
        self.assertIn("TMDB / Emby 季与集", detail)
        self.assertIn("Emby 未配置", detail)
        self.assertIn("data-collapsed", detail)
        self.assertIn("resolved_tmdb_id", detail)
        self.assertIn("影视与资源搜索", search)
        self.assertIn("data-action=\"search\"", search)
        self.assertIn("data-action=\"subscribe\"", search)

    def test_resource_search_accepts_title_without_tmdb_id(self):
        class Registry:
            async def search(self, media_type, tmdb_id, title):
                self.received = (media_type, tmdb_id, title)
                return [], []

            def infos(self):
                return []

        provider = Registry()
        with patch.object(main, "resource_registry", return_value=provider):
            response = self.client.get("/api/web/resources/search", params={"media_type": "movie", "tmdb_id": 0, "title": "测试影片", "douban_id": "123"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.received, ("movie", 0, "测试影片"))
        self.assertEqual(response.json()["match"]["douban_id"], "123")

    def test_resource_search_resolves_tmdb_id_when_missing(self):
        class Registry:
            def __init__(self):
                self.received = None
            async def search(self, media_type, tmdb_id, title):
                self.received = (media_type, tmdb_id, title)
                return [], []
            def infos(self):
                return []

        provider = Registry()
        with patch.object(main, "resource_registry", return_value=provider), \
             patch.object(main, "resolve_tmdb_id", new=AsyncMock(return_value={"tmdb_id": 532753, "title": "我不是药神", "year": "2018"})):
            response = self.client.get("/api/web/resources/search", params={"media_type": "movie", "tmdb_id": 0, "title": "我不是药神", "douban_id": "26752088", "year": "2018"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.received, ("movie", 532753, "我不是药神"))
        self.assertEqual(response.json()["resolved_tmdb_id"], 532753)

    def test_media_detail_resolves_title_without_tmdb_id(self):
        tmdb_payload = {
            "id": 532753, "title": "我不是药神", "original_title": "我不是药神",
            "overview": "", "release_date": "2018-07-06", "vote_average": 8.2,
            "genres": [], "production_countries": [], "status": "Released",
            "credits": {"cast": [], "crew": []}, "seasons": [],
        }
        provider = Mock()
        provider.configured = True
        provider.normalize = main.TMDBProvider.normalize
        provider.request = AsyncMock(return_value=(tmdb_payload, False))
        with patch.object(main, "resolve_tmdb_id", new=AsyncMock(return_value={"tmdb_id": 532753})), \
             patch.object(main, "explore_tmdb_provider", return_value=provider):
            response = self.client.get("/api/web/media/detail", params={"media_type": "movie", "tmdb_id": 0, "title": "深海特搜", "year": "2023"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resolved_tmdb_id"], 532753)
        self.assertEqual(response.json()["data"]["tmdb_id"], 532753)


    def test_douban_subscription_without_tmdb_id_is_supported_and_deduplicated(self):
        payload = {"title": "豆瓣订阅测试", "media_type": "movie", "tmdb_id": 0, "douban_id": "1292052", "year": 1994}
        with main.database() as conn:
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND douban_id=?", (main.WEB_INSTALLATION_ID, payload["douban_id"]))
        first = self.client.post("/api/web/subscriptions", json=payload)
        second = self.client.post("/api/web/subscriptions", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json().get("duplicate", False))
        self.assertTrue(second.json()["duplicate"])
        status = self.client.get("/api/web/subscription-status", params={"media_type": "movie", "tmdb_id": 0, "douban_id": payload["douban_id"]})
        self.assertTrue(status.json()["subscribed"])

    def test_optional_admin_login_protects_dashboard(self):
        old_user, old_password = main.WEB_ADMIN_USER, main.WEB_ADMIN_PASSWORD
        main.WEB_ADMIN_USER, main.WEB_ADMIN_PASSWORD = "owner", "strong-password"
        try:
            with TestClient(main.app, base_url="https://testserver") as client:
                self.assertEqual(client.get("/rankings", follow_redirects=False).status_code, 303)
                self.assertEqual(client.get("/api/web/authorizations").status_code, 401)
                response = client.post("/admin/login", data={"username": "owner", "password": "strong-password", "next": "/rankings"}, follow_redirects=False)
                self.assertEqual(response.status_code, 303)
                self.assertIn("moon_admin", response.cookies)
                self.assertEqual(client.get("/rankings").status_code, 200)
                self.assertEqual(client.get("/health").status_code, 200)
        finally:
            main.WEB_ADMIN_USER, main.WEB_ADMIN_PASSWORD = old_user, old_password

    def test_rejects_invalid_installation_key(self):
        response = self.client.get(
            "/v1/status",
            headers={
                "X-Installation-ID": self.installation_id,
                "X-Installation-Key": "wrong",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_settings_and_subscriptions(self):
        response = self.client.put(
            "/v1/settings",
            headers=self.headers,
            json={"moviepilot_url": "http://moviepilot.local", "save_directory": "/media", "offline_enabled": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/v1/settings", headers=self.headers).json()["save_directory"], "/media")
        self.assertNotIn("tmdb_api_key", self.client.get("/v1/settings", headers=self.headers).json())
        response = self.client.post(
            "/v1/subscriptions",
            headers=self.headers,
            json={"title": "Example", "media_type": "movie", "tmdb_id": 123},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.client.get("/v1/subscriptions", headers=self.headers).json()["items"]), 1)

    def test_subscription_pagination_search_and_filters(self):
        now = int(main.time.time())
        with main.database() as conn:
            for index in range(15):
                conn.execute(
                    """INSERT INTO web_subscriptions
                       (installation_id,title,media_type,tmdb_id,year,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (self.installation_id, f"测试剧集 {index}", "tv" if index % 2 else "movie", 1000 + index, 2025 + index % 2, "active", now + index, now + index),
                )
        page = self.client.get("/v1/subscriptions", headers=self.headers, params={"page": 2, "page_size": 5}).json()
        self.assertEqual(page["total"], 15)
        self.assertEqual(len(page["items"]), 5)
        self.assertEqual(page["total_pages"], 3)
        filtered = self.client.get("/v1/subscriptions", headers=self.headers, params={"search": "剧集 1", "media_type": "tv", "year": 2026}).json()
        self.assertTrue(filtered["items"])
        self.assertTrue(all(x["media_type"] == "tv" and x["year"] == 2026 for x in filtered["items"]))

    def test_subscription_history_delete_and_detail_aggregation(self):
        created = self.client.post("/v1/subscriptions", headers=self.headers, json={"title": "聚合测试", "media_type": "movie", "tmdb_id": 7788, "year": 2026}).json()
        sub_id = created["id"]
        with main.database() as conn:
            conn.execute("INSERT INTO subscription_runs (installation_id,subscription_id,status,resource_count,created_at) VALUES (?,?,?,?,?)", (self.installation_id, sub_id, "resource_found", 3, int(main.time.time())))
            conn.execute("INSERT INTO transfer_records (installation_id,subscription_id,slug,name,status,created_at) VALUES (?,?,?,?,?,?)", (self.installation_id, sub_id, "slug-one", "聚合测试", "resolved", int(main.time.time())))
        detail = self.client.get(f"/v1/subscriptions/{sub_id}", headers=self.headers).json()
        self.assertEqual(len(detail["runs"]), 1)
        self.assertEqual(len(detail["transfers"]), 1)
        tasks = main.task_page(self.installation_id)
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(tasks["items"][0]["title"], "聚合测试")
        unlocks = main.unlock_page(self.installation_id, search="聚合", status="resolved")
        self.assertEqual(unlocks["total"], 1)
        self.assertEqual(unlocks["items"][0]["source_type"], "115网盘")
        self.assertEqual(self.client.get("/v1/subscriptions", headers=self.headers).json()["items"][0]["saved_count"], 1)
        deleted = self.client.delete(f"/v1/subscriptions/{sub_id}", headers=self.headers).json()
        self.assertFalse(deleted["deleted_files"])
        self.assertEqual(self.client.get("/v1/subscriptions", headers=self.headers, params={"tab": "current"}).json()["total"], 0)
        self.assertEqual(self.client.get("/v1/subscriptions", headers=self.headers, params={"tab": "history"}).json()["total"], 1)

    def test_tasks_page_uses_shared_console_layout(self):
        page = self.client.get("/tasks")
        self.assertEqual(page.status_code, 200)
        self.assertIn("影舟 MovieArk", page.text)
        self.assertIn("订阅任务已启用", page.text)
        self.assertNotIn("Subscription tasks", page.text)

    def test_unlocks_page_uses_dense_console_layout(self):
        page = self.client.get("/unlocks")
        self.assertEqual(page.status_code, 200)
        self.assertIn("批量删除", page.text)
        self.assertIn("全部转存状态", page.text)

    def test_subscription_manual_run_and_error(self):
        created = self.client.post("/v1/subscriptions", headers=self.headers, json={"title": "执行测试", "media_type": "movie", "tmdb_id": 8899}).json()
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": [{"slug": "hit"}]})):
            result = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
        # 执行成功后订阅应回到监控态(active)，而非 terminal(completed)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["run_status"], "resource_found")
        with patch.object(main, "query_resources", new=AsyncMock(side_effect=main.HTTPException(502, "上游不可用"))):
            failed = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["run_status"], "failed")
        # 执行后订阅记录必须仍然存在，并停留在“当前订阅”列表（不会因执行而消失）
        listing = self.client.get("/v1/subscriptions", params={"tab": "current"}, headers=self.headers).json()
        self.assertIn(created["id"], [item["id"] for item in listing["items"]])
        with main.database() as conn:
            self.assertEqual(conn.execute("SELECT status FROM web_subscriptions WHERE id=?", (created["id"],)).fetchone()["status"], "failed")

    def test_current_subscription_list_includes_all_non_terminal_statuses(self):
        now = int(main.time.time())
        current_statuses = ("pending", "active", "running", "paused", "waiting_output", "resource_found", "completed", "failed")
        with main.database() as conn:
            for index, status in enumerate((*current_statuses, "cancelled", "expired"), 1):
                conn.execute(
                    "INSERT INTO web_subscriptions (installation_id,title,media_type,tmdb_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (self.installation_id, f"状态测试{index}", "movie", 910000 + index, status, now, now),
                )
        listing = self.client.get("/v1/subscriptions", headers=self.headers, params={"tab": "current", "page_size": 100}).json()
        returned = {item["status"] for item in listing["items"] if item["title"].startswith("状态测试")}
        self.assertEqual(returned, set(current_statuses))
        history = self.client.get("/v1/subscriptions", headers=self.headers, params={"tab": "history", "page_size": 100}).json()
        returned_history = {item["status"] for item in history["items"] if item["title"].startswith("状态测试")}
        self.assertEqual(returned_history, {"cancelled", "expired"})

    def test_running_subscription_is_not_started_twice(self):
        created = self.client.post("/v1/subscriptions", headers=self.headers, json={"title": "重复执行保护", "media_type": "tv", "tmdb_id": 919191}).json()
        with main.database() as conn:
            conn.execute("UPDATE web_subscriptions SET status='running' WHERE id=?", (created["id"],))
            before = conn.execute("SELECT COUNT(*) FROM subscription_runs WHERE subscription_id=?", (created["id"],)).fetchone()[0]
        result = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["run_status"], "skipped")
        with main.database() as conn:
            after = conn.execute("SELECT COUNT(*) FROM subscription_runs WHERE subscription_id=?", (created["id"],)).fetchone()[0]
            self.assertEqual(conn.execute("SELECT status FROM web_subscriptions WHERE id=?", (created["id"],)).fetchone()["status"], "running")
        self.assertEqual(after, before)

    def test_movie_and_tv_subscriptions_survive_successful_run_and_reload(self):
        for media_type, tmdb_id in (("movie", 929201), ("tv", 929202)):
            created = self.client.post("/v1/subscriptions", headers=self.headers, json={"title": f"{media_type}持久化测试", "media_type": media_type, "tmdb_id": tmdb_id}).json()
            with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": []})):
                result = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
            self.assertEqual(result["status"], "active")
            detail = self.client.get(f"/v1/subscriptions/{created['id']}", headers=self.headers).json()
            self.assertEqual(detail["subscription"]["status"], "active")
            self.assertEqual(len(detail["runs"]), 1)
            listing = self.client.get("/v1/subscriptions", headers=self.headers, params={"tab": "current", "page_size": 100}).json()
            self.assertIn(created["id"], [item["id"] for item in listing["items"]])

    def test_explore_result_can_create_subscription_with_poster(self):
        with main.database() as conn:
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND tmdb_id=9911", (main.WEB_INSTALLATION_ID,))
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": []})), patch.object(main, "search_resources", new=AsyncMock(return_value={"items": []})):
            result = self.client.post("/web/query", data={"title": "汇影订阅测试", "media_type": "movie", "tmdb_id": 9911, "year": "2026", "poster": "https://image.example/poster.jpg"})
        self.assertIn("订阅这部影视", result.text)
        created = self.client.post("/web/subscribe", data={"title": "汇影订阅测试", "media_type": "movie", "tmdb_id": 9911, "year": "2026", "poster": "https://image.example/poster.jpg"}, follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        with main.database() as conn:
            item = conn.execute("SELECT year,poster FROM web_subscriptions WHERE installation_id=? AND tmdb_id=9911", (main.WEB_INSTALLATION_ID,)).fetchone()
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND tmdb_id=9911", (main.WEB_INSTALLATION_ID,))
        self.assertEqual(item["year"], 2026)
        self.assertEqual(item["poster"], "https://image.example/poster.jpg")

    def test_oauth_callback_stores_only_encrypted_tokens(self):
        expires = int(main.time.time()) + 300
        signature = main.sign_start(self.installation_id, expires)
        start = self.client.get(
            "/oauth/start",
            params={
                "installation_id": self.installation_id,
                "expires": expires,
                "signature": signature,
            },
            follow_redirects=False,
        )
        self.assertEqual(start.status_code, 307)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

        with patch.object(
            main,
            "hdhive_request",
            new=AsyncMock(
                side_effect=[
                    {
                        "access_token": "access-plain",
                        "refresh_token": "refresh-plain",
                        "expires_in": 3600,
                    },
                    {"id": 32711, "nickname": "tester", "points": 100},
                ]
            ),
        ):
            callback = self.client.get(
                "/oauth/callback", params={"code": "one-time-code", "state": state}
            )
        self.assertEqual(callback.status_code, 200)
        status = self.client.get("/v1/status", headers=self.headers).json()
        self.assertTrue(status["authorized"])
        self.assertEqual(status["user"]["id"], 32711)
        with main.database() as conn:
            row = conn.execute("SELECT * FROM installations").fetchone()
        self.assertNotIn("access-plain", row["access_token"])
        self.assertNotIn("refresh-plain", row["refresh_token"])

    def test_business_request_refreshes_expired_token_once(self):
        with main.database() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO installations
                   (installation_id, access_token, refresh_token, expires_at,
                    user_json, updated_at) VALUES (?, ?, ?, 0, '{}', ?)""",
                (
                    self.installation_id,
                    main.encrypt("expired-access"),
                    main.encrypt("valid-refresh"),
                    int(main.time.time()),
                ),
            )
        with patch.object(
            main,
            "hdhive_request",
            new=AsyncMock(
                side_effect=[
                    main.HDHiveAPIError(401, "OPENAPI_REFRESH_REQUIRED", "expired"),
                    {
                        "access_token": "fresh-access",
                        "refresh_token": "fresh-refresh",
                        "expires_in": 3600,
                    },
                    {"id": 32711, "nickname": "tester"},
                    {"items": [{"slug": "resource-one"}]},
                ]
            ),
        ):
            response = self.client.post(
                "/v1/resources/query",
                headers=self.headers,
                json={"media_type": "movie", "tmdb_id": 123},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["slug"], "resource-one")

    def test_new_business_pages_and_settings(self):
        for path in ("/rankings", "/settings", "/web/settings", "/authorizations", "/telegram", "/unlocks"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("text/html", response.headers["content-type"])
        payload = {"root_directory": "/影视", "save_directory": "123456", "scrape_directory": "/已整理", "save_wait_seconds": 20, "retry_count": 2, "duplicate_policy": "skip", "offline_enabled": True, "ed2k_directory": "654321", "ed2k_poll_interval": 60, "ed2k_retry_count": 3, "ed2k_auto_archive": True}
        response = self.client.put("/api/web/settings/business", json=payload)
        self.assertEqual(response.status_code, 200)
        loaded = self.client.get("/api/web/settings/business").json()
        self.assertEqual(loaded["save_directory"], "123456")

    def test_authorization_and_telegram_secrets_are_not_returned(self):
        response = self.client.put("/api/web/authorizations/tmdb", json={"api_key": "secret-tmdb", "language": "zh-CN"})
        self.assertEqual(response.status_code, 200)
        providers = self.client.get("/api/web/authorizations").json()["providers"]
        self.assertTrue(providers["tmdb"]["configured"])
        self.assertNotIn("secret-tmdb", str(providers))
        response = self.client.put("/api/web/telegram/settings", json={"bot_token": "secret-bot", "chat_id": "123", "enabled": True, "events": {"transfer_success": True}, "template": "{title}", "channel_enabled": False, "channel_name": "oneonefivewpfx", "channel_interval": 600})
        self.assertEqual(response.status_code, 200)
        loaded = self.client.get("/api/web/telegram/settings").json()
        self.assertTrue(loaded["bot_token_configured"])
        self.assertNotIn("secret-bot", str(loaded))

    def test_p115_qr_device_options_and_validation(self):
        apps = self.client.get("/api/web/authorizations/p115/qr/apps")
        self.assertEqual(apps.status_code, 200)
        app_ids = {item["id"] for item in apps.json()["items"]}
        self.assertTrue({"web", "android", "ios", "os_mac", "harmony"}.issubset(app_ids))
        page = self.client.get("/authorizations")
        self.assertIn('id="p115App"', page.text)
        response = self.client.post("/api/web/authorizations/p115/qr/start", json={"app": "advertising-app"})
        self.assertEqual(response.status_code, 400)

    def test_p115_qr_start_returns_embedded_scannable_image(self):
        response = Mock()
        response.json.return_value = {"data": {"uid": "qr-session", "time": 123, "sign": "signed", "qrcode": "https://115.com/scan/dg-test"}}
        http_client = AsyncMock()
        http_client.__aenter__.return_value.get = AsyncMock(return_value=response)
        with patch.object(main.httpx, "AsyncClient", return_value=http_client):
            result = self.client.post("/api/web/authorizations/p115/qr/start", json={"app": "os_mac"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["app"], "os_mac")
        self.assertTrue(result.json()["qr_image"].startswith("data:image/svg+xml;base64,"))

    def test_trusted_notification_event_respects_disabled_event(self):
        self.client.put(
            "/api/web/telegram/settings",
            json={"enabled": False, "events": {"manual_review": True}, "channel_enabled": False, "channel_interval": 600},
        )
        response = self.client.post(
            "/v1/notifications/event",
            headers=self.headers,
            json={"event": "manual_review", "title": "需要人工确认", "status": "等待"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["skipped"])

    def test_telegram_commands_create_real_subscription(self):
        import asyncio
        help_text = asyncio.run(main._telegram_command("/help"))
        self.assertIn("/search", help_text)
        reply = asyncio.run(main._telegram_command("/subscribe movie 998877 Telegram测试影片"))
        self.assertIn("订阅成功", reply)
        with main.database() as conn:
            row = conn.execute("SELECT title FROM web_subscriptions WHERE installation_id=? AND tmdb_id=998877", (main.WEB_INSTALLATION_ID,)).fetchone()
        self.assertEqual(row["title"], "Telegram测试影片")

    def test_classify_share_link(self):
        self.assertEqual(main.classify_share_link("https://115cdn.com/s/abc?password=1"), "115")
        self.assertEqual(main.classify_share_link("https://115.com/s/abc"), "115")
        self.assertEqual(main.classify_share_link("https://pan.quark.cn/s/abc"), "unsupported")
        self.assertEqual(main.classify_share_link("magnet:?xt=urn:btih:abc"), "offline")
        self.assertEqual(main.classify_share_link("ed2k://|file|a.mkv|1|abc|/"), "offline")
        self.assertEqual(main.classify_share_link(""), "empty")

    def test_transfer_reports_unsupported_source_without_calling_115(self):
        with patch.object(main, "resolve_resource", new=AsyncMock(return_value={"name": "资源", "share_url": "https://pan.quark.cn/s/c2c3370f6d7e", "password": "", "files": [], "slug": "abc", "already_owned": False, "is_unlocked": True, "link_type": "unsupported", "pan_type": "quark", "uploader": "用户", "points": 0})), patch.object(main.P115Client, "transfer", new=AsyncMock()) as transfer_mock:
            response = self.client.post("/api/web/resources/transfer", json={"provider": "hdhive", "resource_id": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("当前来源暂不支持直接转存115", response.json()["detail"])
        transfer_mock.assert_not_called()

    def test_transfer_115_success_updates_record(self):
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("INSERT OR REPLACE INTO installations (installation_id,access_token,refresh_token,expires_at,user_json,updated_at) VALUES (?,?,?,?,?,?)", (main.WEB_INSTALLATION_ID, main.encrypt("access"), main.encrypt("refresh"), now + 3600, "{}", now))
            conn.execute("INSERT INTO web_settings (installation_id,p115_cookie,updated_at) VALUES (?,?,?) ON CONFLICT(installation_id) DO UPDATE SET p115_cookie=excluded.p115_cookie", (main.WEB_INSTALLATION_ID, main.encrypt("UID=1"), now))
            conn.execute("INSERT INTO transfer_records (installation_id,slug,name,share_url,status,processing_status,source_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (main.WEB_INSTALLATION_ID, "abc", "影片", "https://115cdn.com/s/abc123?password=x9", "resolved", "resolved", "115网盘", now, now))
        with patch.object(main, "resolve_resource", new=AsyncMock(return_value={"name": "影片", "share_url": "https://115cdn.com/s/abc123?password=x9", "password": "x9", "files": [], "slug": "abc", "already_owned": False, "is_unlocked": True, "link_type": "115", "pan_type": "115", "uploader": "用户", "points": 0})), patch.object(main.P115Client, "transfer", new=AsyncMock(return_value={"ok": True, "count": 1, "message": "转存成功：1 个文件"})), patch.object(main, "_telegram_settings", return_value={"enabled": False}):
            response = self.client.post("/api/web/resources/transfer", json={"provider": "hdhive", "resource_id": "abc"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["transfer_status"], "success")
        self.assertEqual(body["link_type"], "115")
        with main.database() as conn:
            row = conn.execute("SELECT status,processing_status,unlock_status,transfer_status FROM transfer_records WHERE installation_id=? AND slug=? ORDER BY id DESC LIMIT 1", (main.WEB_INSTALLATION_ID, "abc")).fetchone()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["processing_status"], "completed")
        self.assertEqual(row["unlock_status"], "unlocked")
        self.assertEqual(row["transfer_status"], "success")

    def test_web_subscription_reuses_real_transfer_pipeline(self):
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("INSERT OR REPLACE INTO installations (installation_id,access_token,refresh_token,expires_at,user_json,updated_at) VALUES (?,?,?,?,?,?)", (main.WEB_INSTALLATION_ID, main.encrypt("access"), main.encrypt("refresh"), now + 3600, "{}", now))
            conn.execute("INSERT INTO web_settings (installation_id,p115_cookie,updated_at) VALUES (?,?,?) ON CONFLICT(installation_id) DO UPDATE SET p115_cookie=excluded.p115_cookie", (main.WEB_INSTALLATION_ID, main.encrypt("UID=1"), now))
        created = main.create_subscription(main.SubscriptionRequest(title="自动转存", media_type="movie", tmdb_id=7654321), main.WEB_INSTALLATION_ID)
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": [{"slug": "resource-hit"}]})), patch.object(main, "web_resource_transfer", new=AsyncMock(return_value={"ok": True})), patch.object(main, "get_business_settings", return_value={"subscription_auto_transfer": True}):
            result = self.client.post(f"/api/web/subscriptions/{created['id']}/run").json()
        # 回归：成功转存后订阅必须回到监控态(active)，且不能从当前列表消失
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["run_status"], "success")
        self.assertEqual(result["transfer_count"], 1)
        listing = self.client.get("/api/web/subscriptions", params={"tab": "current"}).json()
        self.assertIn(created["id"], [item["id"] for item in listing["items"]])
        with main.database() as conn:
            self.assertEqual(conn.execute("SELECT status FROM web_subscriptions WHERE id=?", (created["id"],)).fetchone()["status"], "active")
            conn.execute("DELETE FROM subscription_runs WHERE subscription_id=?", (created["id"],))
            conn.execute("DELETE FROM web_subscriptions WHERE id=?", (created["id"],))

    def test_subscription_selects_best_and_tags_subscription_id(self):
        """回归：仅转存最佳可直转资源（整季包），跳过收费不可直转的；
        转存请求带上 subscription_id；电视剧追更写入已入库集号。"""
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("INSERT OR REPLACE INTO installations (installation_id,access_token,refresh_token,expires_at,user_json,updated_at) VALUES (?,?,?,?,?,?)", (main.WEB_INSTALLATION_ID, main.encrypt("access"), main.encrypt("refresh"), now + 3600, "{}", now))
            conn.execute("INSERT INTO web_settings (installation_id,p115_cookie,updated_at) VALUES (?,?,?) ON CONFLICT(installation_id) DO UPDATE SET p115_cookie=excluded.p115_cookie", (main.WEB_INSTALLATION_ID, main.encrypt("UID=1"), now))
        resources = [
            {"slug": "paid-unsupported", "remark": "凡人修仙传 S01E01-E05 全5集", "pan_type": "other", "unlock_points": 20, "is_official": False, "is_free_for_user": False},
            {"slug": "free-115-pack", "remark": "凡人修仙传 S01E01-E20 全20集", "pan_type": "115", "unlock_points": 0, "is_official": True, "is_free_for_user": True},
        ]
        created = main.create_subscription(main.SubscriptionRequest(title="凡人修仙传", media_type="tv", tmdb_id=106449, season="1"), main.WEB_INSTALLATION_ID)
        captured = []
        async def fake_transfer(req):
            captured.append(req)
            return {"ok": True}
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": resources})), patch.object(main, "web_resource_transfer", new=fake_transfer), patch.object(main, "get_business_settings", return_value={"subscription_auto_transfer": True}):
            result = self.client.post(f"/api/web/subscriptions/{created['id']}/run").json()
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["run_status"], "success")
        # 仅转存最佳可直转整季包，不转收费不可直转资源
        self.assertEqual(result["transfer_count"], 1)
        self.assertEqual([c.resource_id for c in captured], ["free-115-pack"])
        # 转存请求必须带上 subscription_id（保证“保存次数”能正确归并到该订阅）
        self.assertTrue(all(c.subscription_id == created["id"] for c in captured))
        with main.database() as conn:
            row = conn.execute("SELECT saved_episodes FROM web_subscriptions WHERE id=?", (created["id"],)).fetchone()
            eps = main.json.loads(row["saved_episodes"]).get("eps", [])
            self.assertTrue(set(range(1, 21)).issubset(set(eps)))
        with main.database() as conn:
            conn.execute("DELETE FROM subscription_runs WHERE subscription_id=?", (created["id"],))
            conn.execute("DELETE FROM web_subscriptions WHERE id=?", (created["id"],))


    def test_search_history_and_cache_persistence(self):
        class Registry:
            async def search(self, media_type, tmdb_id, title):
                return [], []
            def infos(self):
                return []

        provider = Registry()
        with patch.object(main, "resource_registry", return_value=provider),              patch.object(main, "resolve_tmdb_id", new=AsyncMock(return_value={"tmdb_id": 532753, "title": "我不是药神", "year": "2018"})):
            response = self.client.get("/api/web/resources/search", params={"media_type": "movie", "tmdb_id": 0, "title": "深海特搜", "year": "2023", "poster": "https://img.example/p.jpg"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["searched_at"])
        cached = self.client.get("/api/web/resources/search/cached", params={"media_type": "movie", "tmdb_id": 0, "title": "深海特搜", "year": "2023"}).json()
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["resolved_tmdb_id"], 532753)
        history = self.client.get("/api/web/search-history").json()
        self.assertEqual(history["items"][0]["title"], "深海特搜")
        self.assertEqual(history["items"][0]["result_count"], 0)
        hid = history["items"][0]["id"]
        self.assertEqual(self.client.delete(f"/api/web/search-history/{hid}").status_code, 200)
        self.assertEqual(self.client.get("/api/web/search-history").json()["items"], [])
        self.client.get("/api/web/resources/search", params={"media_type": "tv", "tmdb_id": 0, "title": "狂飙"})
        cleared = self.client.delete("/api/web/search-history").json()
        self.assertEqual(cleared["deleted"], 1)

    def test_unlock_transfer_status_migration(self):
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("DELETE FROM transfer_records WHERE installation_id=? AND slug LIKE 'mig-%'", (main.WEB_INSTALLATION_ID,))
            for status, share in (("completed", "https://115cdn.com/s/abc123"), ("resolved", "https://115cdn.com/s/def456"), ("failed", ""), ("failed", "https://pan.quark.cn/s/x1")):
                conn.execute("INSERT INTO transfer_records (installation_id,slug,name,share_url,status,processing_status,source_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                             (main.WEB_INSTALLATION_ID, "mig-" + status + share[:3], "迁移测试", share, status, status, "115网盘", now, now))
        main.init_database()
        with main.database() as conn:
            completed = conn.execute("SELECT unlock_status,transfer_status FROM transfer_records WHERE slug='mig-completedhtt'").fetchone()
            resolved = conn.execute("SELECT unlock_status,transfer_status FROM transfer_records WHERE slug='mig-resolvedhtt'").fetchone()
            failed_empty = conn.execute("SELECT unlock_status,transfer_status FROM transfer_records WHERE slug='mig-failed'").fetchone()
            failed_share = conn.execute("SELECT unlock_status,transfer_status FROM transfer_records WHERE slug='mig-failedhtt'").fetchone()
        self.assertEqual(dict(completed), {"unlock_status": "unlocked", "transfer_status": "success"})
        self.assertEqual(dict(resolved), {"unlock_status": "unlocked", "transfer_status": "pending"})
        self.assertEqual(dict(failed_empty), {"unlock_status": "failed", "transfer_status": "pending"})
        self.assertEqual(dict(failed_share), {"unlock_status": "unlocked", "transfer_status": "failed"})

    def test_log_center_endpoints_and_masking(self):
        with main.database() as conn:
            conn.execute("INSERT INTO app_logs (ts, level, module, message) VALUES (?,?,?,?)", (int(main.time.time()), "ERROR", "resources", "module=resources provider=115 operation=transfer error_type=P115Error token=SECRET123"))
        page = self.client.get("/logs").text
        self.assertIn("日志中心", page)
        logs = self.client.get("/api/web/logs", params={"level": "ERROR", "module": "resources"}).json()
        self.assertTrue(logs["total"] >= 1)
        self.assertIn("SECRET", logs["items"][0]["message"])
        modules = self.client.get("/api/web/logs/modules").json()
        self.assertIn("resources", modules["modules"])
        with main.database() as conn:
            conn.execute("DELETE FROM app_logs WHERE module='resources' AND message LIKE '%SECRET123%'")

    def test_115_folders_endpoint(self):
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("INSERT INTO web_settings (installation_id,p115_cookie,updated_at) VALUES (?,?,?) ON CONFLICT(installation_id) DO UPDATE SET p115_cookie=excluded.p115_cookie", (main.WEB_INSTALLATION_ID, main.encrypt("UID=1"), now))
        with patch.object(main.P115Client, "folders", new=AsyncMock(return_value={"cid": "0", "path": [], "folders": [{"cid": "1001", "pid": "0", "name": "影视"}], "count": 1})):
            response = self.client.get("/api/web/115/folders")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["folders"][0]["name"], "影视")
        with patch.object(main.P115Client, "folders", new=AsyncMock(side_effect=main.P115Error("115 Cookie 已失效或无法获取账号 UID"))):
            failed = self.client.get("/api/web/115/folders")
        self.assertEqual(failed.status_code, 401)
        self.assertIn("重新扫码", failed.json()["detail"])

class FakeEmbyClient:
    def __init__(self, routes):
        self.routes = routes
        self.request_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, *args, **kwargs):
        self.request_count += 1
        for suffix, payload, status in self.routes:
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeEmbyResponse(payload, status)
        return FakeEmbyResponse({"Error": "not found"}, 404)


class FakeEmbyResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class EmbyAndTelegramChannelTests(unittest.TestCase):
    def setUp(self):
        main.init_database()
        self.client = TestClient(main.app)
        with main.database() as conn:
            conn.execute("DELETE FROM telegram_channels WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("DELETE FROM telegram_channel_messages WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("DELETE FROM provider_state WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("DELETE FROM web_settings WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("INSERT INTO web_settings (installation_id,channel_name,updated_at) VALUES (?,?,?)", (main.WEB_INSTALLATION_ID, "", int(time.time())))

    def _save_emby(self, url="http://emby.local:8096", api_key="emby-secret", user_id="user-1"):
        response = self.client.put("/api/web/authorizations/emby", json={"url": url, "api_key": api_key, "user_id": user_id})
        self.assertEqual(response.status_code, 200)
        return response

    def test_emby_save_roundtrip_persists_and_refreshes_state(self):
        self._save_emby()
        providers = self.client.get("/api/web/authorizations").json()["providers"]
        self.assertTrue(providers["emby"]["configured"])
        self.assertEqual(providers["emby"]["url"], "http://emby.local:8096")
        self.assertEqual(providers["emby"]["user_id"], "user-1")
        # 重新读取（模拟刷新页面 / 重启后端）配置仍在
        settings, _ = main._authorization_rows()
        self.assertEqual(settings["emby_url"], "http://emby.local:8096")
        self.assertEqual(settings["emby_user_id"], "user-1")
        self.assertTrue(bool(settings["emby_api_key"]))
        # 保存后 available=false，直到测试连接成功
        self.assertFalse(providers["emby"]["available"])

    def test_emby_test_connection_success_uses_form_values(self):
        fake = FakeEmbyClient([
            ("/System/Info", {"ServerName": "MyEmby", "Version": "4.8.0.0"}, 200),
            ("/Users/current-user", {"Name": "小明"}, 200),
        ])
        with patch.object(main.httpx, "AsyncClient", return_value=fake):
            response = self.client.post("/api/web/authorizations/emby/test", json={"url": "http://emby.local:8096", "api_key": "typed-key", "user_id": "current-user"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("连接成功", payload["message"])
        self.assertIn("MyEmby", payload["message"])
        self.assertIn("4.8.0.0", payload["message"])
        self.assertIn("小明", payload["message"])
        with main.database() as conn:
            state = conn.execute("SELECT state_json FROM provider_state WHERE installation_id=? AND provider='emby'", (main.WEB_INSTALLATION_ID,)).fetchone()
        self.assertIn('"available": true', state["state_json"])

    def test_emby_test_connection_invalid_key_and_timeout(self):
        fake = FakeEmbyClient([("/System/Info", {"Error": "Unauthorized"}, 401)])
        with patch.object(main.httpx, "AsyncClient", return_value=fake):
            response = self.client.post("/api/web/authorizations/emby/test", json={"url": "http://emby.local:8096", "api_key": "bad", "user_id": "user-1"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("API Key 无效", response.json()["detail"])
        fake = FakeEmbyClient([("/System/Info", main.httpx.ConnectTimeout("boom"), 0)])
        with patch.object(main.httpx, "AsyncClient", return_value=fake):
            response = self.client.post("/api/web/authorizations/emby/test", json={"url": "http://emby.local:8096", "api_key": "k", "user_id": "u"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("连接超时", response.json()["detail"])

    def test_emby_test_connection_requires_user_id(self):
        response = self.client.post("/api/web/authorizations/emby/test", json={"url": "http://emby.local:8096", "api_key": "k", "user_id": ""})
        self.assertEqual(response.status_code, 409)
        self.assertIn("User ID", response.json()["detail"])

    def test_media_detail_and_season_consume_emby_config(self):
        self._save_emby()
        fake = FakeEmbyClient([
            ("/Users/user-1/Items", {"Items": [{"Id": "series-1"}]}, 200),
            ("/Shows/series-1/Episodes", {"Items": [{"IndexNumber": 1, "ParentIndexNumber": 1}]}, 200),
        ])
        tmdb_payload = {
            "id": 1396, "name": "绝命毒师", "original_name": "Breaking Bad", "overview": "", "first_air_date": "2008-01-20",
            "vote_average": 8.9, "genres": [], "production_countries": [], "status": "Ended", "credits": {"cast": [], "crew": []},
            "seasons": [{"season_number": 1, "name": "第 1 季", "episode_count": 7, "air_date": "2008-01-20", "poster_path": None}],
            "episodes": [{"season_number": 1, "episode_number": 1, "name": "试播集", "overview": "", "air_date": "", "still_path": None}],
        }
        provider = Mock()
        provider.configured = True
        provider.normalize = main.TMDBProvider.normalize
        provider.request = AsyncMock(return_value=(tmdb_payload, False))
        with patch.object(main, "explore_tmdb_provider", return_value=provider), patch.object(main.httpx, "AsyncClient", return_value=fake):
            detail = self.client.get("/api/web/media/detail", params={"media_type": "tv", "tmdb_id": 1396}).json()
            season = self.client.get("/api/web/media/season", params={"tmdb_id": 1396, "season": 1}).json()
        self.assertTrue(detail["emby_configured"])
        self.assertFalse(detail["emby_error"])
        self.assertEqual(detail["emby_status"], "available")
        self.assertTrue(season["emby_configured"])
        self.assertEqual(season["stats"]["available"], 1)
        self.assertEqual(season["items"][0]["emby_status"], "available")

    def test_telegram_single_channel_migrates_to_first_record(self):
        with main.database() as conn:
            conn.execute("DELETE FROM telegram_channels WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("UPDATE web_settings SET channel_enabled=1,channel_name='oneonefivewpfx',channel_interval=900 WHERE installation_id=?", (main.WEB_INSTALLATION_ID,))
            conn.execute("INSERT OR REPLACE INTO channel_state (installation_id,last_message_id,last_check,next_check,last_status,last_error,processed) VALUES (?,?,?,?,?,?,?)",
                         (main.WEB_INSTALLATION_ID, 100, 200, 300, "正常", "", 5))
        main.init_database()
        with main.database() as conn:
            rows = conn.execute("SELECT * FROM telegram_channels WHERE installation_id=?", (main.WEB_INSTALLATION_ID,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "oneonefivewpfx")
        self.assertEqual(rows[0]["enabled"], 1)
        self.assertEqual(rows[0]["check_interval"], 900)
        self.assertEqual(rows[0]["last_message_id"], 100)
        self.assertEqual(rows[0]["processed_count"], 5)

    def test_telegram_channel_crud(self):
        created_a = self.client.post("/api/web/telegram/channels", json={"name": "频道A", "username": "https://t.me/channel_a", "enabled": True, "check_interval": 600})
        self.assertEqual(created_a.status_code, 200)
        channel_a = created_a.json()["channel"]
        created_b = self.client.post("/api/web/telegram/channels", json={"name": "频道B", "username": "@channel_b", "enabled": True, "check_interval": 900})
        self.assertEqual(created_b.status_code, 200)
        channel_b = created_b.json()["channel"]
        items = self.client.get("/api/web/telegram/channels").json()["items"]
        self.assertEqual([item["username"] for item in items], ["channel_a", "channel_b"])
        updated = self.client.put(f"/api/web/telegram/channels/{channel_b['id']}", json={"name": "频道B2", "username": "@channel_b2", "enabled": False, "check_interval": 1200})
        self.assertEqual(updated.json()["channel"]["username"], "channel_b2")
        self.assertFalse(updated.json()["channel"]["enabled"])
        deleted = self.client.delete(f"/api/web/telegram/channels/{channel_a['id']}")
        self.assertEqual(deleted.status_code, 200)
        items = self.client.get("/api/web/telegram/channels").json()["items"]
        self.assertEqual([item["username"] for item in items], ["channel_b2"])

    def test_telegram_channel_check_dedup_and_first_cursor(self):
        created = self.client.post("/api/web/telegram/channels", json={"name": "频道", "username": "@demo_channel", "enabled": True, "check_interval": 600}).json()["channel"]
        channel_id = created["id"]
        messages = [ChannelMessage(10, "t", ["https://hdhive.com/resource/abc-1"]), ChannelMessage(11, "t", ["https://hdhive.com/resource/abc-2"])]
        with patch.object(main.CHANNEL_MONITOR, "fetch", new=AsyncMock(return_value=messages)), patch.object(main, "web_resource_transfer", new=AsyncMock(return_value={"ok": True})) as transfer:
            first = asyncio.run(main.run_channel_check(channel_id))
            self.assertEqual(first["processed"], 0)
            self.assertIn("首次检查", first["message"])
            with main.database() as conn:
                row = conn.execute("SELECT last_message_id FROM telegram_channels WHERE id=?", (channel_id,)).fetchone()
            self.assertEqual(row["last_message_id"], 11)
            transfer.assert_not_awaited()
            # 新消息进入后正常处理，再次检查同一批消息不会重复处理
            new_messages = [ChannelMessage(12, "t", ["https://hdhive.com/resource/abc-3"])]
            with patch.object(main.CHANNEL_MONITOR, "fetch", new=AsyncMock(return_value=[*messages, *new_messages])):
                second = asyncio.run(main.run_channel_check(channel_id))
            self.assertEqual(second["processed"], 1)
            transfer.assert_awaited_once()
            third = asyncio.run(main.run_channel_check(channel_id))
            self.assertEqual(third["processed"], 0)
            transfer.assert_awaited_once()
            with main.database() as conn:
                dedup_count = conn.execute("SELECT COUNT(*) FROM telegram_channel_messages WHERE installation_id=? AND channel_id=?", (main.WEB_INSTALLATION_ID, channel_id)).fetchone()[0]
            self.assertEqual(dedup_count, 1)

    def test_telegram_channel_error_isolation_between_channels(self):
        a = self.client.post("/api/web/telegram/channels", json={"name": "A", "username": "@good_channel", "enabled": True, "check_interval": 600}).json()["channel"]
        b = self.client.post("/api/web/telegram/channels", json={"name": "B", "username": "@bad_channel", "enabled": True, "check_interval": 600}).json()["channel"]
        with patch.object(main, "web_resource_transfer", new=AsyncMock(return_value={"ok": True})) as transfer:
            with patch.object(main.CHANNEL_MONITOR, "fetch", new=AsyncMock(return_value=[ChannelMessage(5, "t", ["https://hdhive.com/resource/ok-1"])])):
                asyncio.run(main.run_channel_check(a["id"]))

            async def fake_fetch(username, after_id=0):
                if username == "bad_channel":
                    raise RuntimeError("Telegram 访问失败")
                return [ChannelMessage(6, "t", ["https://hdhive.com/resource/ok-2"])]

            with patch.object(main.CHANNEL_MONITOR, "fetch", new=fake_fetch):
                result = asyncio.run(main.run_channel_check(None))
        self.assertEqual(result["checked"], 2)
        statuses = {r["ok"] for r in result["results"]}
        self.assertEqual(statuses, {True, False})
        self.assertEqual(transfer.await_count, 1)  # A 正常处理（首检仅建游标），B 失败不影响 A
        with main.database() as conn:
            bad = conn.execute("SELECT last_status,last_error FROM telegram_channels WHERE id=?", (b["id"],)).fetchone()
        self.assertEqual(bad["last_status"], "检查失败")
        self.assertIn("Telegram 访问失败", bad["last_error"])



    def test_split_season_title_helpers(self):
        self.assertEqual(main.split_season_title("龙之家族 第三季"), ("龙之家族", 3))
        self.assertEqual(main.split_season_title("海贼王 第3季"), ("海贼王", 3))
        self.assertEqual(main.split_season_title("凡人修仙传"), ("凡人修仙传", None))
        self.assertEqual(main.split_season_title(""), ("", None))
        self.assertEqual(main._parse_cn_number("二十四"), 24)
        self.assertEqual(main._parse_cn_number("十"), 10)

    def test_web_subscription_resolves_tmdb_and_season_before_store(self):
        payload = {"title": "龙之家族 第三季", "media_type": "tv", "tmdb_id": 0, "douban_id": "36666949", "year": 2026}
        with main.database() as conn:
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND douban_id=?", (main.WEB_INSTALLATION_ID, payload["douban_id"]))
        with patch.object(main, "resolve_tmdb_id", new=AsyncMock(return_value={"tmdb_id": 139699, "season": 3, "title": "龙之家族", "year": "2022", "poster": "http://x/p.jpg"})):
            created = self.client.post("/api/web/subscriptions", json=payload)
        self.assertEqual(created.status_code, 200)
        body = created.json()
        self.assertFalse(body.get("duplicate", False))
        self.assertEqual(body["tmdb_id"], 139699)
        self.assertEqual(body["season"], "3")
        with main.database() as conn:
            row = conn.execute("SELECT tmdb_id,season FROM web_subscriptions WHERE id=?", (body["id"],)).fetchone()
            conn.execute("DELETE FROM web_subscriptions WHERE id=?", (body["id"],))
        self.assertEqual(row["tmdb_id"], 139699)
        self.assertEqual(row["season"], "3")

    def test_create_subscription_heals_and_deduplicates_old_zero_tmdb_record(self):
        now = int(time.time())
        with main.database() as conn:
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND douban_id=?", (main.WEB_INSTALLATION_ID, "36554071"))
            conn.execute("INSERT INTO web_subscriptions (installation_id,title,media_type,tmdb_id,year,poster,season,douban_id,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (main.WEB_INSTALLATION_ID, "重器", "tv", 0, 2026, "", "", "36554071", "failed", now, now))
        payload = {"title": "重器", "media_type": "tv", "tmdb_id": 291856, "douban_id": "36554071", "year": 2026}
        with patch.object(main, "resolve_tmdb_id", new=AsyncMock(return_value={"tmdb_id": 291856})):
            first = self.client.post("/api/web/subscriptions", json=payload)
            second = self.client.post("/api/web/subscriptions", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["duplicate"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        with main.database() as conn:
            rows = conn.execute("SELECT tmdb_id,status FROM web_subscriptions WHERE installation_id=? AND douban_id=?", (main.WEB_INSTALLATION_ID, "36554071")).fetchall()
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND douban_id=?", (main.WEB_INSTALLATION_ID, "36554071"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tmdb_id"], 291856)
        self.assertEqual(rows[0]["status"], "active")

if __name__ == "__main__":
    unittest.main()

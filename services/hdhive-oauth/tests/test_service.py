import importlib
import os
import pathlib
import sys
import tempfile
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

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)

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
        self.assertIn("全部处理状态", page.text)

    def test_subscription_manual_run_and_error(self):
        created = self.client.post("/v1/subscriptions", headers=self.headers, json={"title": "执行测试", "media_type": "movie", "tmdb_id": 8899}).json()
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": [{"slug": "hit"}]})):
            result = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
        self.assertEqual(result["status"], "resource_found")
        with patch.object(main, "query_resources", new=AsyncMock(side_effect=main.HTTPException(502, "上游不可用"))):
            failed = self.client.post(f"/v1/subscriptions/{created['id']}/run", headers=self.headers).json()
        self.assertEqual(failed["status"], "failed")

    def test_explore_result_can_create_subscription_with_poster(self):
        with main.database() as conn:
            conn.execute("DELETE FROM web_subscriptions WHERE installation_id=? AND tmdb_id=9911", (main.WEB_INSTALLATION_ID,))
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": []})), patch.object(main, "search_resources", new=AsyncMock(return_value={"items": []})):
            result = self.client.post("/web/query", data={"title": "遨游订阅测试", "media_type": "movie", "tmdb_id": 9911, "year": "2026", "poster": "https://image.example/poster.jpg"})
        self.assertIn("订阅这部影视", result.text)
        created = self.client.post("/web/subscribe", data={"title": "遨游订阅测试", "media_type": "movie", "tmdb_id": 9911, "year": "2026", "poster": "https://image.example/poster.jpg"}, follow_redirects=False)
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

    def test_web_subscription_reuses_real_transfer_pipeline(self):
        now = int(main.time.time())
        with main.database() as conn:
            conn.execute("INSERT OR REPLACE INTO installations (installation_id,access_token,refresh_token,expires_at,user_json,updated_at) VALUES (?,?,?,?,?,?)", (main.WEB_INSTALLATION_ID, main.encrypt("access"), main.encrypt("refresh"), now + 3600, "{}", now))
            conn.execute("INSERT INTO web_settings (installation_id,p115_cookie,updated_at) VALUES (?,?,?) ON CONFLICT(installation_id) DO UPDATE SET p115_cookie=excluded.p115_cookie", (main.WEB_INSTALLATION_ID, main.encrypt("UID=1"), now))
        created = main.create_subscription(main.SubscriptionRequest(title="自动转存", media_type="movie", tmdb_id=7654321), main.WEB_INSTALLATION_ID)
        with patch.object(main, "query_resources", new=AsyncMock(return_value={"items": [{"slug": "resource-hit"}]})), patch.object(main, "web_resource_transfer", new=AsyncMock(return_value={"ok": True})), patch.object(main, "get_business_settings", return_value={"subscription_auto_transfer": True}):
            result = self.client.post(f"/api/web/subscriptions/{created['id']}/run").json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transfer_count"], 1)


if __name__ == "__main__":
    unittest.main()

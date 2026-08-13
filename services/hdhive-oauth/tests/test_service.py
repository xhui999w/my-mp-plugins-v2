import importlib
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
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

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)

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


if __name__ == "__main__":
    unittest.main()

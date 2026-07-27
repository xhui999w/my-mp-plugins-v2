import importlib.util
import pathlib
import sys
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "plugins.v2"
    / "tg115transfer"
    / "hdhive.py"
)
SPEC = importlib.util.spec_from_file_location("tg115transfer_hdhive", MODULE_PATH)
hdhive = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hdhive
SPEC.loader.exec_module(hdhive)


class FakeResponse:
    def __init__(self, status, payload, headers=None, reason=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.reason = reason
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class HDHiveLinkTests(unittest.TestCase):
    def test_extracts_only_trusted_resource_links(self):
        text = (
            "资源 https://hdhive.com/resource/abc-123?from=tg "
            "广告 https://hdhive.com.evil.test/resource/nope"
        )
        self.assertEqual(
            hdhive.extract_hdhive_urls(text),
            ["https://hdhive.com/resource/abc-123"],
        )

    def test_rejects_non_resource_path(self):
        self.assertEqual(
            hdhive.normalize_hdhive_url("https://hdhive.com/manager"),
            "",
        )


class HDHiveClientTests(unittest.TestCase):
    def test_resolve_resource_returns_standard_115_object(self):
        client = hdhive.HDHiveClient("secret", "access")
        client.session = FakeSession([
            FakeResponse(200, {
                "success": True,
                "data": {"media": {"title": "测试电影"}},
            }),
            FakeResponse(200, {
                "success": True,
                "data": {
                    "url": "https://115.com/s/abc123",
                    "access_code": "x1y2",
                    "full_url": "https://115.com/s/abc123?pwd=x1y2",
                    "already_owned": False,
                },
            }),
        ])

        result = client.resolve_resource(
            "https://hdhive.com/resource/resource-1"
        )

        self.assertEqual(result["name"], "测试电影")
        self.assertEqual(result["share_url"], "https://115.com/s/abc123")
        self.assertEqual(result["password"], "x1y2")
        self.assertEqual(result["files"], [])
        self.assertEqual(
            client.session.calls[1]["json"],
            {"slug": "resource-1"},
        )

    def test_refreshes_expired_access_token_once(self):
        refreshed = []
        client = hdhive.HDHiveClient(
            "secret",
            "expired",
            "refresh",
            on_token_refresh=refreshed.append,
        )
        client.session = FakeSession([
            FakeResponse(401, {
                "success": False,
                "code": "OPENAPI_REFRESH_REQUIRED",
                "message": "expired",
            }),
            FakeResponse(200, {
                "success": True,
                "data": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
            }),
            FakeResponse(200, {
                "success": True,
                "data": {"id": 1, "nickname": "tester"},
            }),
        ])

        result = client.get_me()

        self.assertEqual(result["nickname"], "tester")
        self.assertEqual(client.access_token, "new-access")
        self.assertEqual(refreshed[0]["refresh_token"], "new-refresh")
        self.assertEqual(len(client.session.calls), 3)

    def test_api_error_is_structured(self):
        client = hdhive.HDHiveClient("secret", "access")
        client.session = FakeSession([
            FakeResponse(
                429,
                {
                    "success": False,
                    "code": "RATE_LIMITED",
                    "message": "too fast",
                    "retry_after_seconds": 12,
                },
                headers={"Retry-After": "12"},
            )
        ])

        with self.assertRaises(hdhive.HDHiveError) as context:
            client.get_me()

        self.assertEqual(context.exception.code, "RATE_LIMITED")
        self.assertEqual(context.exception.retry_after, 12)


if __name__ == "__main__":
    unittest.main()

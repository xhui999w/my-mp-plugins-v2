import asyncio
import hashlib
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.p115 import P115Client, P115Error


class P115Tests(unittest.TestCase):
    def test_share_parts(self):
        code, password = P115Client.share_parts("https://115.com/s/abc123?password=z9x8")
        self.assertEqual((code, password), ("abc123", "z9x8"))

    def test_share_parts_supports_115cdn_domain(self):
        code, password = P115Client.share_parts("https://115cdn.com/s/sws6ah73zh3?password=nono")
        self.assertEqual((code, password), ("sws6ah73zh3", "nono"))
        code2, password2 = P115Client.share_parts("https://115cdn.com/s/sws6ah73zh3?code=nono")
        self.assertEqual((code2, password2), ("sws6ah73zh3", "nono"))

    def test_rejects_advertising_and_invalid_offline_links(self):
        with self.assertRaises(P115Error):
            P115Client.share_parts("https://ads.example.com/click")

    def test_security_token_sends_md5_and_returns_token(self):
        client = P115Client("UID=1;CID=2")
        response = MagicMock()
        response.json.return_value = {"state": 1, "data": {"token": "tok123"}}
        http = AsyncMock()
        http.post.return_value = response

        async def scenario():
            return await P115Client.security_token(client, "347887", http)

        token = asyncio.run(scenario())
        self.assertEqual(token, "tok123")
        expected_url = "https://passportapi.115.com/app/1.0/web/1.0/user/security_key_check"
        args, kwargs = http.post.call_args
        self.assertEqual(args[0], expected_url)
        self.assertEqual(kwargs["data"], {"passwd": hashlib.md5(b"347887").hexdigest()})

    def test_folders_parses_webapi_response_shape(self):
        client = P115Client("UID=1;CID=2")
        files_response = MagicMock()
        files_response.json.return_value = {
            "count": 2,
            "data": [
                {"cid": "11", "pid": "0", "n": "文件夹A"},
                {"cid": "12", "pid": "0", "n": "文件夹B"},
            ],
            "path": [{"cid": "0", "name": "根目录"}],
            "state": True,
        }
        user_response = MagicMock()
        user_response.json.return_value = {"state": True, "data": {"uid": "9"}}
        check_response = MagicMock()
        check_response.json.return_value = {"state": 1, "data": {"token": "tok123"}}

        async def scenario():
            http = AsyncMock()
            http.__aenter__.return_value = http
            http.get.side_effect = [user_response, files_response]
            http.post.return_value = check_response
            with patch("app.p115.httpx.AsyncClient", return_value=http):
                return await client.folders("0", "347887"), http

        result, http = asyncio.run(scenario())
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["folders"], [
            {"cid": "11", "pid": "0", "name": "文件夹A"},
            {"cid": "12", "pid": "0", "name": "文件夹B"},
        ])
        self.assertEqual(result["path"], [{"cid": "0", "name": "根目录"}])
        files_call = http.get.await_args_list[1]
        self.assertEqual(files_call.kwargs["params"].get("token"), "tok123")

    def test_folders_retries_on_timeout_then_succeeds(self):
        from httpx import ConnectTimeout
        client = P115Client("UID=1;CID=2")
        files_ok = MagicMock()
        files_ok.json.return_value = {"data": [], "path": [], "state": True, "count": 0}
        user_response = MagicMock()
        user_response.json.return_value = {"state": True, "data": {"uid": "9"}}
        check_response = MagicMock()
        check_response.json.return_value = {"state": 1, "data": {"token": "tok123"}}

        async def scenario():
            http = AsyncMock()
            http.__aenter__.return_value = http
            http.get.side_effect = [user_response, ConnectTimeout("boom"), files_ok]
            http.post.return_value = check_response
            with patch("app.p115.httpx.AsyncClient", return_value=http):
                return await client.folders("0", "347887"), http

        result, http = asyncio.run(scenario())
        self.assertEqual(result["folders"], [])
        self.assertEqual(len(http.get.await_args_list), 3)


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.notifications import ChannelMonitor, NotificationError, NotificationService, TelegramProvider


class NotificationTests(unittest.TestCase):
    def test_template_and_disabled_event(self):
        provider = TelegramProvider("token", "123")
        service = NotificationService(provider, {"success": False}, "{title}:{status}")
        self.assertEqual(service.render({"title": "电影", "status": "成功"}), "电影:成功")
        result = asyncio.run(service.notify("success", {"title": "电影"}))
        self.assertTrue(result["skipped"])

    def test_missing_credentials(self):
        with self.assertRaises(NotificationError):
            asyncio.run(TelegramProvider("", "").send("test"))

    def test_channel_parser_only_accepts_resource_links(self):
        html = '''<div data-post="demo/10"><div class="tgme_widget_message_text">资源
        <a href="https://hdhive.com/resource/abc-123">直达</a>
        <a href="https://ads.example.com/click">广告</a></div></div>'''
        response = AsyncMock()
        response.text = html
        response.raise_for_status = lambda: None
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        with patch("app.notifications.httpx.AsyncClient", return_value=client):
            items = asyncio.run(ChannelMonitor().fetch("demo_channel"))
        self.assertEqual(items[0].links, ["https://hdhive.com/resource/abc-123"])


if __name__ == "__main__":
    unittest.main()

"""Provider-neutral notifications and Telegram channel monitoring helpers."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

LOGGER = logging.getLogger("moon_dream.notifications")


class NotificationError(RuntimeError):
    pass


class TelegramProvider:
    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        self.token, self.chat_id, self.timeout = token.strip(), chat_id.strip(), timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> dict[str, Any]:
        if not self.configured:
            raise NotificationError("Telegram Bot Token 或 Chat ID 未配置")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotificationError(f"Telegram 返回 HTTP {response.status_code}") from exc
        if response.status_code >= 400 or not payload.get("ok"):
            raise NotificationError(str(payload.get("description") or "Telegram 发送失败"))
        return payload


class NotificationService:
    DEFAULT_TEMPLATE = "【影舟 MovieArk】{title}\n状态：{status}\n资源：{resource}\n清晰度：{resolution}\n大小：{size}\n保存路径：{save_path}\n{error}"

    def __init__(self, provider: TelegramProvider, events: dict[str, bool], template: str = ""):
        self.provider, self.events = provider, events
        self.template = template.strip() or self.DEFAULT_TEMPLATE

    def render(self, values: dict[str, Any]) -> str:
        safe = {key: str(values.get(key, "")) for key in ("title", "status", "resource", "resolution", "size", "save_path", "error")}
        try:
            return self.template.format_map(safe)
        except (KeyError, ValueError) as exc:
            raise NotificationError(f"通知模板无效：{exc}") from exc

    async def notify(self, event: str, values: dict[str, Any]) -> dict[str, Any]:
        if not self.events.get(event, False):
            return {"ok": True, "skipped": True, "reason": "event_disabled"}
        return await self.provider.send(self.render(values))


@dataclass(slots=True)
class ChannelMessage:
    message_id: int
    text: str
    links: list[str]


class ChannelMonitor:
    HDHIVE_RE = re.compile(r"https?://(?:www\.)?hdhive\.com/resource/[A-Za-z0-9_-]+", re.I)
    SHARE_RE = re.compile(r"https?://(?:115\.com|115cdn\.com)/s/[A-Za-z0-9]+(?:\?password=[A-Za-z0-9]+)?", re.I)

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    async def fetch(self, channel: str, after_id: int = 0) -> list[ChannelMessage]:
        name = channel.strip().rstrip("/").split("/")[-1].lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", name):
            raise ValueError("Telegram 频道地址无效")
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(f"https://t.me/s/{name}", headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        blocks = re.findall(r'data-post="[^"]+/(\d+)"[\s\S]*?<div class="tgme_widget_message_text[^>]*>([\s\S]*?)</div>', response.text)
        results: list[ChannelMessage] = []
        for raw_id, raw_text in blocks:
            message_id = int(raw_id)
            if message_id <= after_id:
                continue
            text = re.sub(r"<[^>]+>", " ", raw_text)
            links = list(dict.fromkeys(self.HDHIVE_RE.findall(raw_text) + self.SHARE_RE.findall(raw_text)))
            results.append(ChannelMessage(message_id, text.strip(), links))
        return sorted(results, key=lambda item: item.message_id)

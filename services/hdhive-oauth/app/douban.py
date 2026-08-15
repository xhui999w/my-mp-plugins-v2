"""Independent Douban provider used by MovieArk's backend.

The browser never calls Douban directly.  JSON search is preferred for the
hot/high-score lists; Top250 is read from the public Top250 HTML page.  A
short-lived fresh cache plus stale-on-error fallback keeps one bad response
from taking down the whole discovery page.
"""
from __future__ import annotations

import re
import time
from html import unescape
from typing import Any
from urllib.parse import quote, urljoin

import httpx


class DoubanProvider:
    JSON_URL = "https://movie.douban.com/j/search_subjects"
    TOP250_URL = "https://movie.douban.com/top250"
    TAGS = {
        "hot-movie": ("movie", "热门"),
        "high-movie": ("movie", "豆瓣高分"),
        "hot-tv": ("tv", "热门"),
        "high-tv": ("tv", "豆瓣高分"),
    }

    def __init__(self, timeout: float = 15.0, ttl: int = 600):
        self.timeout = timeout
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        # Public Douban lists do not require a user credential.
        return True

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(str(value).strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def normalize(item: dict[str, Any], rank: int, media_type: str) -> dict[str, Any]:
        douban_id = str(item.get("id") or item.get("douban_id") or "")
        title = str(item.get("title") or "未命名")
        year = str(item.get("year") or "")
        return {
            "id": f"douban:{douban_id}", "provider": "douban", "provider_name": "豆瓣",
            "provider_id": douban_id, "douban_id": douban_id, "tmdb_id": 0,
            "source_id": douban_id, "title": title, "original_title": str(item.get("original_title") or ""),
            "year": year, "media_type": media_type, "rating": DoubanProvider._number(item.get("rate") or item.get("rating")),
            "vote_count": int(DoubanProvider._number(item.get("vote_count") or item.get("votes"))),
            "poster": (f"/api/image-proxy?url={quote(str(item.get('cover') or item.get('pic') or ''), safe='')}" if (item.get("cover") or item.get("pic")) else ""), "backdrop": "", "overview": "",
            "rank": rank, "source_url": f"https://movie.douban.com/subject/{douban_id}/" if douban_id else "",
        }

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36 MovieArk/1.0",
            "Referer": "https://movie.douban.com/", "Accept": "application/json,text/plain,*/*",
        }

    def _result(self, category: str, page: int, items: list[dict[str, Any]], count: int) -> dict[str, Any]:
        return {"items": items, "page": page, "page_size": len(items), "total": len(items),
                "total_pages": page + (1 if len(items) >= count else 0), "has_more": len(items) >= count,
                "provider": "douban", "category": category, "configured": True, "error": None}

    async def _json_list(self, category: str, page: int, count: int) -> dict[str, Any]:
        media_type, tag = self.TAGS.get(category, self.TAGS["hot-movie"])
        params = {"type": media_type, "tag": tag, "page_limit": min(max(count, 1), 50), "page_start": max(0, (page - 1) * count)}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(self.JSON_URL, params=params, headers=self._headers())
        if response.status_code in (403, 429):
            raise RuntimeError(f"DOUBAN_HTTP_{response.status_code}")
        response.raise_for_status()
        payload = response.json()
        subjects = payload.get("subjects", []) if isinstance(payload, dict) else []
        items = [self.normalize(x, i + 1 + (page - 1) * count, media_type) for i, x in enumerate(subjects) if isinstance(x, dict)]
        return self._result(category, page, items, count)

    async def _top250(self, page: int, count: int) -> dict[str, Any]:
        start = max(0, (page - 1) * count)
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(self.TOP250_URL, params={"start": start, "filter": ""}, headers=self._headers())
        if response.status_code in (403, 429):
            raise RuntimeError(f"DOUBAN_HTTP_{response.status_code}")
        response.raise_for_status()
        text = response.text
        items: list[dict[str, Any]] = []
        for block in re.findall(r'<div class="item">(.*?)</div>\s*</div>', text, flags=re.S):
            link = re.search(r'href="https://movie\.douban\.com/subject/(\d+)/?"', block)
            title = re.search(r'<span class="title">\s*(.*?)\s*</span>', block, flags=re.S)
            rating = re.search(r'<span class="rating_num"[^>]*>\s*([\d.]+)', block)
            pic = re.search(r'<img[^>]+src="([^"]+)"', block)
            rank = re.search(r'<em class="">\s*(\d+)', block)
            info = re.search(r'<p class="">(.*?)</p>', block, flags=re.S)
            if not link or not title:
                continue
            clean = lambda value: re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", value or ""))).strip()
            info_text = clean(info.group(1) if info else "")
            year_match = re.search(r"(\d{4})", info_text)
            item = {"id": link.group(1), "title": clean(title.group(1)), "rate": rating.group(1) if rating else 0,
                    "cover": urljoin(self.TOP250_URL, pic.group(1)) if pic else "", "year": year_match.group(1) if year_match else ""}
            items.append(self.normalize(item, int(rank.group(1)) if rank else start + len(items) + 1, "movie"))
        return self._result("top250", page, items, count)

    async def discover(self, category: str = "hot-movie", page: int = 1, count: int = 20) -> dict[str, Any]:
        category = category if category in (*self.TAGS, "top250") else "hot-movie"
        key = f"{category}:{page}:{count}"
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self.ttl:
            return {**cached[1], "cached": True}
        try:
            result = await self._top250(page, count) if category == "top250" else await self._json_list(category, page, count)
            self._cache[key] = (now, result)
            return {**result, "cached": False}
        except Exception as exc:
            if cached:
                return {**cached[1], "cached": True, "stale": True, "error": f"豆瓣暂时不可用，显示缓存数据（{type(exc).__name__}）"}
            return {"items": [], "page": page, "page_size": 0, "total": 0, "total_pages": 0, "has_more": False,
                    "provider": "douban", "category": category, "configured": True, "error": f"豆瓣数据暂时不可用（{type(exc).__name__}）"}

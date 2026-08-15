"""Douban discovery provider backed by Douban's public Explore endpoint."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class DoubanProvider:
    BASE_URL = "https://m.douban.com/rexxar/api/v2"
    GENRES = (
        "喜剧", "爱情", "动作", "科幻", "动画", "悬疑", "犯罪", "惊悚", "冒险",
        "音乐", "历史", "奇幻", "恐怖", "战争", "传记", "歌舞", "武侠", "灾难",
        "西部", "纪录片", "短片", "剧情", "家庭", "儿童",
    )
    COUNTRIES = (
        "华语", "欧美", "韩国", "日本", "中国大陆", "美国", "中国香港", "中国台湾",
        "英国", "法国", "德国", "意大利", "西班牙", "印度", "泰国", "俄罗斯", "加拿大",
        "澳大利亚", "爱尔兰", "瑞典", "巴西", "丹麦",
    )
    # Codes are returned by Douban's current Explore endpoint.
    SORTS = {"recommend": "T", "hot": "U", "release": "R", "score": "S"}

    def __init__(self, timeout: float = 20.0, ttl: int = 600):
        self.timeout, self.ttl = timeout, ttl
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def configured(self) -> bool:
        return True

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "supports_country": True, "supports_year": True, "supports_genre": True,
            "supports_sort": True, "supports_media_type": True, "supports_pagination": True,
        }

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        year = time.localtime().tm_year
        return {
            "genres": list(cls.GENRES), "countries": list(cls.COUNTRIES),
            "sorts": [
                {"code": "recommend", "name": "综合排序"}, {"code": "hot", "name": "近期热度"},
                {"code": "release", "name": "首映时间"}, {"code": "score", "name": "高分优先"},
            ],
            "years": [str(value) for value in range(year, year - 8, -1)]
            + [f"{decade}s" for decade in range((year // 10) * 10 - 10, 1950, -10)],
        }

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def normalize(cls, item: dict[str, Any], rank: int, media_type: str) -> dict[str, Any]:
        rating = item.get("rating") or {}
        pic = item.get("pic") or {}
        subtitle = str(item.get("card_subtitle") or "")
        parts = [part.strip() for part in subtitle.split("/")]
        year = str(item.get("year") or "")
        if not re.fullmatch(r"(?:19|20)\d{2}", year):
            match = re.search(r"(?:19|20)\d{2}", subtitle)
            year = match.group(0) if match else ""
        countries = parts[1].split() if len(parts) > 1 else []
        genres = parts[2].split() if len(parts) > 2 else []
        douban_id = str(item.get("id") or "")
        poster = pic.get("large") or pic.get("normal") if isinstance(pic, dict) else ""
        score = rating.get("value") if isinstance(rating, dict) else rating
        return {
            "id": f"douban:{douban_id}", "provider": "douban", "provider_name": "豆瓣",
            "provider_id": douban_id, "douban_id": douban_id, "tmdb_id": int(item.get("tmdb_id") or 0),
            "source_id": douban_id, "title": str(item.get("title") or "未命名"),
            "original_title": str(item.get("original_title") or ""), "year": year,
            "media_type": str(item.get("item_type") or item.get("type") or media_type),
            "rating": round(cls._number(score), 1),
            "vote_count": int(cls._number(rating.get("count") if isinstance(rating, dict) else 0)),
            "poster": f"/api/image-proxy?url={quote(str(poster), safe='')}" if poster else "",
            "backdrop": "", "overview": subtitle, "countries": countries, "region": countries,
            "genres": genres, "languages": [], "rank": rank,
            "source_url": f"https://movie.douban.com/subject/{douban_id}/",
        }

    @staticmethod
    def _year_tag(value: str) -> str:
        if value.isdigit() and len(value) == 4:
            return value
        if value.endswith("s") and value[:-1].isdigit():
            return value[:-1] + "年代"
        return ""

    async def discover(self, query: dict[str, Any] | str, page: int | None = None, count: int = 20) -> dict[str, Any]:
        # Legacy ranking calls remain compatible; the rankings page is not
        # coupled to the Explore UI.
        if isinstance(query, str):
            category = query
            query = {
                "media_type": "tv" if category.endswith("-tv") else "movie",
                "sort": "score" if category.startswith("high-") or category == "top250" else "hot",
                "page": page or 1,
            }
        media_type = "tv" if query.get("media_type") == "tv" else "movie"
        page = max(1, int(query.get("page", 1)))
        genre = str(query.get("genre") or "")
        country = str(query.get("country") or "")
        sort = str(query.get("sort") or "recommend")
        year = str(query.get("year") or "")
        if genre and genre not in self.GENRES:
            raise ValueError("INVALID_DOUBAN_GENRE")
        if country and country not in self.COUNTRIES:
            raise ValueError("INVALID_DOUBAN_COUNTRY")
        if sort not in self.SORTS:
            sort = "recommend"

        selected = {key: value for key, value in (("类型", genre), ("地区", country)) if value}
        # Douban applies Explore filters through both selected_categories and
        # the tags list. Omitting tags makes the server silently ignore the UI.
        tags = [value for value in (genre, country, self._year_tag(year)) if value]
        if not tags:
            tags = ["电视剧" if media_type == "tv" else "电影"]
        params = {
            "refresh": "0", "start": (page - 1) * count, "count": count,
            "selected_categories": json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
            "uncollect": "false", "tags": ",".join(tags), "sort": self.SORTS[sort],
        }
        key = "douban:explore:" + json.dumps(
            {**params, "media_type": media_type, "page": page}, ensure_ascii=False, sort_keys=True,
        )
        cached = self._cache.get(key)
        now = time.time()
        if cached and now - cached[0] < self.ttl:
            return {**cached[1], "cached": True}

        logger.info("[douban] type=%s sort=%s genre=%s country=%s year=%s page=%s", media_type, sort, genre, country, year, page)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                "Referer": "https://movie.douban.com/explore", "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(f"{self.BASE_URL}/{media_type}/recommend", params=params, headers=headers)
            if response.status_code in (403, 429):
                raise RuntimeError(f"DOUBAN_HTTP_{response.status_code}")
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("items", []) if isinstance(payload, dict) else []
            subjects = [item for item in raw if isinstance(item, dict) and item.get("card") == "subject" and item.get("id")]
            items = [self.normalize(item, (page - 1) * count + index + 1, media_type) for index, item in enumerate(subjects)]
            total = int(payload.get("total") or len(items))
            result = {
                "items": items, "page": page, "page_size": len(items), "total": total,
                "total_pages": (total + count - 1) // count if total else 0,
                "has_more": page * count < total, "provider": "douban", "source": "douban-explore",
                "configured": True, "error": None,
                "query": {"media_type": media_type, "sort": sort, "genre": genre, "country": country, "year": year},
            }
            self._cache[key] = (now, result)
            return {**result, "cached": False}
        except Exception as exc:
            logger.warning("[douban] request failed error_type=%s", type(exc).__name__)
            if cached:
                return {**cached[1], "cached": True, "stale": True, "error": "豆瓣暂时不可用，正在显示缓存结果。"}
            return {
                "items": [], "page": page, "page_size": 0, "total": 0, "total_pages": 0,
                "has_more": False, "provider": "douban", "source": "douban-explore", "configured": True,
                "error": "豆瓣数据暂时不可用，请稍后重试。", "detail": type(exc).__name__,
            }

"""Explore providers, normalized media models, filters and lightweight cache."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx


REGIONS = {
    "CN": "中国大陆", "HK": "香港", "TW": "台湾", "JP": "日本", "KR": "韩国",
    "US": "美国", "CA": "加拿大", "GB": "英国", "AU": "澳大利亚", "NZ": "新西兰",
    "DE": "德国", "FR": "法国", "ES": "西班牙", "IT": "意大利", "NL": "荷兰",
    "SE": "瑞典", "IN": "印度", "TH": "泰国", "MY": "马来西亚", "PH": "菲律宾",
    "VN": "越南", "BR": "巴西", "MX": "墨西哥",
}
LANGUAGES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "fr": "法语", "de": "德语",
    "es": "西班牙语", "it": "意大利语", "ru": "俄语", "pt": "葡萄牙语", "ar": "阿拉伯语",
    "hi": "印地语", "th": "泰语", "id": "印尼语", "ms": "马来语", "tr": "土耳其语", "vi": "越南语",
}
SORTS = {
    "popularity.desc": "热度降序", "popularity.asc": "热度升序",
    "primary_release_date.desc": "上映日期降序", "primary_release_date.asc": "上映日期升序",
    "vote_average.desc": "评分降序", "vote_average.asc": "评分升序",
    "vote_count.desc": "评分人数降序", "vote_count.asc": "评分人数升序",
    "title.asc": "片名 A-Z", "title.desc": "片名 Z-A",
}
RANKINGS = {
    "trending-day": "今日趋势", "trending-week": "本周趋势", "popular-movie": "热门电影",
    "popular-tv": "热门电视剧", "top-movie": "高分电影", "top-tv": "高分电视剧",
    "now-playing": "正在上映", "upcoming": "即将上映",
}
STREAMING_NAMES = {
    "netflix": ("Netflix", ("Netflix",)),
    "max": ("HBO Max", ("Max", "HBO Max")),
    "prime": ("Prime Video", ("Amazon Prime Video", "Prime Video")),
    "disney": ("Disney+", ("Disney Plus", "Disney+")),
    "apple": ("Apple TV+", ("Apple TV", "Apple TV Plus", "Apple TV+", "Apple TV App")),
}


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    name: str
    icon: str
    configured: bool
    capabilities: tuple[str, ...]
    supported_media_types: tuple[str, ...] = ("movie", "tv")

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "enabled": True, "capabilities": list(self.capabilities), "supported_media_types": list(self.supported_media_types)}


class TTLCache:
    def __init__(self) -> None:
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            value = self._values.get(key)
            if not value or value[0] < time.time():
                self._values.pop(key, None)
                return None
            return value[1]

    async def set(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            self._values[key] = (time.time() + ttl, value)


CACHE = TTLCache()


class TMDBProvider:
    base_url = "https://api.themoviedb.org/3"

    def __init__(self, token: str, language: str = "zh-CN", region: str = "CN", timeout: int = 30):
        self.token = token.strip()
        self.language = language or "zh-CN"
        self.region = region or "CN"
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def auth(self) -> tuple[dict[str, str], dict[str, Any]]:
        headers = {"Accept": "application/json"}
        params: dict[str, Any] = {"language": self.language, "region": self.region}
        if self.token.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            params["api_key"] = self.token
        return headers, params

    async def request(self, path: str, params: dict[str, Any] | None = None, ttl: int = 900) -> tuple[dict[str, Any], bool]:
        if not self.configured:
            raise RuntimeError("TMDB_NOT_CONFIGURED")
        headers, defaults = self.auth()
        query = {**defaults, **(params or {})}
        key = f"tmdb:{path}:{sorted(query.items())}"
        cached = await CACHE.get(key)
        if cached is not None:
            return cached, True
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}{path}", headers=headers, params=query)
        if response.status_code in (401, 403):
            raise RuntimeError("TMDB_AUTH_FAILED")
        response.raise_for_status()
        payload = response.json()
        await CACHE.set(key, payload, ttl)
        return payload, False

    @staticmethod
    def normalize(item: dict[str, Any], media_type: str, provider: str = "tmdb") -> dict[str, Any]:
        date = str(item.get("release_date") or item.get("first_air_date") or "")
        poster_path = item.get("poster_path")
        backdrop_path = item.get("backdrop_path")
        return {
            "id": f"tmdb:{media_type}:{item.get('id')}", "provider": provider, "provider_id": item.get("id"),
            "tmdb_id": item.get("id"), "imdb_id": None, "douban_id": None, "media_type": media_type,
            "title": item.get("title") or item.get("name") or "未命名", "original_title": item.get("original_title") or item.get("original_name") or "",
            "poster": f"https://image.tmdb.org/t/p/w342{poster_path}" if poster_path else "", "backdrop": f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else "",
            "overview": item.get("overview") or "", "release_date": date, "year": date[:4],
            "rating": round(float(item.get("vote_average") or 0), 1), "vote_count": item.get("vote_count") or 0,
            "genres": item.get("genre_ids") or [], "countries": item.get("origin_country") or [],
            "languages": [item.get("original_language")] if item.get("original_language") else [],
            "popularity": item.get("popularity") or 0, "actors": [], "director": None, "watch_providers": [],
        }

    async def genres(self, media_type: str) -> list[dict[str, Any]]:
        data, _ = await self.request(f"/genre/{media_type}/list", ttl=86400)
        return data.get("genres", [])

    async def watch_provider_id(self, key: str, media_type: str, region: str) -> int | None:
        names = STREAMING_NAMES.get(key, ("", ()))[1]
        params = {"watch_region": region} if region else {}
        data, _ = await self.request(f"/watch/providers/{media_type}", params, ttl=86400)
        for provider in data.get("results", []):
            if str(provider.get("provider_name")) in names:
                return int(provider["provider_id"])
        return None

    async def discover(self, query: dict[str, Any]) -> dict[str, Any]:
        media_type = query.get("media_type", "movie")
        page = max(1, int(query.get("page", 1)))
        params: dict[str, Any] = {"page": page, "sort_by": query.get("sort", "popularity.desc"), "vote_average.gte": query.get("rating", 0)}
        mappings = {"region": "region", "language": "with_original_language", "genre": "with_genres", "year": "primary_release_year" if media_type == "movie" else "first_air_date_year"}
        for source, target in mappings.items():
            if query.get(source):
                params[target] = query[source]
        platform = query.get("platform")
        if platform in STREAMING_NAMES:
            # Empty region means ?all regions?: do not force the account default.
            watch_region = query.get("region") or ""
            watch_id = await self.watch_provider_id(platform, media_type, watch_region)
            if watch_id is None:
                return {"items": [], "page": page, "total_pages": 0, "total": 0, "has_more": False, "cached": False}
            params["with_watch_providers"] = watch_id
            if watch_region:
                params["watch_region"] = watch_region
        payload, cached = await self.request(f"/discover/{media_type}", params, ttl=600)
        items = [self.normalize(item, media_type, platform or "tmdb") for item in payload.get("results", [])]
        return {"items": items, "page": payload.get("page", page), "page_size": len(items), "total": payload.get("total_results", 0), "total_pages": payload.get("total_pages", 0), "has_more": page < int(payload.get("total_pages", 0)), "provider": platform or "tmdb", "source": "tmdb-discover", "cached": cached, "configured": True, "error": None}

    async def ranking(self, ranking: str, page: int = 1) -> dict[str, Any]:
        paths = {
            "trending-day": ("/trending/all/day", None), "trending-week": ("/trending/all/week", None),
            "popular-movie": ("/movie/popular", "movie"), "popular-tv": ("/tv/popular", "tv"),
            "top-movie": ("/movie/top_rated", "movie"), "top-tv": ("/tv/top_rated", "tv"),
            "now-playing": ("/movie/now_playing", "movie"), "upcoming": ("/movie/upcoming", "movie"),
        }
        path, fixed_type = paths.get(ranking, paths["trending-week"])
        payload, cached = await self.request(path, {"page": page}, ttl=900)
        items = []
        for item in payload.get("results", []):
            media_type = fixed_type or item.get("media_type")
            if media_type in ("movie", "tv"):
                items.append(self.normalize(item, media_type))
        return {"items": items, "page": page, "total_pages": payload.get("total_pages", 0), "has_more": page < int(payload.get("total_pages", 0)), "provider": "tmdb", "source": ranking, "cached": cached, "configured": True, "error": None}


def registry(tmdb_configured: bool) -> list[dict[str, Any]]:
    common = ("discover", "rankings", "filters", "pagination", "details")
    providers = [ProviderInfo("tmdb", "TheMovieDB", "🎬", tmdb_configured, common)]
    providers.append(ProviderInfo("douban", "豆瓣", "🟢", False, ("rankings",)))
    for key, (name, _) in STREAMING_NAMES.items():
        providers.append(ProviderInfo(key, name, "▶", tmdb_configured, ("discover", "filters", "pagination")))
    return [provider.as_dict() for provider in providers]


def filter_metadata(genres: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    year = time.localtime().tm_year
    return {"regions": [{"code": code, "name": name} for code, name in REGIONS.items()], "languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()], "sorts": [{"code": code, "name": name} for code, name in SORTS.items()], "rankings": [{"id": code, "name": name} for code, name in RANKINGS.items()], "years": list(range(year, year - 15, -1)), "genres": genres or []}

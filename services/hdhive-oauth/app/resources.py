"""Provider-neutral media resource normalization, filtering and de-duplication."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(slots=True)
class ResourceItem:
    provider: str
    provider_name: str
    provider_resource_id: str
    title: str
    original_title: str = ""
    url: str = ""
    share_url: str = ""
    resolution: str = ""
    quality: str = ""
    size: str = ""
    subtitle: str = ""
    language: str = ""
    audio: str = ""
    source_type: str = ""
    uploader: str = ""
    uploader_avatar: str = ""
    user_level: str = ""
    vip: bool = False
    points: int | None = None
    is_unlocked: bool = False
    unlock_count: int | None = None
    publish_time: str = ""
    season: str = ""
    episode: str = ""
    resource_tags: list[str] = field(default_factory=list)
    transfer_supported: bool = False
    duplicate_count: int = 1

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class ResourceProvider:
    id = "base"
    name = "资源来源"
    logo = "◉"
    capabilities: tuple[str, ...] = ("search", "detail")

    @property
    def enabled(self) -> bool:
        return True

    @property
    def configured(self) -> bool:
        return True

    async def search(self, media_type: str, tmdb_id: int, title: str = "") -> list[ResourceItem]:
        raise NotImplementedError

    async def detail(self, resource_id: str) -> ResourceItem | None:
        return None

    async def transfer(self, resource_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "logo": self.logo, "enabled": self.enabled, "configured": self.configured, "capabilities": list(self.capabilities)}


PAN_TYPE_LABELS = {
    "115": "115网盘",
    "quark": "夸克网盘",
    "aliPan": "阿里云盘",
    "aliyun": "阿里云盘",
    "baiDu": "百度网盘",
    "xunLei": "迅雷",
    "tianyi": "天翼云盘",
    "123": "123云盘",
    "139": "移动云盘",
    "guangYa": "光丫网盘",
    "ed2k": "ED2K",
    "magnet": "磁力链接",
}


def humanize_pan_type(value: object) -> str:
    """把影巢 pan_type 映射为可读网盘名称，未知类型原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return PAN_TYPE_LABELS.get(text) or text


class HDHiveResourceProvider(ResourceProvider):
    id = "hdhive"
    name = "影巢"
    logo = "🎬"
    capabilities = ("search", "detail", "unlock", "transfer")

    def __init__(self, query: Callable[[str, int, str], Awaitable[Any]], configured: bool = True):
        self._query = query
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("items", "resources", "list", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return HDHiveResourceProvider._items(payload.get("data"))

    @staticmethod
    def _clean_list(value: Any) -> str:
        """HDHive 会把数组序列化成 Python 字面量字符串，例如 ['蓝光原盘/ISO']。"""
        if value is None:
            return ""
        if isinstance(value, list):
            parts = [str(x).strip() for x in value if str(x).strip()]
            return " / ".join(parts)
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1]
            parts = [x.strip().strip("'\"") for x in re.split(r"[,\n]", inner) if x.strip()]
            return " / ".join(parts)
        return text

    @staticmethod
    def normalize(raw: dict[str, Any]) -> ResourceItem:
        slug = str(raw.get("slug") or raw.get("resource_slug") or raw.get("id") or "").strip()
        title = str(raw.get("title") or raw.get("name") or slug or "未命名资源").strip()
        tags = raw.get("tags") or raw.get("resource_tags") or []
        if isinstance(tags, str):
            tags = [x.strip() for x in re.split(r"[,/|]", tags) if x.strip()]
        share = str(raw.get("share_url") or raw.get("full_url") or "").strip()
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
        points_value = raw.get("actual_unlock_points")
        if points_value is None:
            points_value = raw.get("unlock_points")
        if points_value is None:
            points_value = raw.get("points")
        points: int | None = None
        if isinstance(points_value, (int, float)) and not isinstance(points_value, bool):
            points = int(points_value)
        elif str(points_value or "").isdigit():
            points = int(points_value)
        is_unlocked = bool(raw.get("is_unlocked"))
        raw_pan = str(
            raw.get("pan_type") or raw.get("source_type") or raw.get("disk_type")
            or raw.get("cloud_type") or raw.get("drive_type") or raw.get("website") or ""
        ).strip()
        source_type = humanize_pan_type(raw_pan) or "其他来源"
        transfer_supported = raw_pan.strip().lower() in ("115", "magnet", "ed2k")
        unlock_count: int | None = None
        if str(raw.get("unlocked_users_count") or raw.get("unlock_count") or "").isdigit():
            unlock_count = int(raw.get("unlocked_users_count") or raw.get("unlock_count"))
        return ResourceItem(
            provider="hdhive", provider_name="影巢", provider_resource_id=slug, title=title,
            original_title=str(raw.get("original_title") or ""),
            url=str(raw.get("url") or (f"https://hdhive.com/resource/{slug}" if slug else "")), share_url=share,
            resolution=HDHiveResourceProvider._clean_list(raw.get("resolution") or raw.get("definition") or media.get("resolution") or ""),
            quality=HDHiveResourceProvider._clean_list(raw.get("quality") or raw.get("source") or media.get("quality") or ""),
            size=str(raw.get("share_size") or raw.get("size") or raw.get("file_size") or ""),
            subtitle=HDHiveResourceProvider._clean_list(raw.get("subtitle") or raw.get("subtitles") or media.get("subtitle") or ""),
            language=str(raw.get("language") or media.get("language") or ""),
            audio=HDHiveResourceProvider._clean_list(raw.get("audio") or raw.get("audio_info") or media.get("audio") or ""),
            source_type=source_type,
            uploader=str(user.get("nickname") or user.get("name") or raw.get("uploader") or raw.get("author") or raw.get("username") or ""),
            uploader_avatar=str(user.get("avatar_url") or user.get("avatar") or raw.get("uploader_avatar") or raw.get("avatar") or ""),
            user_level=str(raw.get("user_level") or raw.get("level") or user.get("level") or ""),
            vip=bool(raw.get("is_vip") or raw.get("vip") or raw.get("member") or user.get("is_vip")),
            points=points,
            is_unlocked=is_unlocked,
            unlock_count=unlock_count,
            publish_time=str(raw.get("publish_time") or raw.get("created_at") or ""),
            season=str(raw.get("season") if raw.get("season") is not None else media.get("season") or ""),
            episode=str(raw.get("episode") if raw.get("episode") is not None else media.get("episode") or ""),
            resource_tags=[str(x) for x in tags], transfer_supported=transfer_supported,
        )

    async def search(self, media_type: str, tmdb_id: int, title: str = "") -> tuple[list[ResourceItem], list[dict[str, str]]]:
        if not self.configured:
            return [], []
        result = await self._query(media_type, tmdb_id, title)
        if isinstance(result, tuple):
            payload, errors = result
        else:
            payload, errors = result, []
        items = [self.normalize(x) for x in self._items(payload)]
        return items, [dict(e) if isinstance(e, dict) else {"provider": self.id, "error": str(e)} for e in errors]


class ResourceProviderRegistry:
    def __init__(self, providers: list[ResourceProvider] | None = None):
        self.providers = providers or []

    async def search(self, media_type: str, tmdb_id: int, title: str = "") -> tuple[list[ResourceItem], list[dict[str, str]]]:
        items: list[ResourceItem] = []
        errors: list[dict[str, str]] = []
        for provider in self.providers:
            if not provider.enabled or not provider.configured:
                continue
            try:
                result = await provider.search(media_type, tmdb_id, title)
                if isinstance(result, tuple):
                    provider_items, provider_errors = result
                    items.extend(provider_items)
                    errors.extend(provider_errors)
                else:
                    items.extend(result)
            except Exception as exc:  # isolation: one provider must not stop the rest
                errors.append({"provider": provider.id, "error": str(exc)})
        return deduplicate_resources(items), errors

    def infos(self) -> list[dict[str, Any]]:
        return [provider.info() for provider in self.providers]


def _clean(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def resource_fingerprint(item: ResourceItem) -> str:
    strong = item.share_url.strip().lower() or item.url.strip().lower()
    if strong:
        raw = f"link:{strong}"
    else:
        raw = "|".join((_clean(item.title), _clean(item.size), _clean(item.resolution), _clean(item.quality), _clean(item.season), _clean(item.episode)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deduplicate_resources(items: list[ResourceItem]) -> list[ResourceItem]:
    result: list[ResourceItem] = []
    seen: dict[str, ResourceItem] = {}
    for item in items:
        key = resource_fingerprint(item)
        if key in seen:
            seen[key].duplicate_count += 1
            continue
        seen[key] = item
        result.append(item)
    return result


def filter_resources(items: list[ResourceItem], provider: str = "", season: str = "", resolution: str = "", quality: str = "", language: str = "", source_type: str = "") -> list[ResourceItem]:
    filters = {"provider": provider, "season": season, "resolution": resolution, "quality": quality, "language": language, "source_type": source_type}
    return [item for item in items if all(not value or _clean(str(getattr(item, key))) == _clean(value) for key, value in filters.items())]


def filter_options(items: list[ResourceItem]) -> dict[str, list[str]]:
    return {key: sorted({str(getattr(item, key)) for item in items if getattr(item, key)}) for key in ("provider", "uploader", "season", "resolution", "quality", "language", "source_type")}

"""Provider-neutral media resource normalization, filtering and de-duplication."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import httpx


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
    is_official: bool = False
    is_free: bool = False
    official_label: str = ""
    fee_label: str = ""
    transfer_supported: bool = False
    duplicate_count: int = 1
    indexer: str = ""
    magnet: str = ""
    torrent_url: str = ""
    seeders: int | None = None
    leechers: int | None = None
    category: str = ""
    provider_sources: list[str] = field(default_factory=list)
    # 资源原始标题与影视名称分离；季/集结构化字段（从 remark/title 解析）
    media_title: str = ""
    resource_title: str = ""
    season_number: int = 0
    episode_start: int = 0
    episode_end: int = 0
    episode_count: int = 0
    is_complete: bool = False
    is_finale: bool = False
    season_episode_label: str = ""

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


_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int(text: str) -> int | None:
    """把中文/阿拉伯数字（季、集）转成 int，覆盖 0~299 常见范围。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "百" or s == "一百":
        return 100
    if s == "十":
        return 10
    if "百" in s:
        parts = s.split("百")
        h = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        rest = _cn_int(parts[1]) or 0 if parts[1] else 0
        return h * 100 + rest
    if "十" in s:
        left, _, right = s.partition("十")
        l = _CN_NUM.get(left, 0) if left else 0
        r = _CN_NUM.get(right, 0) if right else 0
        return (l * 10 + r) if left else (10 + r)
    if s in _CN_NUM:
        return _CN_NUM[s]
    return None


_AUDIO_RE = re.compile(r"(DDP\s?\d(?:\.\d)?|TrueHD|Atmos|ATMOS|AAC|FLAC|DTS(?:-HD)?|Opus|EAC3|AC3|LPCM)", re.I)
_CODEC_RE = re.compile(r"(H\.?265|H\.?264|H\.?266|HEVC|AVC|VP9|AV1|REMUX)", re.I)


def parse_season_episode(text: str) -> dict[str, Any]:
    """从资源标题/remark 中解析季、集范围、集数、是否完结。

    兼容：S01E01 / S01E01-E18 / S01E187 / S01 / Season 1 / 第1季 / 第八季 /
    第1-18集 / 全17集 / 全集 / 已完结 等。
    """
    if not text:
        return {}
    t = str(text)
    season = None
    ep_start = None
    ep_end = None
    ep_count = None
    is_complete = False
    is_finale = False

    m = re.search(r"[Ss](\d{1,2})(?![0-9])", t)
    if m:
        season = int(m.group(1))
    else:
        m = re.search(r"[Ss]eason\s*(\d{1,2})", t, re.I)
        if m:
            season = int(m.group(1))
        else:
            m = re.search(r"第\s*([0-9一二三四五六七八九十百零两]+)\s*季", t)
            if m:
                season = _cn_int(m.group(1))
            else:
                m = re.search(r"([0-9一二三四五六七八九十百零两]+)\s*季", t)
                if m:
                    season = _cn_int(m.group(1))

    m = re.search(r"(?<![a-z])[Ee](\d{1,3})(?:\s*-\s*[Ee]?(\d{1,3}))?(?![a-z])", t)
    if m:
        ep_start = int(m.group(1))
        if m.group(2):
            ep_end = int(m.group(2))
    else:
        m = re.search(r"第\s*([0-9一二三四五六七八九十百零两]+)\s*-\s*([0-9一二三四五六七八九十百零两]+)\s*集", t)
        if m:
            ep_start = _cn_int(m.group(1))
            ep_end = _cn_int(m.group(2))
        else:
            m = re.search(r"第\s*([0-9一二三四五六七八九十百零两]+)\s*集", t)
            if m:
                ep_start = _cn_int(m.group(1))

    m = re.search(r"全\s*([0-9一二三四五六七八九十百零两]+)\s*集", t)
    if m:
        ep_count = _cn_int(m.group(1))
        is_complete = True
    m = re.search(r"([0-9一二三四五六七八九十百零两]+)\s*集", t)
    if m and ep_count is None and ep_start is None and ep_end is None:
        ep_count = _cn_int(m.group(1))

    if re.search(r"全集|全\d+集|已完结|完结", t):
        is_complete = True
    if re.search(r"已完结|完结", t):
        is_finale = True

    if ep_count is None and ep_start and ep_end and ep_end >= ep_start:
        ep_count = ep_end - ep_start + 1

    return {k: v for k, v in {
        "season_number": season or 0,
        "episode_start": ep_start or 0,
        "episode_end": ep_end or 0,
        "episode_count": ep_count or 0,
        "is_complete": is_complete,
        "is_finale": is_finale,
    }.items()}


def build_season_episode_label(se: dict[str, Any]) -> str:
    """把解析结果拼成展示文案，例如 S01E01-E20 · 第1季 · 第1-20集。"""
    if not se:
        return ""
    season = se.get("season_number") or 0
    ep_start = se.get("episode_start") or 0
    ep_end = se.get("episode_end") or 0
    ep_count = se.get("episode_count") or 0
    is_complete = se.get("is_complete")

    segs: list[str] = []
    if season:
        segs.append(f"S{season:02d}")
    if ep_start:
        if ep_end and ep_end != ep_start:
            segs.append(f"E{ep_start:02d}-E{ep_end:02d}")
        else:
            segs.append(f"E{ep_start:02d}")

    cn: list[str] = []
    if season:
        cn.append(f"第{season}季")
    if ep_start and ep_end and ep_end != ep_start:
        cn.append(f"第{ep_start}-{ep_end}集")
    elif ep_start:
        cn.append(f"第{ep_start}集")
    if ep_count:
        cn.append(f"全{ep_count}集")
    if is_complete:
        cn.append("已完结")

    label = " ".join(segs)
    if cn:
        label = (label + " · " + " · ".join(cn)).strip(" ·")
    return label


def parse_audio_codec(text: str) -> str:
    """从资源标题提取音轨编码，如 DDP5.1 / TrueHD / AAC。"""
    if not text:
        return ""
    found = _AUDIO_RE.findall(str(text))
    seen: list[str] = []
    for f in found:
        key = f.strip().upper().replace(" ", "")
        if key not in [s.upper() for s in seen]:
            seen.append(f.strip())
    return " / ".join(seen)


def parse_video_codec(text: str) -> str:
    """从资源标题提取视频编码，如 H265 / REMUX。"""
    if not text:
        return ""
    found = _CODEC_RE.findall(str(text))
    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return " / ".join(seen)


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
        # 影巢真实资源标题在 remark 字段；title 在某些媒体下会被 API 覆盖成影视名。
        # 资源卡片主展示用 resource_title，media_title 作为次级信息，二者互不覆盖。
        resource_title = str(raw.get("remark") or raw.get("resource_title") or "").strip()
        if not resource_title:
            resource_title = title
        media_title = title
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
        # 真实影巢标签：官组(is_official) / 免费(is_free_for_user 或 0 积分) / 积分(unlock_points>0)
        is_official = bool(raw.get("is_official"))
        is_free = bool(raw.get("is_free_for_user")) or (points == 0)
        official_label = "官组" if is_official else "非官组"
        if is_free:
            fee_label = "免费"
        elif points and points > 0:
            fee_label = "积分"
        else:
            fee_label = "未知"
        raw_pan = str(
            raw.get("pan_type") or raw.get("source_type") or raw.get("disk_type")
            or raw.get("cloud_type") or raw.get("drive_type") or raw.get("website") or ""
        ).strip()
        source_type = humanize_pan_type(raw_pan) or "其他来源"
        transfer_supported = raw_pan.strip().lower() in ("115", "magnet", "ed2k")
        unlock_count: int | None = None
        if str(raw.get("unlocked_users_count") or raw.get("unlock_count") or "").isdigit():
            unlock_count = int(raw.get("unlocked_users_count") or raw.get("unlock_count"))
        subtitle_text = HDHiveResourceProvider._clean_list(
            raw.get("subtitle_language") or raw.get("subtitle_type") or raw.get("subtitle")
            or raw.get("subtitles") or media.get("subtitle") or ""
        )
        item = ResourceItem(
            provider="hdhive", provider_name="影巢", provider_resource_id=slug, title=title,
            original_title=str(raw.get("original_title") or ""),
            url=str(raw.get("url") or (f"https://hdhive.com/resource/{slug}" if slug else "")), share_url=share,
            resolution=HDHiveResourceProvider._clean_list(raw.get("video_resolution") or raw.get("resolution") or raw.get("definition") or media.get("resolution") or ""),
            quality=HDHiveResourceProvider._clean_list(raw.get("quality") or raw.get("source") or media.get("quality") or ""),
            size=str(raw.get("share_size") or raw.get("size") or raw.get("file_size") or ""),
            subtitle=subtitle_text,
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
            resource_tags=tags,
            is_official=is_official,
            is_free=is_free,
            official_label=official_label,
            fee_label=fee_label,
            transfer_supported=transfer_supported,
        )
        # 解析季/集：影巢无结构化字段，从资源标题 + 影视名 + 字幕文案中提取
        se = parse_season_episode(
            " ".join(filter(None, [resource_title, media_title, subtitle_text]))
        )
        # API 若已返回显式 season/episode 结构化字段，则优先使用；否则用解析结果
        api_season = raw.get("season") if raw.get("season") is not None else media.get("season")
        api_episode = raw.get("episode") if raw.get("episode") is not None else media.get("episode")
        if api_season not in (None, "", 0):
            try:
                se["season_number"] = int(api_season)
            except (TypeError, ValueError):
                pass
        if api_episode not in (None, "", 0):
            try:
                se["episode_start"] = int(api_episode)
            except (TypeError, ValueError):
                pass
        se_label = build_season_episode_label(se) if se else ""
        item.season_number = int(se.get("season_number") or 0)
        item.episode_start = int(se.get("episode_start") or 0)
        item.episode_end = int(se.get("episode_end") or 0)
        item.episode_count = int(se.get("episode_count") or 0)
        item.is_complete = bool(se.get("is_complete"))
        item.is_finale = bool(se.get("is_finale"))
        item.season_episode_label = se_label
        # 回填旧字符串字段，保证前端既有渲染与缓存兼容：
        # API 已返回显式 season/episode 时原样保留；否则用解析结果格式化。
        if api_season not in (None, "", 0):
            item.season = str(api_season)
        elif item.season_number:
            item.season = str(item.season_number)
        if api_episode not in (None, "", 0):
            item.episode = str(api_episode)
        elif item.episode_start:
            item.episode = f"E{item.episode_start:02d}" + (
                f"-E{item.episode_end:02d}" if item.episode_end and item.episode_end != item.episode_start else ""
            )
        item.media_title = media_title
        item.resource_title = resource_title
        item.title = resource_title
        return item

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
        active = [provider for provider in self.providers if provider.enabled and provider.configured]

        async def run(provider: ResourceProvider):
            try:
                result = await provider.search(media_type, tmdb_id, title)
                if isinstance(result, tuple):
                    return provider, result[0], result[1]
                return provider, result, []
            except Exception as exc:  # isolation: one provider must not stop the rest
                return provider, [], [{"provider": provider.id, "error": str(exc)}]

        for provider, provider_items, provider_errors in await asyncio.gather(*(run(p) for p in active)):
            items.extend(provider_items)
            errors.extend(provider_errors)
        return deduplicate_resources(items), errors

    def infos(self) -> list[dict[str, Any]]:
        return [provider.info() for provider in self.providers]


def _clean(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def resource_fingerprint(item: ResourceItem) -> str:
    infohash = magnet_infohash(item.magnet or item.share_url)
    if infohash:
        raw = f"btih:{infohash}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    strong = item.share_url.strip().lower() or item.url.strip().lower() or item.torrent_url.strip().lower()
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
            sources = seen[key].provider_sources or [seen[key].provider_name]
            if item.provider_name not in sources:
                sources.append(item.provider_name)
            seen[key].provider_sources = sources
            continue
        item.provider_sources = item.provider_sources or [item.provider_name]
        seen[key] = item
        result.append(item)
    return result


def filter_resources(items: list[ResourceItem], provider: str = "", season: str = "", resolution: str = "", quality: str = "", language: str = "", source_type: str = "") -> list[ResourceItem]:
    filters = {"provider": provider, "season": season, "resolution": resolution, "quality": quality, "language": language, "source_type": source_type}
    return [item for item in items if all(not value or _clean(str(getattr(item, key))) == _clean(value) for key, value in filters.items())]


def filter_options(items: list[ResourceItem]) -> dict[str, list[str]]:
    return {key: sorted({str(getattr(item, key)) for item in items if getattr(item, key)}) for key in ("provider", "uploader", "season", "resolution", "quality", "language", "source_type", "official_label", "fee_label")}


def magnet_infohash(value: str) -> str:
    match = re.search(r"(?:magnet:\?[^\s]*?xt=urn:btih:)([a-z0-9]{32,40})", str(value or ""), re.I)
    return match.group(1).lower() if match else ""


def build_search_terms(title: str, year: str = "", media_type: str = "movie", season: str = "") -> list[str]:
    base = re.sub(r"\s+", " ", str(title or "")).strip()
    suffix = " ".join(x for x in (str(year or "").strip(), f"S{int(season):02d}" if str(season or "").isdigit() and media_type == "tv" else "") if x)
    return [" ".join(x for x in (base, suffix) if x).strip()] if base else []


def _size_text(value: Any) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if not size:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(size)
    idx = 0
    while number >= 1024 and idx < len(units) - 1:
        number /= 1024
        idx += 1
    return f"{number:.2f} {units[idx]}"


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def is_directly_transferable(link: str) -> bool:
    value = str(link or "").strip().lower()
    return value.startswith(("magnet:?", "ed2k://")) or bool(
        re.search(r"https?://(?:[^/]+\.)?(?:115\.com|115cdn\.com)/s/", value)
    )


class TorznabResourceProvider(ResourceProvider):
    """Prowlarr/Jackett Torznab adapter. No indexer-specific scraping."""

    capabilities = ("search", "magnet", "offline")

    def __init__(self, provider_id: str, name: str, base_url: str, api_key: str, year: str = "", season: str = "", timeout: float = 10):
        self.id, self.name = provider_id, name
        self.base_url, self.api_key = base_url.rstrip("/"), api_key
        self.year, self.season, self.timeout = year, season, timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def search(self, media_type: str, tmdb_id: int, title: str = "") -> list[ResourceItem]:
        terms = build_search_terms(title, self.year, media_type, self.season)
        if not terms:
            return []
        endpoint = f"{self.base_url}/api/v1/search" if self.id == "prowlarr" else f"{self.base_url}/api/v2.0/indexers/all/results"
        params = {"query" if self.id == "prowlarr" else "Query": terms[0], "type": "search", "apikey": self.api_key}
        headers = {"X-Api-Key": self.api_key} if self.id == "prowlarr" else {}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(endpoint, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("Results") or payload.get("results") or []
        return [self.normalize(row) for row in rows if isinstance(row, dict)]

    def normalize(self, raw: dict[str, Any]) -> ResourceItem:
        title = str(raw.get("title") or raw.get("Title") or "未命名 BT 资源")
        magnet = str(raw.get("magnetUrl") or raw.get("MagnetUri") or raw.get("magnet") or "")
        torrent = str(raw.get("downloadUrl") or raw.get("Link") or raw.get("Guid") or "")
        indexer = str(raw.get("indexer") or raw.get("Tracker") or raw.get("indexerName") or self.name)
        se = parse_season_episode(title)
        resolution = next((x.upper() for x in re.findall(r"(?:2160|1080|720)[Pp]|4K|8K", title)), "")
        quality = " / ".join(re.findall(r"WEB[- .]?DL|WEBRip|BluRay|REMUX|HDTV", title, re.I))
        item = ResourceItem(
            provider=self.id, provider_name=self.name, provider_resource_id=str(raw.get("guid") or raw.get("Guid") or magnet_infohash(magnet) or torrent),
            title=title, resource_title=title, media_title="", url=torrent, share_url=magnet or torrent,
            magnet=magnet, torrent_url=torrent, indexer=indexer, size=_size_text(raw.get("size") or raw.get("Size")),
            seeders=_safe_int(raw.get("seeders") or raw.get("Seeders")), leechers=_safe_int(raw.get("leechers") or raw.get("Peers")),
            publish_time=str(raw.get("publishDate") or raw.get("PublishDate") or raw.get("pubDate") or ""),
            category=str(raw.get("category") or raw.get("CategoryDesc") or "BT"), resolution=resolution, quality=quality,
            source_type="BT / 磁力", transfer_supported=bool(magnet), season_number=int(se.get("season_number") or 0),
            episode_start=int(se.get("episode_start") or 0), episode_end=int(se.get("episode_end") or 0),
            episode_count=int(se.get("episode_count") or 0), is_complete=bool(se.get("is_complete")),
            season_episode_label=build_season_episode_label(se), resource_tags=[indexer],
        )
        return item


class GenericJSONResourceProvider(ResourceProvider):
    """Opt-in adapter for public JSON aggregation APIs; never scrapes protected pages."""

    capabilities = ("search", "json-api")

    def __init__(self, provider_id: str, name: str, base_url: str, api_key: str = "", timeout: float = 8):
        self.id, self.name, self.base_url, self.api_key, self.timeout = provider_id, name, base_url.strip(), api_key, timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def search(self, media_type: str, tmdb_id: int, title: str = "") -> list[ResourceItem]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(self.base_url, params={"q": title, "type": media_type}, headers=headers)
            response.raise_for_status()
            payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("items") or payload.get("results") or payload.get("data") or []
        result: list[ResourceItem] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            link = str(raw.get("share_url") or raw.get("url") or raw.get("link") or "")
            title_text = str(raw.get("title") or raw.get("name") or "未命名资源")
            se = parse_season_episode(title_text)
            result.append(ResourceItem(provider=self.id, provider_name=self.name, provider_resource_id=str(raw.get("id") or link), title=title_text,
                resource_title=title_text, url=link, share_url=link, source_type=str(raw.get("pan_type") or raw.get("source_type") or "第三方聚合"),
                size=str(raw.get("size") or ""), resolution=str(raw.get("resolution") or ""), quality=str(raw.get("quality") or ""),
                uploader=str(raw.get("uploader") or raw.get("author") or ""), publish_time=str(raw.get("publish_time") or raw.get("created_at") or ""),
                transfer_supported=is_directly_transferable(link), season_episode_label=build_season_episode_label(se)))
        return result

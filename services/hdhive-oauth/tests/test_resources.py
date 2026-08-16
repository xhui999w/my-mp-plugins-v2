import asyncio
import unittest

from app.resources import (
    HDHiveResourceProvider,
    ResourceItem,
    ResourceProvider,
    ResourceProviderRegistry,
    deduplicate_resources,
    filter_options,
    filter_resources,
)


class FailingProvider(ResourceProvider):
    id = "broken"
    name = "故障来源"

    async def search(self, media_type, tmdb_id, title=""):
        raise RuntimeError("upstream unavailable")


class ResourceProviderTests(unittest.TestCase):
    def test_hdhive_normalization_real_api_fields(self):
        item = HDHiveResourceProvider.normalize({
            "slug": "abc", "title": "电影 2160P", "resolution": "4K", "share_size": "20 GB",
            "tags": "HDR,中字", "pan_type": "115",
            "user": {"id": 1, "nickname": "分享者", "avatar_url": "https://img.example/u.jpg"},
            "unlock_points": 2, "unlocked_users_count": 9, "is_unlocked": False,
        })
        self.assertEqual(item.provider, "hdhive")
        self.assertEqual(item.provider_resource_id, "abc")
        self.assertTrue(item.transfer_supported)
        self.assertEqual(item.resource_tags, ["HDR", "中字"])
        self.assertEqual(item.uploader, "分享者")
        self.assertEqual(item.uploader_avatar, "https://img.example/u.jpg")
        self.assertEqual(item.size, "20 GB")
        self.assertEqual(item.points, 2)
        self.assertEqual(item.unlock_count, 9)
        self.assertFalse(item.is_unlocked)
        self.assertEqual(item.source_type, "115网盘")

    def test_hdhive_normalization_non_115_source_is_not_transfer_supported(self):
        item = HDHiveResourceProvider.normalize({"slug": "abc", "title": "电影", "pan_type": "quark", "unlock_points": 0})
        self.assertFalse(item.transfer_supported)
        self.assertEqual(item.source_type, "夸克网盘")

    def test_hdhive_normalization_unlocked_flag_and_ed2k_offline(self):
        item = HDHiveResourceProvider.normalize({"slug": "abc", "title": "电影", "pan_type": "115", "unlock_points": 2, "is_unlocked": True})
        self.assertTrue(item.is_unlocked)
        self.assertTrue(item.transfer_supported)
        self.assertEqual(item.points, 2)
        magnet = HDHiveResourceProvider.normalize({"slug": "m1", "title": "磁力", "pan_type": "magnet"})
        self.assertTrue(magnet.transfer_supported)

    def test_deduplication_keeps_different_versions(self):
        same1 = ResourceItem("a", "A", "1", "影片", share_url="https://115.com/s/one", resolution="4K")
        same2 = ResourceItem("b", "B", "2", "影片", share_url="https://115.com/s/one", resolution="4K")
        other = ResourceItem("a", "A", "3", "影片", size="5GB", resolution="1080P")
        result = deduplicate_resources([same1, same2, other])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].duplicate_count, 2)

    def test_filtering_and_options(self):
        items = [ResourceItem("a", "A", "1", "一", resolution="4K", season="S01", uploader="甲"), ResourceItem("b", "B", "2", "二", resolution="1080P", season="S02", uploader="乙")]
        self.assertEqual(len(filter_resources(items, resolution="4K")), 1)
        self.assertEqual(filter_options(items)["season"], ["S01", "S02"])
        self.assertEqual(filter_options(items)["uploader"], ["乙", "甲"])

    def test_provider_failure_is_isolated(self):
        async def query(media_type, tmdb_id, title):
            return {"items": [{"slug": "ok", "title": "正常资源"}]}

        registry = ResourceProviderRegistry([FailingProvider(), HDHiveResourceProvider(query)])
        items, errors = asyncio.run(registry.search("movie", 1, "测试"))
        self.assertEqual(len(items), 1)
        self.assertEqual(errors[0]["provider"], "broken")


    def test_normalize_cleans_python_list_quality_and_pan_type(self):
        item = HDHiveResourceProvider.normalize({
            "slug": "abc", "title": "电影", "quality": ["蓝光原盘/REMUX", "4K"],
            "pan_type": "123云盘", "actual_unlock_points": 3, "media": {"season": 2, "episode": 5},
        })
        self.assertEqual(item.quality, "蓝光原盘/REMUX / 4K")
        self.assertEqual(item.source_type, "123云盘")
        self.assertEqual(item.points, 3)
        self.assertEqual(item.season, "2")
        self.assertEqual(item.episode, "5")

    def test_normalize_parses_quoted_list_string(self):
        item = HDHiveResourceProvider.normalize({"slug": "x", "title": "影片", "quality": "['蓝光原盘/ISO']", "unlock_points": 0, "is_free_for_user": True})
        self.assertEqual(item.quality, "蓝光原盘/ISO")
        self.assertEqual(item.points, 0)
        self.assertFalse(item.is_unlocked)

    def test_provider_search_surfaces_query_errors(self):
        async def query(media_type, tmdb_id, title):
            return {"items": []}, [{"provider": "hdhive", "error": "标题搜索失败：HDHive returned HTTP 404"}]

        provider = HDHiveResourceProvider(query)
        items, errors = asyncio.run(provider.search("movie", 0, "测试"))
        self.assertEqual(items, [])
        self.assertEqual(errors[0]["error"], "标题搜索失败：HDHive returned HTTP 404")


if __name__ == "__main__":
    unittest.main()

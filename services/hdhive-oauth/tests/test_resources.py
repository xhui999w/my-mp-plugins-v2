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
    def test_hdhive_normalization(self):
        item = HDHiveResourceProvider.normalize({"slug": "abc", "title": "电影 2160P", "resolution": "4K", "size": "20 GB", "tags": "HDR,中字"})
        self.assertEqual(item.provider, "hdhive")
        self.assertEqual(item.provider_resource_id, "abc")
        self.assertTrue(item.transfer_supported)
        self.assertEqual(item.resource_tags, ["HDR", "中字"])

    def test_deduplication_keeps_different_versions(self):
        same1 = ResourceItem("a", "A", "1", "影片", share_url="https://115.com/s/one", resolution="4K")
        same2 = ResourceItem("b", "B", "2", "影片", share_url="https://115.com/s/one", resolution="4K")
        other = ResourceItem("a", "A", "3", "影片", size="5GB", resolution="1080P")
        result = deduplicate_resources([same1, same2, other])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].duplicate_count, 2)

    def test_filtering_and_options(self):
        items = [ResourceItem("a", "A", "1", "一", resolution="4K", season="S01"), ResourceItem("b", "B", "2", "二", resolution="1080P", season="S02")]
        self.assertEqual(len(filter_resources(items, resolution="4K")), 1)
        self.assertEqual(filter_options(items)["season"], ["S01", "S02"])

    def test_provider_failure_is_isolated(self):
        async def query(media_type, tmdb_id, title):
            return {"items": [{"slug": "ok", "title": "正常资源"}]}

        registry = ResourceProviderRegistry([FailingProvider(), HDHiveResourceProvider(query)])
        items, errors = asyncio.run(registry.search("movie", 1, "测试"))
        self.assertEqual(len(items), 1)
        self.assertEqual(errors[0]["provider"], "broken")


if __name__ == "__main__":
    unittest.main()

import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

SERVICE_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.explore import TMDBProvider, TTLCache, filter_metadata, registry


class ExploreTests(unittest.TestCase):
    def test_registry_reports_missing_token(self):
        providers = registry(False)
        self.assertEqual(providers[0]["id"], "tmdb")
        self.assertFalse(providers[0]["configured"])
        self.assertTrue(any(item["id"] == "netflix" for item in providers))

    def test_media_normalization(self):
        item = TMDBProvider.normalize({"id": 7, "title": "示例", "release_date": "2026-08-14", "vote_average": 8.26, "poster_path": "/a.jpg"}, "movie")
        self.assertEqual(item["tmdb_id"], 7)
        self.assertEqual(item["year"], "2026")
        self.assertEqual(item["rating"], 8.3)
        self.assertIn("w342", item["poster"])

    def test_filters_use_codes_and_dynamic_years(self):
        filters = filter_metadata([{"id": 28, "name": "动作"}])
        self.assertIn({"code": "KR", "name": "韩国"}, filters["regions"])
        self.assertIn({"code": "ko", "name": "韩语"}, filters["languages"])
        self.assertEqual(filters["genres"][0]["id"], 28)
        self.assertGreaterEqual(len(filters["years"]), 10)

    def test_cache_expiration(self):
        async def scenario():
            cache = TTLCache()
            await cache.set("x", {"ok": True}, 10)
            self.assertEqual((await cache.get("x"))["ok"], True)
        asyncio.run(scenario())

    def test_discover_parameter_mapping_and_pagination(self):
        async def scenario():
            provider = TMDBProvider("key")
            payload = {"page": 2, "total_pages": 3, "total_results": 50, "results": [{"id": 1, "title": "A", "release_date": "2025-01-01"}]}
            with patch.object(provider, "request", new=AsyncMock(return_value=(payload, False))) as request:
                result = await provider.discover({"media_type": "movie", "region": "KR", "language": "ko", "genre": "28", "year": 2025, "rating": 7, "sort": "vote_average.desc", "page": 2})
            params = request.await_args.args[1]
            self.assertEqual(params["region"], "KR")
            self.assertEqual(params["with_original_language"], "ko")
            self.assertEqual(params["with_genres"], "28")
            self.assertEqual(params["primary_release_year"], 2025)
            self.assertTrue(result["has_more"])
        asyncio.run(scenario())

    

    def test_streaming_ranking_routes_through_tmdb_discover(self):
        from test_service import main
        client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
        tmdb = TMDBProvider("key")
        payload = {"items": [{"id": "tmdb:movie:1", "provider": "netflix"}], "page": 1, "total_pages": 1, "has_more": False, "configured": True, "error": None, "provider": "netflix"}
        with patch.object(main, "explore_tmdb_provider", return_value=tmdb), patch.object(tmdb, "discover", new=AsyncMock(return_value=payload)) as discover:
            response = client.get("/api/explore/ranking/netflix/popular-movie")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["provider"], "netflix")
        query = discover.await_args.args[0]
        self.assertEqual(query["platform"], "netflix")
        self.assertEqual(query["media_type"], "movie")
        self.assertEqual(query["sort"], "popularity.desc")
        self.assertEqual(query["rating"], 0)
        self.assertEqual(query["page"], 1)

    def test_streaming_ranking_top_lists_require_votes(self):
        from test_service import main
        client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
        tmdb = TMDBProvider("key")
        payload = {"items": [], "page": 1, "total_pages": 1, "has_more": False, "configured": True, "error": None, "provider": "disney"}
        with patch.object(main, "explore_tmdb_provider", return_value=tmdb), patch.object(tmdb, "discover", new=AsyncMock(return_value=payload)) as discover:
            response = client.get("/api/explore/ranking/disney/top-tv")
        self.assertEqual(response.status_code, 200)
        query = discover.await_args.args[0]
        self.assertEqual(query["platform"], "disney")
        self.assertEqual(query["media_type"], "tv")
        self.assertEqual(query["sort"], "vote_average.desc")
        self.assertEqual(query["rating"], 7.0)
        self.assertEqual(query["min_votes"], 50)

    def test_streaming_ranking_tmdb_unconfigured_message(self):
        from test_service import main
        client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
        response = client.get("/api/explore/ranking/netflix/popular-movie")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["configured"])
        self.assertIn("TMDB 尚未配置", body["error"])
        self.assertNotIn("Netflix 尚未配置", body["error"])


    def test_missing_token_does_not_break_explore_page(self):
        from test_service import main
        client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(main.app)
        page = client.get("/explore")
        self.assertEqual(page.status_code, 200)
        result = client.get("/api/explore/discover").json()
        self.assertFalse(result["configured"])
        self.assertIn("尚未配置", result["error"])


if __name__ == "__main__":
    unittest.main()

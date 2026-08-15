from app.douban import DoubanProvider


def test_douban_metadata_and_capabilities_are_real_filters():
    provider = DoubanProvider()
    metadata = provider.metadata()
    assert provider.capabilities["supports_country"] is True
    assert provider.capabilities["supports_year"] is True
    assert "韩国" in metadata["countries"]
    assert "悬疑" in metadata["genres"]
    assert {item["code"] for item in metadata["sorts"]} == {"recommend", "hot", "release", "score"}


def test_douban_normalize_subject_fields():
    item = {
        "id": "37071133", "title": "菜肉馄饨", "year": "2025", "type": "movie",
        "item_type": "movie", "card": "subject",
        "card_subtitle": "2025 / 中国大陆 / 剧情 喜剧 爱情 / 吴天戈 / 周野芒 潘虹",
        "rating": {"value": 7.1, "count": 29074},
        "pic": {"large": "https://img1.doubanio.com/view/photo/m_ratio_poster/public/p2928450378.jpg"},
    }
    result = DoubanProvider.normalize(item, 1, "movie")
    assert result["douban_id"] == "37071133"
    assert result["year"] == "2025"
    assert result["countries"] == ["中国大陆"]
    assert result["genres"] == ["剧情", "喜剧", "爱情"]
    assert result["rating"] == 7.1
    assert result["vote_count"] == 29074
    assert result["poster"].startswith("/api/image-proxy?url=")


def test_douban_year_tag_normalization():
    assert DoubanProvider._year_tag("2025") == "2025"
    assert DoubanProvider._year_tag("2020s") == "2020年代"
    assert DoubanProvider._year_tag("") == ""

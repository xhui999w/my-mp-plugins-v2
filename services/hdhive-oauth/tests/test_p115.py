import unittest

from app.p115 import P115Client, P115Error


class P115Tests(unittest.TestCase):
    def test_share_parts(self):
        code, password = P115Client.share_parts("https://115.com/s/abc123?password=z9x8")
        self.assertEqual((code, password), ("abc123", "z9x8"))

    def test_share_parts_supports_115cdn_domain(self):
        code, password = P115Client.share_parts("https://115cdn.com/s/sws6ah73zh3?password=nono")
        self.assertEqual((code, password), ("sws6ah73zh3", "nono"))
        code2, password2 = P115Client.share_parts("https://115cdn.com/s/sws6ah73zh3?code=nono")
        self.assertEqual((code2, password2), ("sws6ah73zh3", "nono"))

    def test_rejects_advertising_and_invalid_offline_links(self):
        with self.assertRaises(P115Error):
            P115Client.share_parts("https://ads.example.com/click")


if __name__ == "__main__":
    unittest.main()

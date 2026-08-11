import importlib.util
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "plugins.v2"
    / "tg115transfer"
    / "hdhive_gateway.py"
)
SPEC = importlib.util.spec_from_file_location(
    "tg115transfer_hdhive_gateway", MODULE_PATH
)
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


class HDHiveGatewayTests(unittest.TestCase):
    def setUp(self):
        self.client = gateway.HDHiveGatewayClient(
            "https://hdhive.example.com", "installation_123", "secret-key"
        )

    def test_authorize_url_is_signed_and_does_not_expose_key(self):
        url = self.client.build_authorize_url()
        self.assertIn("/oauth/start?", url)
        self.assertIn("installation_id=installation_123", url)
        self.assertIn("signature=", url)
        self.assertNotIn("secret-key", url)

    @patch.object(gateway.requests, "request")
    def test_query_resources_normalizes_items(self, request):
        response = Mock(status_code=200)
        response.json.return_value = {"items": [{"slug": "one"}]}
        request.return_value = response
        items = self.client.query_resources("movie", 123)
        self.assertEqual(items[0]["slug"], "one")
        self.assertEqual(
            request.call_args.kwargs["headers"]["X-Installation-ID"],
            "installation_123",
        )

    @patch.object(gateway.requests, "request")
    def test_gateway_error_is_safe(self, request):
        response = Mock(status_code=401)
        response.json.return_value = {"detail": "not authorized"}
        request.return_value = response
        with self.assertRaises(gateway.HDHiveGatewayError):
            self.client.status()


if __name__ == "__main__":
    unittest.main()

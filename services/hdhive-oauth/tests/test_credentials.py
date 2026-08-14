import unittest
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

from app.credentials import AUTHORIZATION_PROVIDERS, AuthorizationProvider, AuthorizationProviderRegistry


class CredentialProviderTests(unittest.TestCase):
    def test_registry_is_extensible(self):
        registry = AuthorizationProviderRegistry()
        registry.register(AuthorizationProvider("example", "示例", "新资源站", "token", ("search",)))
        self.assertEqual(registry.get("example").name, "示例")
        self.assertEqual(registry.infos()[0]["capabilities"], ["search"])

    def test_builtin_providers(self):
        self.assertIsNotNone(AUTHORIZATION_PROVIDERS.get("hdhive"))
        self.assertIsNotNone(AUTHORIZATION_PROVIDERS.get("p115"))


if __name__ == "__main__":
    unittest.main()

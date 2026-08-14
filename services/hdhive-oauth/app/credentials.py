"""Extensible credential/authorization provider metadata registry."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuthorizationProvider:
    id: str
    name: str
    description: str
    auth_type: str
    capabilities: tuple[str, ...]

    def info(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        return data


class AuthorizationProviderRegistry:
    def __init__(self, providers: list[AuthorizationProvider] | None = None):
        self._providers = {provider.id: provider for provider in (providers or [])}

    def register(self, provider: AuthorizationProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, provider_id: str) -> AuthorizationProvider | None:
        return self._providers.get(provider_id)

    def infos(self) -> list[dict[str, Any]]:
        return [provider.info() for provider in self._providers.values()]


AUTHORIZATION_PROVIDERS = AuthorizationProviderRegistry([
    AuthorizationProvider("hdhive", "影巢", "资源查询、解锁和115分享", "oauth2", ("search", "unlock", "transfer")),
    AuthorizationProvider("p115", "115 网盘", "转存与离线下载", "cookie_or_qr", ("transfer", "offline")),
    AuthorizationProvider("emby", "Emby", "媒体库匹配", "api_key", ("library", "users")),
    AuthorizationProvider("tmdb", "TMDB", "影视元数据、榜单和海报", "token", ("metadata", "discover", "rankings")),
    AuthorizationProvider("douban", "??", "???????", "optional_cookie", ("rankings",)),
    AuthorizationProvider("netflix", "Netflix", "Netflix?????", "token", ("rankings",)),
    AuthorizationProvider("max", "HBO Max", "HBO Max?????", "token", ("rankings",)),
    AuthorizationProvider("prime", "Prime Video", "Prime Video?????", "token", ("rankings",)),
    AuthorizationProvider("disney", "Disney+", "Disney+?????", "token", ("rankings",)),
    AuthorizationProvider("apple", "Apple TV+", "Apple TV+?????", "token", ("rankings",)),
])

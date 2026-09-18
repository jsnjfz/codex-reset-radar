"""数据源适配器。

业务层只依赖 `base.FetchPage`，不直接依赖第三方 JSON 结构（方案 §5.3 / §15）。
"""

from __future__ import annotations

from typing import Any, Dict

from ..httpclient import HttpClient
from .base import FetchPage, Provider
from .fxtwitter_json import FxTwitterJsonProvider
from .fxtwitter_rss import FxTwitterRssProvider

__all__ = [
    "FetchPage",
    "Provider",
    "FxTwitterJsonProvider",
    "FxTwitterRssProvider",
    "build_provider",
]

_REGISTRY = {
    "fxtwitter_json": FxTwitterJsonProvider,
    "fxtwitter_rss": FxTwitterRssProvider,
}


def build_provider(name: str, client: HttpClient, source_config: Dict[str, Any]) -> Provider:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise ValueError("未知 provider：%r" % name)
    return factory(client, source_config)

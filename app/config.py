from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.toml"


@dataclass(frozen=True)
class ApiKeySettings:
    amap_api_key: str | None = None
    amap_web_key: str | None = None
    ors_api_key: str | None = None
    dashscope_api_key: str | None = None
    bailian_api_key: str | None = None
    amadeus_client_id: str | None = None
    amadeus_client_secret: str | None = None


@dataclass(frozen=True)
class BailianSettings:
    model: str = "qwen3.7-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class DashscopeSettings:
    base_http_api_url: str = "https://dashscope.aliyuncs.com/api/v1"


@dataclass(frozen=True)
class AppSettings:
    api_keys: ApiKeySettings
    bailian: BailianSettings
    dashscope: DashscopeSettings
    config_path: Path


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return load_settings(DEFAULT_CONFIG_PATH)


def load_settings(config_path: Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    raw = _read_toml(config_path)
    api_keys = raw.get("api_keys", {})
    bailian = raw.get("bailian", {})
    dashscope = raw.get("dashscope", {})

    return AppSettings(
        api_keys=ApiKeySettings(
            amap_api_key=_first_value(os.getenv("AMAP_API_KEY"), os.getenv("AMAP_WEB_KEY"), api_keys.get("amap_api_key")),
            amap_web_key=_first_value(os.getenv("AMAP_WEB_KEY"), os.getenv("AMAP_API_KEY"), api_keys.get("amap_web_key")),
            ors_api_key=_first_value(os.getenv("ORS_API_KEY"), api_keys.get("ors_api_key")),
            dashscope_api_key=_first_value(
                os.getenv("DASHSCOPE_API_KEY"),
                api_keys.get("dashscope_api_key"),
            ),
            bailian_api_key=_first_value(os.getenv("BAILIAN_API_KEY"), api_keys.get("bailian_api_key")),
            amadeus_client_id=_first_value(os.getenv("AMADEUS_CLIENT_ID"), api_keys.get("amadeus_client_id")),
            amadeus_client_secret=_first_value(
                os.getenv("AMADEUS_CLIENT_SECRET"),
                api_keys.get("amadeus_client_secret"),
            ),
        ),
        bailian=BailianSettings(
            model=_first_value(os.getenv("BAILIAN_MODEL"), bailian.get("model")) or "qwen3.7-plus",
            base_url=(
                _first_value(os.getenv("BAILIAN_BASE_URL"), bailian.get("base_url"))
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ).rstrip("/"),
        ),
        dashscope=DashscopeSettings(
            base_http_api_url=(
                _first_value(
                    os.getenv("DASHSCOPE_BASE_HTTP_API_URL"),
                    dashscope.get("base_http_api_url"),
                )
                or "https://dashscope.aliyuncs.com/api/v1"
            ).rstrip("/"),
        ),
        config_path=config_path,
    )


def _read_toml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as file:
        data = tomllib.load(file)
    return data if isinstance(data, dict) else {}


def _first_value(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return None

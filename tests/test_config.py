from pathlib import Path

from app.config import load_settings


def test_load_settings_reads_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("ORS_API_KEY", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("BAILIAN_MODEL", raising=False)
    monkeypatch.delenv("BAILIAN_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_BASE_HTTP_API_URL", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("JUHE_MCP_TOKEN", raising=False)
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        """
        [api_keys]
        amap_api_key = "amap-from-file"
        ors_api_key = "ors-from-file"
        dashscope_api_key = "dashscope-from-file"
        serpapi_api_key = "serpapi-from-file"

        [bailian]
        model = "qwen-test"
        base_url = "https://example.com/compatible-mode/v1/"

        [dashscope]
        base_http_api_url = "https://example.com/api/v1/"
        """,
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.api_keys.amap_api_key == "amap-from-file"
    assert settings.api_keys.ors_api_key == "ors-from-file"
    assert settings.api_keys.dashscope_api_key == "dashscope-from-file"
    assert settings.api_keys.serpapi_api_key == "serpapi-from-file"
    assert settings.bailian.model == "qwen-test"
    assert settings.bailian.base_url == "https://example.com/compatible-mode/v1"
    assert settings.dashscope.base_http_api_url == "https://example.com/api/v1"


def test_environment_variables_override_config_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMAP_API_KEY", "amap-from-env")
    monkeypatch.setenv("BAILIAN_MODEL", "qwen-from-env")
    monkeypatch.setenv("SERPAPI_API_KEY", "serpapi-from-env")
    monkeypatch.setenv("JUHE_MCP_TOKEN", "juhe-from-env")
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        """
        [api_keys]
        amap_api_key = "amap-from-file"
        serpapi_api_key = "serpapi-from-file"

        [bailian]
        model = "qwen-from-file"
        """,
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.api_keys.amap_api_key == "amap-from-env"
    assert settings.api_keys.serpapi_api_key == "serpapi-from-env"
    assert settings.api_keys.juhe_mcp_token == "juhe-from-env"
    assert settings.bailian.model == "qwen-from-env"

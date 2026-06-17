from fastapi.testclient import TestClient

from app import main as main_module


def test_llm_health_endpoint_returns_provider_result(monkeypatch) -> None:
    class FakeProvider:
        model = "qwen-test"

        def test_connection(self):
            return {
                "ok": True,
                "model": self.model,
                "elapsed_ms": 12,
                "reply_preview": "OK",
            }

    monkeypatch.setattr(main_module, "BailianQwenGuideProvider", FakeProvider)

    response = TestClient(main_module.app).get("/api/v1/llm/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "model": "qwen-test",
        "elapsed_ms": 12,
        "reply_preview": "OK",
    }


def test_llm_health_endpoint_returns_public_error(monkeypatch) -> None:
    class FakeProvider:
        model = "qwen-test"

        def test_connection(self):
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

    monkeypatch.setattr(main_module, "BailianQwenGuideProvider", FakeProvider)

    response = TestClient(main_module.app).get("/api/v1/llm/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "model": "qwen-test",
        "error": "未配置 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY",
    }

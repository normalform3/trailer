import sys
import types

from app.providers.dashscope_multimodal import DashScopeMultiModalProvider


def test_dashscope_multimodal_describes_image(monkeypatch) -> None:
    calls = {}

    class FakeMultiModalConversation:
        @staticmethod
        def call(api_key, model, messages):
            calls["api_key"] = api_key
            calls["model"] = model
            calls["messages"] = messages
            return types.SimpleNamespace(
                status_code=200,
                output=types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            message=types.SimpleNamespace(
                                content=[{"text": "图中是一位女孩和一只狗在户外互动。"}]
                            )
                        )
                    ]
                ),
            )

    fake_dashscope = types.SimpleNamespace(
        base_http_api_url=None,
        MultiModalConversation=FakeMultiModalConversation,
    )
    monkeypatch.setitem(sys.modules, "dashscope", fake_dashscope)

    provider = DashScopeMultiModalProvider(api_key="test-key", model="qwen3.7-plus")
    text = provider.describe_image("https://example.com/image.jpeg")

    assert fake_dashscope.base_http_api_url == "https://dashscope.aliyuncs.com/api/v1"
    assert calls["api_key"] == "test-key"
    assert calls["model"] == "qwen3.7-plus"
    assert calls["messages"] == [
        {
            "role": "user",
            "content": [
                {"image": "https://example.com/image.jpeg"},
                {"text": "图中描绘的是什么景象?"},
            ],
        }
    ]
    assert text == "图中是一位女孩和一只狗在户外互动。"

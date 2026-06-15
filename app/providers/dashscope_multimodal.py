from __future__ import annotations

from typing import Any

from app.config import get_settings


class DashScopeMultiModalProvider:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_http_api_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = (
            api_key
            or settings.api_keys.dashscope_api_key
            or settings.api_keys.bailian_api_key
        )
        self.model = model or settings.bailian.model
        self.base_http_api_url = base_http_api_url or settings.dashscope.base_http_api_url

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def describe_image(self, image_url: str, question: str = "图中描绘的是什么景象?") -> str:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY or BAILIAN_API_KEY is not configured")

        try:
            import dashscope
        except ModuleNotFoundError as exc:
            raise RuntimeError("dashscope package is not installed; run `pip install -e .`") from exc

        dashscope.base_http_api_url = self.base_http_api_url
        response = dashscope.MultiModalConversation.call(
            api_key=self.api_key,
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"image": image_url},
                        {"text": question},
                    ],
                }
            ],
        )
        self._raise_for_dashscope_error(response)
        return self._extract_text(response)

    def _raise_for_dashscope_error(self, response: Any) -> None:
        status_code = self._read(response, "status_code")
        if status_code is None or int(status_code) == 200:
            return
        code = self._read(response, "code") or "unknown"
        message = self._read(response, "message") or "DashScope request failed"
        raise RuntimeError(f"DashScope request failed ({status_code}, {code}): {message}")

    def _extract_text(self, response: Any) -> str:
        try:
            output = self._read(response, "output")
            choices = self._read(output, "choices")
            message = self._read(choices[0], "message")
            content = self._read(message, "content")
            text = self._read(content[0], "text")
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("DashScope response did not include text content") from exc

        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("DashScope response text content is empty")
        return text.strip()

    def _read(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value[key]
        return getattr(value, key)

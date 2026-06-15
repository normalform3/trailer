from __future__ import annotations

from app.providers.dashscope_multimodal import DashScopeMultiModalProvider


IMAGE_URL = "https://help-static-aliyun-doc.aliyuncs.com/file-manage-files/zh-CN/20241022/emyrja/dog_and_girl.jpeg"


def main() -> None:
    provider = DashScopeMultiModalProvider()
    answer = provider.describe_image(IMAGE_URL, "图中描绘的是什么景象?")
    print(answer)


if __name__ == "__main__":
    main()

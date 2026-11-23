# =====================================================================
# vision.py —— 多模态视觉客户端（看图回答）
#
# 唯一用途：给「参考资产流水线」做定妆图质检（app/media/refs.py）。
# 走 OpenAI 兼容的 /chat/completions 多模态消息格式，任何支持视觉的
# 厂商都能接（DeepSeek 视觉 / 智谱 GLM-4V / Qwen-VL / GPT-4o ...）。
#
# 两个实测坑（见 docs/DEVELOPMENT.md 5.8）：
#   - 图片由服务端拉取，URL 必须当时可达；base64 data URI 更稳；
#   - 推理型视觉模型可能把答案写进 reasoning_content，要一并兼容。
# =====================================================================

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import ModelConfig
from .providers import get_provider_spec

log = logging.getLogger(__name__)


class VisionClient:
    """视觉问答客户端。``available`` 为 False 时应由调用方跳过质检。"""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.api_key and self.config.model)

    @property
    def label(self) -> str:
        return self.config.label or self.config.provider

    def ask(
        self,
        image_url: str | list[str],
        question: str,
        *,
        max_tokens: int = 600,
        timeout: float = 180.0,
    ) -> str:
        """让视觉模型看图回答问题，返回文本（兼容 content / reasoning_content）。"""
        if not self.available:
            raise RuntimeError("未配置视觉模型（VISION_API_KEY，或任一支持视觉的厂商 key）")

        image_urls = [image_url] if isinstance(image_url, str) else image_url
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    *[{"type": "image_url", "image_url": {"url": url}} for url in image_urls],
                ],
            }],
        }
        # 厂商特有请求体（DeepSeek 推理模型需显式关闭 thinking 才会直出 content）。
        spec = get_provider_spec(self.config.provider)
        if spec:
            body.update(spec.extra_body_for(self.config))
        for key, value in self.config.extra_body.items():
            if not key.startswith("_"):
                body[key] = value

        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.post(
                self.config.endpoint("chat/completions"),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        message = (data.get("choices") or [{}])[0].get("message", {})
        return str(message.get("content") or message.get("reasoning_content") or "").strip()

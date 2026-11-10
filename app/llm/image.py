# =====================================================================
# image.py —— 文生图客户端
#
# 用途：为「参考资产注册」生成角色定妆图 / 场景定妆图，作为视频生成的
# 图像级视觉锚（比纯文字描述强一个量级）。
#
# 与视频生成接口不同，文生图是**同步**的：POST /images/generations
# 直接返回 {"data":[{"url": ...}]}（智谱 CogView 实测）。
# 不同厂商的响应字段略有差异，由各客户端自行解析。
# =====================================================================

from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .base import ModelConfig

log = logging.getLogger(__name__)

DEFAULT_SIZE = "1024x1024"


class ImageClient(ABC):
    """文生图客户端抽象。"""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return bool(self.config.api_key)

    @property
    def label(self) -> str:
        return self.config.label or self.config.provider

    @abstractmethod
    def generate(self, prompt: str, *, size: str = DEFAULT_SIZE, timeout: float = 120.0,
                 negative_prompt: str = "") -> str:
        """生成一张图，返回图片地址（URL 或 data URI）。失败抛异常。

        negative_prompt：画风反向词（如二次元项目要排除「真人、写实照片」）。
        走厂商的结构化字段而不是塞进正文——提示词里写否定句会反噬
        （模型把「不要真人」听成「要真人」，见记忆 [[video-prompt-adherence-budget]]）。
        """
        ...

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            resp = client.post(
                self.config.endpoint(path),
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            return resp.json()


class CogViewImageClient(ImageClient):
    """智谱 CogView：POST /images/generations → data[0].url（同步）。"""

    def generate(self, prompt: str, *, size: str = DEFAULT_SIZE, timeout: float = 120.0,
                 negative_prompt: str = "") -> str:
        body: dict[str, Any] = {"model": self.config.model, "prompt": prompt, "size": size}
        if negative_prompt:
            body["negative_prompt"] = negative_prompt  # CogView 实测支持（2026-11-10）
        data = self._post("images/generations", body, timeout)
        items = data.get("data") or []
        url = items[0].get("url") if items else None
        if not url:
            raise RuntimeError(f"文生图未返回图片地址：{str(data)[:200]}")
        return str(url)


class OpenAIImageClient(ImageClient):
    """OpenAI Images：返回 url 或 b64_json，统一转成可直传的图片地址。"""

    def generate(self, prompt: str, *, size: str = DEFAULT_SIZE, timeout: float = 120.0,
                 negative_prompt: str = "") -> str:
        body: dict[str, Any] = {"model": self.config.model, "prompt": prompt, "size": size, "n": 1}
        if negative_prompt:
            body["negative_prompt"] = negative_prompt
        data = self._post("images/generations", body, timeout)
        items = data.get("data") or []
        if not items:
            raise RuntimeError(f"文生图未返回数据：{str(data)[:200]}")
        item = items[0]
        if item.get("url"):
            return str(item["url"])
        if item.get("b64_json"):
            # 转成 data URI：下游（视觉质检 / 视频模型）可直接消费。
            return f"data:image/png;base64,{item['b64_json']}"
        raise RuntimeError(f"文生图响应既无 url 也无 b64_json：{str(item)[:200]}")


_CLIENTS: dict[str, type[ImageClient]] = {
    "cogview": CogViewImageClient,
    "zhipu": CogViewImageClient,
    "openai": OpenAIImageClient,
}


def register_image_client(name: str, cls: type[ImageClient]) -> None:
    """注册自定义文生图客户端（扩展点）。"""
    _CLIENTS[name.strip().lower()] = cls


def build_image_client(config: ModelConfig | None) -> ImageClient | None:
    """按 ModelConfig.provider 选择客户端；未配置则返回 None。"""
    if config is None or not config.api_key:
        return None
    cls = _CLIENTS.get((config.provider or "").strip().lower())
    if cls is None:
        log.warning("未知的文生图厂商 %s，回退到 CogView 协议", config.provider)
        cls = CogViewImageClient
    return cls(config)


def data_uri(raw: bytes, mime: str = "image/png") -> str:
    """把二进制图片转成 data URI（服务端拉不到临时 URL 时的稳妥方案）。"""
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

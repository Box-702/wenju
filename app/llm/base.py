# =====================================================================
# base.py —— 模型接入层的数据契约
#
# 这里只放「描述性」类型，不放任何厂商细节：
#   - Modality：能力维度（对话 / 视觉 / 文生图），同一个 key 可以横跨多维；
#   - ModelConfig：一次调用所需的全部参数（厂商 + 模型 + 端点 + key）；
#   - ProviderSpec：某家厂商的静态声明（默认端点、默认模型、环境变量名）。
#
# 厂商细节在 providers.py，解析规则在 registry.py，统一门面在 client.py。
# =====================================================================

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Modality(StrEnum):
    """模型能力维度。"""

    CHAT = "chat"  # 文本对话 / 工具调用 / 结构化输出
    VISION = "vision"  # 多模态理解（看图回答，用于定妆图质检）
    IMAGE = "image"  # 文生图（参考资产）


@dataclass
class ModelConfig:
    """一次模型调用所需的完整参数（与具体 SDK 解耦）。"""

    provider: str  # 厂商标识，如 openai / deepseek / zhipu / ollama
    model: str
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.4
    extra_body: dict[str, Any] = field(default_factory=dict)
    label: str = ""  # 人类可读的厂商名，用于提示词与前端展示
    source: str = "env"  # 配置来源：env | db | default

    @property
    def configured(self) -> bool:
        return bool(self.model)

    def endpoint(self, path: str = "") -> str:
        """拼接完整端点，避免各处重复写 rstrip('/')。"""
        base = (self.base_url or "").rstrip("/")
        return f"{base}/{path.lstrip('/')}" if path else base


@dataclass(frozen=True)
class ProviderSpec:
    """某家模型厂商的静态声明。

    新增厂商 = 在 providers.py 里加一条声明（可选覆写 extra_body），
    不需要改动 client / registry / 上层业务代码。
    """

    name: str
    label: str
    default_base_url: str = ""
    default_model: str = ""
    env_key: str = ""  # .env 中的变量名，仅用于报错提示
    docs: str = ""  # 控制台地址，便于排查 key 问题
    requires_key: bool = True
    supports_vision: bool = False
    vision_model: str = ""  # 该厂商用于质检的视觉模型
    supports_image: bool = False
    image_model: str = ""  # 该厂商用于文生图的模型
    # 该厂商是否支持 response_format=json_object。
    # 不支持时（如 MiniMax，实测报 2013 unknown response_format type），
    # 结构化输出自动降级为「提示词要求 JSON + 容错解析」。
    supports_json_mode: bool = True
    # 内置厂商的「对话模型目录」：厂商 /models 接口不返全部或排序不友好时，
    # 用它兜底展示（BYOK 流程：选厂商 → 填 key → 发现 → 勾选）。
    # 发现成功时以厂商实时清单为准，这个目录只在发现失败/为空时顶上。
    chat_models: tuple[str, ...] = ()
    # 部分厂商（如 DeepSeek 推理模型）需要额外的请求体参数。
    extra_body: Callable[[ModelConfig], dict[str, Any]] | None = None

    def extra_body_for(self, config: ModelConfig) -> dict[str, Any]:
        return self.extra_body(config) if self.extra_body else {}

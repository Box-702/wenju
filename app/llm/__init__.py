# =====================================================================
# llm —— 统一大模型接入包
#
# 一个门面 + 一张厂商表，覆盖全部模型能力：
#   client.py   LLM 门面（chat / structured / vision / image）
#   base.py     ModelConfig / ProviderSpec 数据契约
#   providers.py 各厂商声明（OpenAI 兼容 / DeepSeek / 智谱 / Kimi / Qwen / Ollama）
#   registry.py 配置解析（.env 优先级链 + DB 偏好 → ModelConfig）
#   vision.py   多模态视觉客户端（定妆图质检）
#   image.py    文生图客户端（参考资产）
#
# 上层只写 `from ..llm import LLM`，不感知任何厂商细节。
# =====================================================================

# 只导出「包路径真的有人用」的名字——其余一律从子模块直接 import。
# 之前的 __init__ 把整个子模块的公开面都再导出了一遍（40+ 个名字），
# 其中绝大多数没人从包路径取过：两处写法并存，下游就得猜该写哪个，
# 而改口径时真正要改的那一处（子模块）反倒没人看。
from .client import LLM, build_llm
from .image import ImageClient
from .registry import (
    describe_providers,
    resolve_chat,
    resolve_image,
    resolve_vision,
)
from .vision import VisionClient

__all__ = [
    "LLM",
    "build_llm",
    "ImageClient",
    "VisionClient",
    "resolve_chat",
    "resolve_vision",
    "resolve_image",
    "describe_providers",
]

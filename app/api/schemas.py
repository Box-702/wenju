# =====================================================================
# schemas.py —— API 请求模型
#
# 只放「进站数据」的校验模型；出站一律用 dict（字段随前端演进，
# 不必为响应维护一套平行模型）。
# =====================================================================

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------- 项目 / 版本 ----------


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    raw_text: str = Field(min_length=1)
    adaptation_type: str = "short_drama"
    language: str = "zh-CN"


class GenerateRequest(BaseModel):
    adaptation_type: str | None = None
    language: str | None = None


class MilestoneSet(BaseModel):
    milestone: str | None = None  # draft | candidate | final | None（清除）


class ApplyEdit(BaseModel):
    """内置剧本编辑器的改动：一组字段级操作（set/add/remove，见 patch.PatchOp）。"""

    ops: list[dict] = Field(default_factory=list)


class SceneOrderSet(BaseModel):
    """场景拖拽排序：当前版本场景 id 的完整排列。"""

    scene_ids: list[str] = Field(min_length=1)


class NotesSet(BaseModel):
    notes: str = Field(default="")


# ---------- Agent 运行 ----------


class AgentRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    base_version_id: str | None = None
    scene_ids: list[str] = Field(default_factory=list)


class AcceptRequest(BaseModel):
    patch_indexes: list[int] | None = None


class ResumeRequest(BaseModel):
    """人类在中断处的结构化决策。action 见 agent/state.py 的 HumanDecision。"""

    action: str = "accept"  # accept | edit | regenerate | reject
    patch_indexes: list[int] | None = None
    patch: list[dict] | None = None  # edit 时人工修订的操作清单
    feedback: str | None = None      # regenerate 时给模型的反馈


# ---------- 对话 ----------


class AssetRef(BaseModel):
    """用户用 @ 引用的一份资产。

    kind: character=角色 / location=场景 / asset=成片或导入素材
    id:   资产库编号（char_xxx / loc_xxx / asset_xxx），Agent 靠它定位
    name: 显示名（用户输入框里写的就是它）
    """

    kind: str = ""
    id: str = ""
    name: str = ""


class ChatRequest(BaseModel):
    """对话式 Agent 的一轮输入。"""

    project_id: str | None = None   # 作品；缺省为「新建作品」的全局会话
    conversation_id: str | None = None  # 项目下的具体对话（线程）；缺省用项目默认对话
    message: str = Field(default="")  # 审阅动作（meta.intent=resume）时可为空
    meta: dict[str, Any] | None = None  # 结构化意图（如 {"intent": "resume", ...}）
    refs: list[AssetRef] = Field(default_factory=list)  # 用户用 @ 引用的资产
    # 本轮消息附带的图片 id（先经 /chat/images 上传拿 id）；进入上下文且随消息持久化。
    images: list[str] = Field(default_factory=list)


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=120)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


# ---------- 工作目录 ----------


class WorkspaceSet(BaseModel):
    root: str = Field(default="", min_length=0)
    persist: bool = Field(default=True)  # False = 仅应用内、不落盘文件


# ---------- Provider / 模型偏好 ----------


class ProviderCreate(BaseModel):
    kind: str = "video"
    name: str = Field(min_length=1)
    label: str = Field(min_length=1)
    base_url: str = ""
    api_key: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ProviderUpdate(BaseModel):
    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class ModelPreferenceSet(BaseModel):
    task_type: str = Field(min_length=1)
    provider_id: str | None = None
    # provider_id=null 表示清除界面指定、回落 .env，此时 model_name 允许为空
    model_name: str = Field(default="")
    params: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = True


# ---------- 视频 ----------


class VideoVersionCreate(BaseModel):
    shots: list[dict[str, Any]]
    style_guide: dict[str, Any] = Field(default_factory=dict)
    source_type: str = "manual"
    label: str | None = None
    notes: str | None = None
    parent_version_id: str | None = None


class VideoPromptReview(BaseModel):
    """保存单个镜头的 Prompt，并记录人工审阅决定。"""

    prompt: str = Field(min_length=1, max_length=8000)
    decision: Literal["save", "approve", "needs_revision"] = "save"
    note: str = Field(default="", max_length=2000)
    # 用户从参考素材里挑的参考视频（资产 id）。
    # None=本次不改动；列表=整体替换（空列表即清空），最多 3 条。
    reference_asset_ids: list[str] | None = Field(default=None, max_length=3)


class VideoPromptBulkReview(BaseModel):
    """对多个镜头应用同一个人工审阅决定。"""

    shot_ids: list[str] = Field(min_length=1)
    decision: Literal["approve", "needs_revision"] = "approve"
    note: str = Field(default="", max_length=2000)


class VideoJobCreate(BaseModel):
    """视频生成任务。

    除文本 prompt 外，开放多模态输入入口（对应 MiniMax H3 等
    多模态模型的 content 数组）：首帧图 / 尾帧图 / 参考图 /
    参考音频，以及原生 content 数组直通。

    参考视频**不在这里传**：它由镜头自己带的「参考素材」决定
    （见 app/api/video.py 的 _auto_references）。对外不再留
    「手填一条公网 URL 当参考视频」的口子——服务商只认 http(s) 或
    data:base64，而本机回放地址它拉不到（见 app/media/inline.py）。
    """

    shot_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt: str = Field(default="", max_length=8000)  # 纯图 / 视频驱动时可为空
    params: dict[str, Any] = Field(default_factory=dict)
    version_id: str | None = None
    # ---- 多模态入口 ----
    first_frame_image: str | None = None       # 首帧图 URL（图生视频）
    last_frame_image: str | None = None        # 尾帧图（首尾帧控制）
    reference_images: list[str] = Field(default_factory=list)  # 参考图（≤9）
    reference_audios: list[str] = Field(default_factory=list)  # 参考音频（≤3）
    content: list[dict[str, Any]] | None = None  # 原生多模态数组直通（优先级最高）

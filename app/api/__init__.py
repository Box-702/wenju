# =====================================================================
# api —— REST 路由包
#
# 拆分原则：按业务域分文件，全部挂在同一个 `/api` 前缀下，路由声明
# 与拆分前完全一致（顺序无关，路径无冲突）。
#
#   system.py      运行状态 / 后台任务 / 工作目录
#   projects.py    项目生命周期、初稿生成、导入、落盘、笔记、文件浏览
#   versions.py    剧本版本（含导出）
#   chat.py        对话式 Agent 与会话管理（改编审阅也走这里，见 D-59）
#   video.py       Provider / 模型偏好 / 视频版本 / 生成任务
#   cast.py        资产库（角色形象 / 声音 / 图片 / 场景）与试听
#   blockout.py    白模施工单与渲染状态（D-54）
#   memory.py      记忆管理（列表 / 启停 / 删除）
#   production.py  生产工作流（小说 → 提示词 整条流水线的启动与状态）
#
#   common.py      共享的序列化与落盘辅助
#   schemas.py     请求模型
#   deps.py        依赖入口（测试可整体替换）
# =====================================================================

from __future__ import annotations

from fastapi import APIRouter

from . import assets, blockout, cast, chat, memory, production, projects, system, versions, video

router = APIRouter(prefix="/api")

for _sub in (
    system, projects, versions, chat, video, assets, cast,
    blockout, memory, production,
):
    router.include_router(_sub.router)

__all__ = ["router"]

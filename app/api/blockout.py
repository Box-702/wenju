# =====================================================================
# blockout.py —— 白模施工单与渲染状态 API（D-54）
#
# 白模能力此前只存在于对话里（blockout_sheet_save / blockout_build /
# blockout_check 三个工具）：Agent 起草、用户对话调整、后台渲染——但界面
# 上什么都看不到，用户不知道施工单长什么样、渲染有没有跑完、片子在哪。
# 这里把状态摊开，界面上的「白模」面板直接读它。
# =====================================================================

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from ..media.blockout import blockout_dir, get_blockout_manager
from . import common

router = APIRouter()


@router.get("/projects/{project_id}/blockout")
def get_blockout(project_id: str) -> dict[str, Any]:
    """白模施工单原文 + 渲染状态 + 产物。"""
    common.get_project(project_id)
    d = blockout_dir(project_id)
    sheet_path = d / "施工单.md"
    sheet_text = ""
    if sheet_path.exists():
        try:
            sheet_text = sheet_path.read_text(encoding="utf-8")
        except OSError:
            sheet_text = ""
    rec = get_blockout_manager().get(project_id) or {}
    result = rec.get("result") or {}
    return {
        "project_id": project_id,
        "has_sheet": bool(sheet_text),
        "sheet": sheet_text,
        "render": {
            # 渲染状态是**进程内**的（BlockoutManager 是内存单例，重启即作废），
            # 所以重启后回到 idle 是正常的——产物本身在资产库里，不受影响。
            "status": rec.get("status") or "idle",
            "messages": (rec.get("messages") or [])[-4:],
            "error": rec.get("error"),
            "asset_id": result.get("asset_id"),
            "video_url": result.get("video_url"),
            "duration_sec": result.get("duration_sec"),
        },
    }

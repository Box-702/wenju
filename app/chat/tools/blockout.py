# =====================================================================
# blockout.py —— 白模施工域：起草/调整施工单 → 后台渲染 → 查结果（D-54）
#
# 「文字是资产、成品是衍生物」：用户调整的唯一入口是施工单（对话里说，
# Agent 改单重存），渲染出的 mp4 只是衍生物——改完单子重渲一次就是新版。
# 不做任何 3D 视口/预览 UI；构建是确定性代码（app/media/blockout.py），
# 本机 Blender 渲染、零花费，所以不走 D-43 审批闸门（那是给花钱动作的）。
# =====================================================================
from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from .context import ToolContext


def build_blockout_tools(ctx: ToolContext) -> list[Any]:
    @tool
    def blockout_sheet_save(project_id: str, content: str, note: str = "") -> str:
        """起草或更新白模施工单（镜头表 + 对象清单 + 动画节拍）。用户想做白模 /
        分镜预演，或对施工单提出任何调整（改镜头 / 加对象 / 移相机 / 改动画）时
        调用：按 blockout 技能里的格式写**全文**（不是增量补丁），保存前严格校验，
        有问题会逐条返回且不落盘。参数：project_id=项目 id；content=施工单全文；
        note=本轮改了什么（给用户看的备注）。"""
        from ...media.blockout import blockout_dir, parse_sheet

        p = ctx.project(project_id)
        sheet, errors = parse_sheet(content)
        if sheet is None:
            return "施工单没保存，先改这几处：\n" + "\n".join(f"- {e}" for e in errors)
        d = blockout_dir(p.id)
        (d / "施工单.md").write_text(content, encoding="utf-8")
        meta = sheet["meta"]
        head = (f"施工单已保存（{note or '新起草'}）：{len(sheet['objects'])} 个对象、"
                f"{len(sheet['shots'])} 个镜头、{meta['duration_sec']:.0f} 秒 @ "
                f"{meta['fps']}fps，"
                f"{meta['resolution'][0]}x{meta['resolution'][1]} {meta['engine']}。")
        shots = "\n".join(
            f"- {s['shot']}（{s.get('label') or '未命名'}）：{float(s['start']):g}-"
            f"{float(s['end']):g}s" for s in sheet["shots"])
        return (f"{head}\n【镜头】\n{shots}\n"
                "没问题就说「渲染白模」，要调整继续说。")

    @tool
    def blockout_build(project_id: str) -> str:
        """按当前施工单后台渲染白模视频（确定性构建，本机 Blender 渲染，不花钱）。
        用户看完施工单说「渲染 / 重建 / 出白模视频」时调用；没施工单会提示先起草，
        施工单还有校验错误会先拦下。"""
        from ...deps import blockout_manager as _manager
        from ...media.blockout import blockout_dir, parse_sheet

        p = ctx.project(project_id)
        sheet_path = blockout_dir(p.id) / "施工单.md"
        if not sheet_path.exists():
            return "这个作品还没有施工单——先按 blockout 技能起草一份（blockout_sheet_save）。"
        sheet, errors = parse_sheet(sheet_path.read_text(encoding="utf-8"))
        if sheet is None:
            return "施工单还没改到位，先解决这几处再渲染：\n" + "\n".join(f"- {e}" for e in errors)
        rec = _manager().start(p.id)
        if rec.get("status") != "running":
            return "白模正在渲染中，不用重复发起；想看进度就问「白模好了吗」。"
        return ("白模开始后台渲染（本机 Blender，不花钱）。完成后成片会登记进资产库，"
                "稍等片刻或问「白模好了吗」。")

    @tool
    def blockout_check(project_id: str) -> str:
        """查看白模渲染进度与结果。用户问「白模好了吗 / 渲染到哪了 / 白模视频呢」时调用。"""
        from ...deps import blockout_manager as _manager

        p = ctx.project(project_id)
        rec = _manager().get(p.id)
        if rec is None:
            return "这个作品还没发起过白模渲染（先起草施工单再说「渲染白模」）。"
        if rec["status"] == "running":
            tail = "；".join(rec["messages"][-2:])
            return f"白模还在渲染中（{tail or '刚启动'}）。"
        if rec["status"] == "failed":
            return f"白模渲染失败：{rec['error']}"
        r = rec["result"] or {}
        return (f"白模渲染完成：{float(r.get('duration_sec') or 0):.0f} 秒，已登记进资产库"
                f"（asset_id={r.get('asset_id')}）。要当参考素材时在镜头提示词编辑器里挑它；"
                "要调整就继续说改哪，改完单子重新渲染。")

    return [blockout_sheet_save, blockout_build, blockout_check]

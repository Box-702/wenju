# =====================================================================
# production.py —— 生产域工具：一键跑「小说 → 提示词」整条流水线
#
# 这是「工作流 = 对话 Agent 的一个工具」的落点：用户说「开始制作 /
# 出一版提示词」时调用。流水线本身在 app/agent/production.py（LangGraph
# 状态图），这里只负责启动后台任务并转述进度入口。
# 产出的是**待审提示词**——提交视频生成仍是用户的决定（既有门禁不变）。
# =====================================================================

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from .context import ToolContext

# 一次生成的时长上限（MiniMax H3 / 镜头组合同；见 PLAN-shot-group）
_UNIT_MAX_SEC = 15.0
_UNIT_MIN_SEC = 1.0
_SHOT_TYPES = {"extreme_wide", "wide", "medium", "close_up",
               "extreme_close_up", "over_shoulder", "pov"}


def validate_storyboard_units(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """把 Agent 解析出的分镜单元列表做确定性校验，返回 (清洗后的单元, 错误)。

    单元 = 一次生成（单镜或镜头组）。结构：
      {"prompt": 提交视频模型的全文, "duration_sec": 总秒,
       "shots": [{"duration_sec","shot_type","subject","action"}, ...]（可选，缺=单镜）}
    校验：prompt 非空、总时长 1-15s、多机位时成员时长之和≈总时长、
    景别合法。错误非空则不落库——成品分镜是用户手给的，格式错了要当场说，
    不能带着坏数据进出片（花钱）。
    """
    errors: list[str] = []
    if not isinstance(raw, list) or not raw:
        return [], ["分镜必须是非空数组（每个元素是一段生成单元）"]
    units: list[dict[str, Any]] = []
    for i, u in enumerate(raw, 1):
        if not isinstance(u, dict):
            errors.append(f"第 {i} 段不是对象")
            continue
        prompt = str(u.get("prompt") or "").strip()
        if not prompt:
            errors.append(f"第 {i} 段缺 prompt（提交视频模型的提示词）")
            continue
        try:
            dur = float(u.get("duration_sec") or 0)
        except (TypeError, ValueError):
            errors.append(f"第 {i} 段 duration_sec 不是数字")
            continue
        if not (_UNIT_MIN_SEC <= dur <= _UNIT_MAX_SEC):
            errors.append(f"第 {i} 段时长 {dur}s 超一次生成区间 [{_UNIT_MIN_SEC}, {_UNIT_MAX_SEC}]s")
            continue
        members_raw = u.get("shots")
        members: list[dict[str, Any]] = []
        if isinstance(members_raw, list) and members_raw:
            msum = 0.0
            for j, m in enumerate(members_raw, 1):
                if not isinstance(m, dict):
                    errors.append(f"第 {i} 段第 {j} 机位不是对象")
                    continue
                st = str(m.get("shot_type") or "medium").strip()
                if st not in _SHOT_TYPES:
                    errors.append(f"第 {i} 段第 {j} 机位景别非法：{st!r}")
                    st = "medium"
                try:
                    md = float(m.get("duration_sec") or 0)
                except (TypeError, ValueError):
                    md = 0.0
                msum += md
                members.append({
                    "duration_sec": md, "shot_type": st,
                    "subject": str(m.get("subject") or "").strip(),
                    "action": str(m.get("action") or "").strip(),
                })
            # 多机位：成员之和要≈总时长（±1s 容差）——组内时间戳由它推，漂了就乱
            if members and abs(msum - dur) > 1.0:
                errors.append(
                    f"第 {i} 段机位时长之和 {msum:.1f}s 与总时长 {dur:.1f}s 对不上（差>1s）")
        units.append({"prompt": prompt, "duration_sec": dur, "shots": members})
    return units, errors


def build(ctx: ToolContext) -> list:
    """装配生产域工具。"""
    store, llm, runner = ctx.store, ctx.llm, ctx.runner

    @tool
    def start_production(project_id: str) -> str:
        """启动整条生产流水线（后台运行）：初始化剧本 → 导演拆解 → 视觉风格 → 人物白模与场景图（资产库）→ 视频提示词。已完成的阶段自动复用，不会重复花钱。产出的提示词需要逐镜批准后才能出视频。用户说「开始制作 / 一键出提示词 / 跑完整流程」时调用。参数：project_id=项目 id。"""
        from ...agent.production import run_production

        p = ctx.project(project_id)

        def _settings():
            from ...config import get_settings

            return get_settings()

        def _run(task):
            def _progress(stage: str, msg: str) -> None:
                # runner 可能为 None（同步回退路径），update_step 前必须守卫——
                # 此前无守卫，同步跑一旦上报进度就 AttributeError（2026-11-11 审计；
                # 对照 screenplay.py 同款写法）。
                if runner is not None:
                    runner.update_step(task.id, msg, "running")

            return "；".join(run_production(
                store, llm, _settings(), p.id, on_progress=_progress,
            ).get("notes") or [])

        if runner:
            task_id = runner.start("生产流水线（小说→提示词）", _run, project_id=p.id)
            ctx.collector["payloads"].append({
                "type": "sub_agent_started",
                "task_id": task_id,
                "name": "生产流水线",
                "project_id": p.id,
            })
            return (
                "生产流水线已启动：初始化剧本 → 导演拆解 → 视觉风格 → 人物白模与场景图 → 视频提示词，"
                "已完成的阶段会自动复用。完成后提示词进入「待审」状态，"
                "用户逐镜批准后我们再决定什么时候生成视频。"
            )
        # 无 runner（测试 / 本地直跑）：同步执行完再回复——话术必须与后台路径区分，
        # 说「已启动」而实际已经跑完是失实汇报（审计修掉的同类问题）。
        summary = _run(type("_T", (), {"id": "local"})())
        return (
            f"生产流水线已跑完：{summary or '各阶段已就绪'}。"
            "提示词处于「待审」状态，用户可以批准后决定什么时候出片。"
        )

    @tool
    def import_storyboard(units: str, title: str = "", project_id: str = "") -> str:
        """导入一份**已经写好的成品分镜/视频提示词**，直接落成镜头方案进入批准→出片流程——
        不经过改编、导演、摄影重写（用户原文逐字保留，这就是提交视频模型的内容）。
        用户贴来分秒时序脚本、现成分镜表、或「按这段提示词直接生成」时调用（D-47：生成是一等公民，
        建项目只是簿记——没有 project_id 会自动建作品）。参数：
        units=JSON 数组字符串，每个元素是一段生成单元：
          {"prompt":"提交视频模型的提示词全文","duration_sec":总秒数(1-15),
           "shots":[{"duration_sec":机位秒,"shot_type":"medium|close_up|wide|over_shoulder|pov|extreme_close_up|extreme_wide","subject":"主体","action":"动作"}]}
          ——多机位段（镜头组）填 shots（成员时长之和≈总时长）；单镜段可省略 shots。
        title=作品名——自动建作品时**必须给**：用内容主题起一个短剧名
          （如「果园对白」「排钱」），作品列表靠它对照；project_id=已有作品 id（可选）。
        落库后提示词是「待批准」状态——批准（发起+用户确认闸门）后才出片，费用照旧明码。"""
        from ...domain import normalize_adaptation_type

        try:
            raw = json.loads(units) if isinstance(units, str) else units
        except (TypeError, ValueError) as e:
            return f"units 不是合法 JSON：{e}。请给出 JSON 数组字符串。"
        parsed, errors = validate_storyboard_units(raw)
        if errors:
            return "分镜校验未通过，未落库（避免带着坏数据花钱）：\n- " + "\n- ".join(errors[:8])

        # 容器：有 project 用之，没有就隐式建（簿记，不是仪式）
        if project_id:
            p = ctx.project(project_id)
            if p is None:
                return f"找不到作品 {project_id}。"
        else:
            from ...pipeline.knowledge import extract_title
            # 兜底链：Agent 起名 → 首段提示词提取 → 通用占位（尽量不留占位名）
            fallback = extract_title(parsed[0]["prompt"]) if parsed else ""
            p = store.create_project(
                title=(title.strip() or fallback or "提示词直出作品"),
                adaptation_type=normalize_adaptation_type("short_drama"),
                language="zh-CN", raw_text="",
            )
            ctx.collector["project_id"] = p.id

        shot_dicts: list[dict[str, Any]] = []
        order = 0
        for i, u in enumerate(parsed, 1):
            members = u["shots"]
            scene_id = "scene_001"
            if len(members) > 1:
                gid = f"g_import_{i:02d}"
                for k, m in enumerate(members):
                    shot_dicts.append({
                        "id": f"shot_{scene_id[6:]}_{order:03d}", "scene_id": scene_id,
                        "order": order, "duration_sec": m["duration_sec"] or 3,
                        "shot_type": m["shot_type"], "subject": m["subject"],
                        "action": m["action"],
                        "group_id": gid, "group_index": k,
                        # 组头带全文；组员 video_prompt 留空——提交走组单元
                        "group_prompt": u["prompt"] if k == 0 else None,
                        "video_prompt": u["prompt"] if k == 0 else "",
                        "prompt_status": "needs_review", "prompt_review_note": "",
                        "cut_reason": "导入分镜的机位切换" if k else "导入段起点",
                    })
                    order += 1
            else:
                shot_dicts.append({
                    "id": f"shot_{scene_id[6:]}_{order:03d}", "scene_id": scene_id,
                    "order": order, "duration_sec": u["duration_sec"],
                    "shot_type": (members[0]["shot_type"] if members else "medium"),
                    "subject": members[0]["subject"] if members else "",
                    "action": members[0]["action"] if members else "",
                    "video_prompt": u["prompt"],
                    "prompt_status": "needs_review", "prompt_review_note": "",
                    "cut_reason": "导入段",
                })
                order += 1

        version = store.create_video_version(
            project_id=p.id, shots=shot_dicts, source_type="agent",
            label="导入成品分镜", notes="逐字保留用户提供的提示词（import_storyboard）",
        )
        n_units = len(parsed)
        total = sum(u["duration_sec"] for u in parsed)
        est = n_units * 1.2
        return (
            f"已导入《{p.title}》：{n_units} 段生成单元（{len(shot_dicts)} 个机位，"
            f"共约 {total:.0f} 秒），提示词逐字保留你的原文（id={version.id}）。"
            f"作品 id={p.id}。下一步：发起批准（approve_video_prompts），"
            f"用户确认后才会提交出片——按当前档位预估约 ¥{est:.1f}。"
        )

    return [start_production, import_storyboard]

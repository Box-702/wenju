# =====================================================================
# screenplay.py —— 剧本域工具：建项目 → 建图 → 选段 → 初稿 → 改编 → 咨询
#
# 这条链是产品的主线。三条贯穿性约定：
#   1. 覆盖诚实化——长篇一次只能改编开头一段，返回话术必须说清覆盖范围，
#      并把「建章节地图 → 挑故事弧 → 按章续接」给成出路；
#   2. 没有初稿时 run_adaptation 自动先补一版，保证链路闭环；
#   3. 改编永远走底层工作流的 propose -> guard -> review(interrupt) 闭环，
#      工具只负责启动和转述，绝不替用户接受。
# =====================================================================

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from .context import ADAPT_TYPE_LABELS, LONG_TEXT_CHARS, ToolContext

log = logging.getLogger(__name__)


def build(ctx: ToolContext) -> list:
    """装配剧本域工具。"""
    store, llm, runner = ctx.store, ctx.llm, ctx.runner

    @tool
    def create_project(title: str, raw_text: str, adaptation_type: str = "short_drama") -> str:
        """新建一部作品。产品只做短剧。参数：title=剧名，raw_text=原著或故事原文。adaptation_type 可省略（默认短剧）。"""
        from ...domain import normalize_adaptation_type

        name = normalize_adaptation_type(adaptation_type)
        p = store.create_project(
            title=title.strip(),
            adaptation_type=name,
            language="zh-CN",
            raw_text=(raw_text or "").strip(),
        )
        ctx.collector["project_id"] = p.id
        label = ADAPT_TYPE_LABELS.get(name, name)
        # 作者风格只做规则画像（无网络调用），避免对话轮次被额外 LLM 调用拖慢
        try:
            from ...pipeline.knowledge import extract_author_style
            style = extract_author_style(raw_text)
            notes = f"作者风格：{style.get('summary', '')}"
            store.set_project_notes(p.id, notes)
        except Exception:
            pass
        from ...pipeline.knowledge import detect_genres

        genres = detect_genres(raw_text, top=2)
        reply = (
            f"已创建作品《{p.title}》（id={p.id}，类型：{label}，题材：{'、'.join(genres)}）。"
        )
        # 诚实化：超长原文一次生成只覆盖开头一小段，必须说清楚并给出路。
        from ...pipeline.chunking import chapter_spans

        text_len = len((raw_text or "").strip())
        n_chapters = len(chapter_spans((raw_text or "").strip()))
        if text_len > LONG_TEXT_CHARS:
            w = text_len / 10000
            chapter_tip = f"、共识别出 {n_chapters} 章" if n_chapters > 1 else ""
            reply += (
                f"注意：原文约 {w:.1f} 万字{chapter_tip}，直接生成初稿只会改编开头约 1.4 万字。"
                f"建议先让我「建立章节地图」，再从里面挑一段最适合改编的故事弧。"
            )
        reply += "需要我现在生成剧本初稿吗？"
        return reply

    @tool
    def map_novel(project_id: str, rebuild: str = "") -> str:
        """为长篇原文建立章节地图：逐章摘要 + 出场人物，作为「选段改编」的依据。超过约 1.5 万字的小说建议先建图。后台运行（章节数多时要几分钟）。已有地图时会拒绝重复建图，除非 rebuild=true（用户明确要求重建）。参数：project_id=项目 id；rebuild=传 "true" 强制重建。"""
        p = ctx.project(project_id)
        text = (p.raw_text or "").strip()
        if len(text) <= LONG_TEXT_CHARS:
            return f"原文只有约 {len(text) / 10000:.1f} 万字，不算长篇，直接「生成初稿」就能全部覆盖，不需要建图。"
        existing = len((p.novel_map or {}).get("chapters") or [])
        if existing and str(rebuild).lower() != "true":
            return (
                f"这本书已经有章节地图（{existing} 章），不用重建，直接说「挑一段改编」就能选段；"
                f"只有当原文替换过或摘要有问题时才需要重建（那时传 rebuild=true）。"
            )
        if not llm.available:
            return "建立章节地图需要对话模型（当前不可用），请先在设置里配置模型。"

        def _run(task):
            from ...pipeline.novel_map import build_novel_map

            m = build_novel_map(
                text, llm,
                on_progress=(lambda msg: runner.update_step(task.id, msg, "running")) if runner else None,
            )
            store.set_novel_map(p.id, m)
            n = len(m.get("chapters") or [])
            kind = "伪章节（原文没有章节标题，按长度切分）" if m.get("pseudo") else "章"
            return f"章节地图已建成：共 {n} {kind}，可从中挑选改编段落。"

        if runner:
            task_id = runner.start("章节地图", _run)
            ctx.collector["payloads"].append({
                "type": "sub_agent_started", "task_id": task_id, "name": "章节地图", "project_id": p.id,
            })
            return "正在为全书逐章建立地图（摘要每章剧情与出场人物），完成后告诉我可以「挑一段改编」。"
        m = _run(type("_T", (), {"id": "local"})())
        return m

    @tool
    def propose_arcs(project_id: str, from_chapter: str = "") -> str:
        """基于章节地图提出 2-3 个候选改编故事弧（章节范围+戏剧钩子），由用户选定后再生成剧本。需要先完成「建立章节地图」。参数：project_id=项目 id；from_chapter=起始章号（可选，默认从已改编进度之后开始）。"""
        p = ctx.project(project_id)
        m = p.novel_map
        chapters = m.get("chapters") or []
        if not chapters:
            return "还没有章节地图。请先让我「建立章节地图」，再挑选改编段落。"
        if not llm.available:
            return "挑选故事弧需要对话模型（当前不可用）。"
        from ...pipeline.novel_map import ARCS_PROMPT, arcs_context, parse_arcs

        try:
            start = int(from_chapter) if str(from_chapter).strip() else (p.adapted_until + 1 if p.adapted_until else 1)
        except ValueError:
            return "from_chapter 需要是章号数字。"
        start = max(1, min(start, len(chapters)))
        ctx_arcs = arcs_context(m, from_chapter=start)
        resp = llm.chat().invoke([
            {"role": "system", "content": ARCS_PROMPT},
            {"role": "user", "content": f"改编范围：第 {start} 章到第 {len(chapters)} 章。\n\n章节地图：\n{ctx_arcs}"},
        ])
        raw = getattr(resp, "content", "") or ""
        if isinstance(raw, list):
            raw = " ".join(b.get("text", "") for b in raw if isinstance(b, dict) and b.get("type") == "text")
        try:
            arcs = parse_arcs(str(raw))
        except ValueError:
            return "这次没能从章节地图里提炼出合适的故事弧，可以让我再试一次，或直接告诉我你想改编的章节范围。"
        lines = []
        for i, arc in enumerate(arcs, 1):
            line = f"{i}. 「{arc['title']}」第 {arc['from']}–{arc['to']} 章：{arc['hook']}"
            if arc["why"]:
                line += f"（{arc['why']}）"
            lines.append(line)
        return (
            f"从第 {start} 章起共 {len(chapters)} 章，为你挑出 {len(arcs)} 个候选故事弧：\n"
            + "\n".join(lines)
            + "\n告诉我选哪个（如「选 1」），或直接给我章节范围（如「改编第 5 到 16 章」），我就开始生成剧本。"
        )

    @tool
    def generate_script(project_id: str, from_chapter: str = "", to_chapter: str = "") -> str:
        """为指定项目生成剧本初稿（生成流水线：故事圣经 -> 场景/节拍）。

        长篇建议配合章节地图按章选段：from_chapter/to_chapter=章号（1 起，含两端）。
        有章节地图时不传 to_chapter 会按一次生成的容量（约 1.2 万字）自动续接；
        用户说「继续改编下一段」时直接调用本工具、章节参数留空即可，从进度接续并自动带前情提要。
        参数：project_id=项目 id。
        """
        from ...config import get_settings
        from ...pipeline.generation import generate_script as run_generation
        from ...pipeline.patch import validate_script

        p = ctx.project(project_id)
        raw = p.raw_text or ""
        span_label = ""
        prior_context = ""
        text = raw
        if str(from_chapter).strip() or str(to_chapter).strip() or (p.novel_map.get("chapters") if p.novel_map else None):
            from ...pipeline.chunking import slice_by_chapter
            from ...pipeline.novel_map import next_to_chapter, previous_context

            try:
                a = int(from_chapter) if str(from_chapter).strip() else (p.adapted_until + 1 if p.adapted_until else 1)
                if str(to_chapter).strip():
                    b = int(to_chapter)
                elif p.novel_map.get("chapters"):
                    b = next_to_chapter(p.novel_map, a)  # 按容量自动续接，不再默认切到全书末尾
                else:
                    b = 0
                text, span_label, b = slice_by_chapter(raw, a, b or 10**9)
            except ValueError as e:
                return f"选段失败：{e}"
            if a > 1:
                prior_context = previous_context(p.novel_map, a - 1)
        script, artifacts = run_generation(
            llm,
            get_settings(),
            title=p.title,
            raw_text=text,
            adaptation_type=p.adaptation_type,
            language=p.language,
            prior_context=prior_context,
        )
        version_label = f"初始生成（{span_label}）" if span_label else "初始生成"
        version = store.create_version(
            p,
            script,
            source_type="generation",
            label=version_label,
            notes=f"生成模式：{artifacts.get('mode', 'n/a')}",
            parent_version_id=p.current_version_id,
            set_current=True,
        )
        if span_label:
            store.set_adapted_until(p.id, b)
        issues = [i for i in validate_script(script) if i.severity == "error"]
        # 覆盖诚实化：说清这次改编的原文范围。
        from ...pipeline.chunking import chapter_spans

        n_chapters = len(chapter_spans(raw))
        if span_label:
            coverage = f"本次改编自{span_label}（约 {len(text) / 10000:.1f} 万字）"
            if len(text) > LONG_TEXT_CHARS:
                coverage += (
                    f"。注意：这个选段超过一次生成的容量（约 {LONG_TEXT_CHARS / 10000:.1f} 万字），"
                    f"初稿只覆盖了选段开头，建议把章节范围切小（单次不超过 2 章左右）再分别生成"
                )
            if p.adapted_until:
                total = len(p.novel_map.get('chapters') or []) or n_chapters
                coverage += f"，连载进度：已改编到第 {p.adapted_until} 章（全书 {total} 章）"
        elif len(raw) > LONG_TEXT_CHARS:
            coverage = (
                f"注意：原文约 {len(raw) / 10000:.1f} 万字，本次初稿只基于开头约 {len(text) / 10000:.1f} 万字。"
                f"要改编后面的内容，先让我「建立章节地图」再选段。"
            )
        else:
            coverage = f"原文约 {len(raw) / 10000:.1f} 万字，已全部纳入改编"
        tail = f"（有 {len(issues)} 处结构问题需注意）" if issues else ""
        if artifacts.get("naming_degraded"):
            # 名层护栏重跑后人物/地点名仍是拼音：如实说出来，别让用户以为一切正常。
            tail += "（注意：这次模型把人物或地点名写成了拼音，可能需要在对话里让我改名）"
        return (
            f"已生成初稿版本 {version.id}（模式：{artifacts.get('mode')}）："
            f"共 {len(script.scenes)} 场，{len(script.characters)} 个人物，"
            f"{len(script.locations)} 个地点。{coverage}。梗概：{script.logline}。{tail}"
            f"你可以说「把对白改口语一点」「节奏改紧凑」等让我开始改编。"
        )

    @tool
    def run_adaptation(project_id: str, instruction: str) -> str:
        """对指定项目发起一次改编：走底层 LangGraph 审阅工作流，产出可逐条接受的结构化提议。参数：project_id=项目 id，instruction=改编需求描述。"""
        from ...agent import runner as agent_svc
        from ...config import get_settings
        from ...pipeline.generation import generate_script as run_generation

        p = ctx.project(project_id)
        base_version = store.latest_version(p)
        if base_version is None:
            # 没有初稿时先自动生成，保证链路闭环。
            script, artifacts = run_generation(
                llm,
                get_settings(),
                title=p.title,
                raw_text=p.raw_text,
                adaptation_type=p.adaptation_type,
                language=p.language,
            )
            base_version = store.create_version(
                p, script, source_type="generation", label="初始生成",
                notes=f"生成模式：{artifacts.get('mode', 'n/a')}", set_current=True,
            )
        result = agent_svc.start_agent_run(
            store,
            llm,
            get_settings(),
            project=p,
            base_version=base_version,
            instruction=instruction,
            scene_ids=[],
        )
        ctx.collector["payloads"].append(
            {
                "type": "patch_review",
                "run_id": result.get("run_id"),
                "project_id": p.id,
                "plan": result.get("plan") or [],
                "patch": result.get("patch") or [],
                "steps": result.get("steps") or [],
                "status": result.get("status"),
                "error": result.get("error"),
                "review": result.get("review"),  # 评审打分 + 一致性保障结果
            }
        )
        if result.get("error"):
            return (
                f"已生成改编提议（{len(result.get('patch') or [])} 项操作），"
                f"但模型调用出现异常，已保留说明性建议：{result['error']}"
            )
        return (
            f"已完成改编分析，共 {len(result.get('patch') or [])} 项改动建议（"
            f"计划：{'；'.join((result.get('plan') or [])[:3])}）。"
            f"请审阅下面的卡片：可勾选部分接受、接受全部、拒绝，或给我反馈重新生成。"
        )

    @tool
    def ask(project_id: str, question: str) -> str:
        """就剧本 / 改编创作提问（参考剧本概况与作者风格），给出有依据的回答。参数：project_id=项目 id，question=问题。"""
        from ...pipeline.knowledge import format_author_style

        p = ctx.project(project_id)
        overview = ctx.overview_text(p)
        style_text = format_author_style(p.raw_text)

        if llm.available:
            try:
                resp = llm.chat().invoke(
                    [
                        SystemMessage(
                            content=(
                                "你是剧本创作顾问。基于下面提供的剧本概况和作者风格回答问题；"
                                "回答要具体、可操作，不要编造。"
                            )
                        ),
                        HumanMessage(
                            content=(
                                f"剧本概况：\n{overview}\n\n"
                                f"作者风格：\n{style_text}\n\n"
                                f"用户问题：{question}"
                            )
                        ),
                    ]
                )
                return str(resp.content or "").strip()
            except Exception as e:  # noqa: BLE001
                log.warning("ask 组装回答失败：%s", e)
        return f"（未配置模型，以下为直接信息）\n{style_text}"

    return [create_project, map_novel, propose_arcs, generate_script, run_adaptation, ask]

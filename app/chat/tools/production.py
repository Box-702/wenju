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
import re
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


# ---------- 外部成品分镜的确定性归一化（2026-11-12 果园 demo 漂移复盘） ----------
#
# 外部分镜工具产出的是「一条全局时间线的连续脚本」：全局锚只写一遍、切镜用
# 全局时间戳（At 00:09, cuts to…）。我们按生成上限 ≤15s 拆成独立单元后，
# 每段是一次全新的采样——段 2 若只剩「At 00:09, cuts to …」，既没时间锚点、
# 又丢了人物/环境描述，模型必然自由发挥（实测：换脸、换景、台词消失）。
# 归一化三件事，全部确定性规则、不动段内内容（D-47：逐字保留承诺针对内容）：
#   1. 剥掉段首的「At 00:XX, cuts to/back」全局时间戳前缀；
#   2. 组内后续切镜的时间戳按本段累计时长折算成相对值；
#   3. 剥离后不含任何锚特征（环境/人物）的段，自动前置全局锚句。

# 段首全局切镜前缀：At 00:09, cuts to/back …（逗号可选）
_CUT_LEAD_RE = re.compile(
    r"^(?:\[[^\]]{1,6}\]\s*)?[Aa]t\s*\d{1,2}[:.：]\d{1,2}(?:\.\d+)?\s*[,，]?\s*"
    r"(?:cuts?(?:\s+back)?\s*(?:to)?|切回|切至|镜头切(?:换)?至|切到)\s*[:：]?\s*")
# 任意 At 00:XX 时刻写法（组内折算用，也用于「段内已带时刻」判断）
_TS_ANY_RE = re.compile(r"[Aa]t\s*\d{1,2}[:.：]\d{1,2}(?:\.\d+)?")
# 锚特征：环境/时间/人物身份词——段落剥离前缀后若仍含其一，视为自带锚不补。
_ANCHOR_FEATURE_RE = re.compile(
    r"(午后|清晨|黄昏|夜晚|白天|果园|教室|酒馆|街道|客厅|荒野|沙漠|森林|海边|雪地|"
    r"室内|室外|阳光|月光|烛光|中年|年轻|老年|少年|少女|绅士|女子|男人|女人|"
    r"\d{1,3}\s*岁|[A-Za-z]+\s+(?:man|woman|girl|boy|lady|gentleman))")
# 全局锚：首单元第一个完整句（句号/换行边界），去掉台词引用，≤150 字。
_ANCHOR_STOP_RE = re.compile(r"[。.\n]")
# 台词开始：「…哽咽说：{…}」「念：{…}」——锚只要描写，不要对白。
_DIALOGUE_RE = re.compile(r"[，,]?\s*(?:[\u4e00-\u9fff]{0,6}(?:说|念|道|喊|问道|回答|低语|呢喃))?\s*[:：]?\s*\{")
# 表演/位移子句：全局锚截断点——这些是**这一镜**的动作指令，抄进锚会让
# 每个后续单元都重演一遍（果园 2026-11-14 实测动作+运镜重复三次）。
# 只收「明显是动态表演」的动词习惯语，静态外观（身穿/站姿/目光习惯性…）
# 由更长的外观描述承担，不在此列——宁可截少不误杀外观。
_ANCHOR_ACTION_RE = re.compile(
    r"(缓慢|快步|转身|踱步|走近|走近|逼近|后退|移动|位移|迈步|上前|凑近|"
    r"打量|扫视|盯着看|抬手|伸手|挥手|点头|摇头|俯身|起身|站起|坐下|"
    r"凑到|抿嘴|皱眉|蹙眉|挑眉|泛红|哽咽|叹气|吸气|呼吸|吞咽|喉结|"
    r"walk|turn|step|approach|scan|nod|shake|lean|rise|sit|sigh|swallow|"
    r"raise|lower|reach|move|shift|gaze (?:moves|shifts))")
_CAMERA_ONLY_RE = re.compile(
    r"(镜头|机位|景别|特写|近景|中景|全景|远景|俯拍|仰拍|过肩|主观|空镜|推拉|摇移|跟随)$")


def _global_anchor(first_prompt: str) -> str:
    """从首单元提示词取全局锚：第一个含锚特征（环境/人物）的句子，
    在台词引用处截断，**再在第一个动作子句前截断**。

    锚只该传静态事实（环境/机位/人物身份外观），不能夹带表演指令——
    实测（果园 2026-11-14）：首句「…焦点锁在少女面部，她缓慢踱步打量对方，
    眼眶泛红，哽咽说：{…}」整句抄进锚后，每个后续单元都收一遍
    「踱步+打量+泛红」，同一段动作与运镜在成片里重演三次。"""
    for s in _ANCHOR_STOP_RE.split(first_prompt):
        s = s.strip(" ，,、")
        if len(s) < 8:
            continue
        m = _DIALOGUE_RE.search(s)
        if m:
            s = s[:m.start()].strip(" ，,、:：")
        # 动作截断：按逗号切子句，累加静态前缀，遇表演/位移子句即停。
        kept: list[str] = []
        for clause in re.split(r"[，,]", s):
            if _ANCHOR_ACTION_RE.search(clause):
                break
            if clause.strip(" ：:"):
                kept.append(clause.strip())
        s = "，".join(kept).strip(" ，,、:：")
        if len(s) < 8 or not _ANCHOR_FEATURE_RE.search(s):
            continue
        return s[:150]
    return ""


def normalize_units(parsed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """就地归一化外部成品分镜单元列表（见上函数头注释）。返回同一列表。"""
    if not parsed:
        return parsed
    anchor = _global_anchor(str(parsed[0].get("prompt") or ""))
    cursor_before = 0.0  # 本段在外部全局时间线上的起点（前段时长之和）
    for u in parsed:
        dur = float(u.get("duration_sec") or 0)
        start = cursor_before
        cursor_before += dur
        body = str(u.get("prompt") or "").strip()
        stripped = _CUT_LEAD_RE.sub("", body, count=1)
        needs_anchor = bool(anchor) and stripped != body  # 曾带全局切镜前缀=非首段
        # 剥后无锚特征也补（外部脚本常有「他走进房间」式裸续段）
        if anchor and not _ANCHOR_FEATURE_RE.search(stripped):
            needs_anchor = True
        # 组内切镜时刻折算：把全局时间戳改成本段相对值（钳到 [0, 本段时长]）
        if _TS_ANY_RE.search(stripped):
            def _rel(m: re.Match, start: float = start, dur: float = dur) -> str:
                # 外部分镜的时间戳是全局时间线的：平移 cost = 本段起点，
                # 落在 [0, 本段时长] 内的改写成本段相对时刻；越界的整段
                # 不存在这次采样里，删掉写法只留切镜描述。
                raw = m.group(0)
                tok = re.search(r"(\d+(?:[:.：]\d+)*)\s*$", raw)
                if not tok:
                    return raw
                parts = re.split(r"[:.：]", tok.group(1))
                try:
                    sec = (float(parts[0]) * 60 + float(parts[1])) if len(parts) >= 2 else float(parts[0])
                except (ValueError, IndexError):
                    return raw
                rel = sec - start
                if 0.0 <= rel <= dur:
                    return raw[:tok.start()] + f"At 00:{rel:.3f}"
                return ""
            stripped = _TS_ANY_RE.sub(_rel, stripped).strip(" ，,")
        u["prompt"] = f"{anchor}。{stripped}" if (needs_anchor and anchor) else stripped
    return parsed


# ---------- 悬空指代与台词超载（2026-11-12 接力v2 漂移复盘） ----------
#
# 外部成品分镜是「连续片子」语域写的：人物可以指代画面外的内容
# （"继续念后半段"——观众刚听过前半段；"他听完质问"——质问在上一镜）。
# 拆成独立生成单元后这些指代**不可解析**：一次全新采样没有"前半段"可续，
# 模型只能自由发挥编故事——实测 12s 单元里出现完全偏离剧本的后半段剧情。
# 5.40 修的是"段内缺环境/人物锚"，这里修的是"段内指代段外剧情"——
# 后者代码无法替用户补内容（台词后半段是什么只有用户知道），所以只能拦。

# 悬空指代词：出现在**非首单元**里、且附近没有同段台词文本时，几乎必跑偏。
_DANGLING_RE = re.compile(
    r"(后半段|后半部分|前半段|接着念|继续念|继续说|接着说|接下来说|"
    r"如前(?:所述|所说)|刚才(?:的|那)|上一(?:句|段|镜)|同上|同理|重复(?:刚才|上一)|"
    r"台词同上|the (?:rest|second half|latter) (?:of|part)|as (?:before|above)|"
    r"continue(?:s|d)? (?:with|the)|repeats? (?:the|it))")

# 台词超载估算：{} 「」 『』 里的文本按口播语速估秒（英文 ~2.6 词/s，中文 ~4.5 字/s，
# 取保守偏慢值）。生成时长 < 台词 + 1s 余量时，台词必被截——实测漂移的次要成因。
_QUOTED_RE = re.compile(r"\{([^{}]*)\}|「([^「」]*)」|『([^『』]*)』")


def estimate_speech_seconds(prompt: str) -> float:
    """提示词里全部台词的口播时长估算（秒）。无台词返回 0。"""
    total = 0.0
    for m in _QUOTED_RE.finditer(prompt):
        text = next((g for g in m.groups() if g), "")
        en_words = len(re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text))
        cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        # 剩余杂字符按 3 字符/s 粗估
        other = max(0, len(text) - en_words * 4 - cjk_chars * 2)
        total += en_words / 2.2 + cjk_chars / 3.8 + other / 3.0
    return round(total, 1)


def check_units_selfcontained(parsed: list[dict[str, Any]]) -> list[str]:
    """确定性检查：每段必须能独立拍——悬空指代 / 台词超载 / 台词重播当场报错，别花钱赌。"""
    errors: list[str] = []
    # 已完成台词账本（seedance 借鉴清单 #4：completed beats 禁止重播）。
    # 同一条 {} 引用在后面的单元再出现 = 模型会把这句话**再念一遍**
    # （果园 v3 段2「他刚听完质问『…』」就是这种写法，靠引用上段台词演反应
    # ——独立采样听不到上段，只会重说）。反应镜要写身体反应，不引用台词。
    # 注：normalize_units 补的全局锚句在台词引用前截断（_DIALOGUE_RE），
    # 不含 {} 段，不会污染账本。
    spoken: dict[str, int] = {}
    for i, u in enumerate(parsed, 1):
        prompt = str(u.get("prompt") or "")
        dur = float(u.get("duration_sec") or 0)
        if i > 1:  # 只查非首单元：首单元的"停顿/吸气"类节奏词多为自指画面，误报率高
            for m in _DANGLING_RE.finditer(prompt):
                errors.append(
                    f"第 {i} 段有悬空指代「{m.group(0)}」：拆段生成是独立采样，"
                    f"画面外的前半段/上一句对它不存在，模型只能编——"
                    f"请把指代的内容在本段提示词里写全（如台词的完整文本）")
        speech = estimate_speech_seconds(prompt)
        if speech and speech + 1.0 > dur:
            errors.append(
                f"第 {i} 段台词约 {speech:.1f}s，超过本段生成时长 {dur:.0f}s（含 1s 余量）："
                f"台词会被截断或挤掉表演——加长该段或精简台词")
        for q in _QUOTED_RE.finditer(prompt):
            text = next((g for g in q.groups() if g), "")
            key = re.sub(r"[\s,，.。!！?？'\"'']+", "", text).lower()
            if len(key) < 8:
                continue  # 短感叹词重复是正常戏剧手法，不管
            if key in spoken:
                errors.append(
                    f"第 {i} 段重播了第 {spoken[key]} 段已念的台词「{text[:40]}…」："
                    f"独立采样听不到上段，这句会被再念一遍——反应镜请写身体反应"
                    f"（眉头渐锁/喉结滚动），不要引用上段台词")
            else:
                spoken.setdefault(key, i)
    return errors


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
        落库后提示词是「待批准」状态——批准（发起+用户确认闸门）后才出片，费用照旧明码。
        ⚠️ 拆段=独立采样：每段提示词必须**自带全部所需信息**，不能写「继续念后半段」
        「如前所述」这类指代前面段落的承接语（闸门会当场拒绝并点名哪段哪句）——
        用户贴来的连续脚本若有这种写法，先把被指代的内容（台词全文、事件描述）
        改写进对应段再调用。
        改写时守两条剧情连贯规则（2026-11-13 果园v3 实测教训）：
        ① **动作不重播**：后段从上一段末帧的状态接着写，上一段已完成的动作
          （走近/踱步/打量）不得在后段再出现一遍——每段的动词必须是新发生的
          事，否则成片就是同一段动作放三遍；
        ② **同时性切镜进组**：原文表达「A 说话的同时切 B 反应」的，必须做成
          一个镜头组（shots 数组，组内共享一条音轨），不得拆成先后两个独立段
          ——独立段的模型听不到前段台词，B 只会呆站。
        """
        from ...domain import normalize_adaptation_type

        try:
            raw = json.loads(units) if isinstance(units, str) else units
        except (TypeError, ValueError) as e:
            return f"units 不是合法 JSON：{e}。请给出 JSON 数组字符串。"
        parsed, errors = validate_storyboard_units(raw)
        if errors:
            return "分镜校验未通过，未落库（避免带着坏数据花钱）：\n- " + "\n- ".join(errors[:8])
        # 外部全局时间线 → 独立生成单元的确定性归一化（时间戳折算 + 补全局锚）
        parsed = normalize_units(parsed)
        # 悬空指代 / 台词超载闸门：代码补不了内容（后半段台词是什么只有用户知道），
        # 但绝不带着「不可解析的段」花钱出片（接力v2 实测：12s 段自由发挥出另一部戏）。
        dangling = check_units_selfcontained(parsed)
        if dangling:
            return ("分镜含拆段后无法独立拍摄的表述，未落库（避免花钱漂移）：\n- "
                    + "\n- ".join(dangling[:6])
                    + "\n修正办法：把指代的内容在本段提示词里写全"
                      "（台词「后半段」请贴完整文本，「听完质问」请写明质问了什么）。")

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
        prev_head_order: int | None = None
        for i, u in enumerate(parsed, 1):
            members = u["shots"]
            scene_id = "scene_001"
            # 跨单元接力：段头链到上一段段头（批次串行推进时上一段已出片，
            # _auto_references 解析成参考视频；果园 demo 实测断链=漂移，2026-11-12）
            chain = {"chain_from": prev_head_order} if prev_head_order is not None else {}
            if len(members) > 1:
                gid = f"g_import_{i:02d}"
                for k, m in enumerate(members):
                    shot_dicts.append({
                        "id": f"shot_{scene_id[6:]}_{order:03d}", "scene_id": scene_id,
                        "order": order, "duration_sec": m["duration_sec"] or 3,
                        "shot_type": m["shot_type"], "subject": m["subject"],
                        "action": m["action"],
                        "group_id": gid, "group_index": k,
                        **(chain | {"reference_group": "g_import_scene"} if k == 0 else {}),
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
                    **(chain | {"reference_group": "g_import_scene"}),
                    "video_prompt": u["prompt"],
                    "prompt_status": "needs_review", "prompt_review_note": "",
                    "cut_reason": "导入段",
                })
                order += 1
            prev_head_order = order - (len(members) if len(members) > 1 else 1)

        version = store.create_video_version(
            project_id=p.id, shots=shot_dicts, source_type="agent",
            label="导入成品分镜", notes="逐字保留用户提供的提示词（import_storyboard）",
        )
        n_units = len(parsed)
        total = sum(u["duration_sec"] for u in parsed)
        return (
            f"已导入《{p.title}》：{n_units} 段生成单元（{len(shot_dicts)} 个机位），"
            f"提示词逐字保留你的原文（id={version.id}）。"
            f"作品 id={p.id}。下一步：发起批准（approve_video_prompts），"
            f"用户确认后才会提交出片——预计生成约 {total:.0f} 秒视频"
            f"（接力参考与参考图在提交时按镜头计划自动挂上）。"
        )

    return [start_production, import_storyboard]

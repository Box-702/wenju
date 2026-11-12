# =====================================================================
# dramaturgy.py —— 「先戏剧，后语法」的确定性审计核心
#
# 方法论来源：《AIGC 视觉内容提示词方法论》（smixs/visual-skills · CC BY 4.0）
# 核心思想：模型语法在戏剧结构到位之前一文不值——结构对了，模型才有东西
# 可渲染；结构错了，再正确的语法也救不了这个镜头。
#
# 本模块把方法论里「可代码化的硬规则」全部落成纯字符串/字段运算，不依赖 LLM：
#   - 场景公式五要素、三工作规则、细节律（三细节）、五锚点、最终画面规则；
#   - 空洞占位词表（cinematic/epic/masterpiece 这类渲染不出来的词）；
#   - 否定句检测（视频模型会把被否定的动作画出来）；
#   - 台词引用检测（会渲染成含混的假说话）。
#
# 接入点：
#   - 导演拆解后：audit_breakdowns() 的结论回喂 critique 循环重拆；
#   - 摄影指导拼装后：audit_video_prompt() 写进 shot.prompt_audit，
#     人工审阅时逐镜可见（见 PromptEditor）。
# =====================================================================

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------
# 空洞占位词：每一个都是「作者没写出细节」的占位符，没有一个能被渲染。
# 命中时只告警 + 机械剥除独立成分，真正的修复（换成三个物理事实）靠
# 摄影指导的写作规则在源头完成。
# ---------------------------------------------------------------------
_SLOP_WORDS_ZH = (
    "电影感", "大片感", "史诗感", "史诗级", "高级感", "震撼", "唯美",
    "高质量", "高画质", "超高清", "大师级", "极致画面", "视觉冲击",
)
_SLOP_WORDS_EN = (
    "cinematic", "epic", "stunning", "masterpiece", "breathtaking",
    "high quality", "professional", "beautiful lighting", "dynamic camera",
    "intense moment", "powerful scene", "dramatic lighting", "dramatic close-up",
    "award-winning", "4k", "8k", "ultra detailed", "best quality",
)
# 英文占位短语剥离时视为可丢弃的连接组织（"cinematic look" 整体算占位）。
_EN_FILLER = {"shot", "look", "style", "feel", "quality", "footage", "scene", "vibes", "a", "the", "of"}

# ---------------------------------------------------------------------
# 否定句：视频模型会把被否定的动作本身画出来（「手没有穿过带子」→ 画穿带子）。
# 只抓明确的否定副词/动词；「不断」「不同步」「无人的街道」这类正向/无关用法不误伤。
# ---------------------------------------------------------------------
_NEGATION_RE = re.compile(
    r"(并不是|并非|没有|不是|不能|不会|不得|不要|不宜|避免|防止|切勿|禁止|别让|不存在)"
    r"|\b(no|not|never|without|avoid)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------
# 台词引用：说话动词后的引用几乎一定是台词——语音交给后期配音，
# 引用进 prompt 只会得到含混的假说话。招牌/字幕类引用（「写着」）不受影响。
# ---------------------------------------------------------------------
_SPEECH_VERBS = ("说", "问", "答", "喊", "叫", "念", "读", "道", "开口", "报告", "低声", "大声", "嘀咕", "嘟囔")
_QUOTE_SPAN = re.compile(r"[「『“\"]([^」』”\"]*)[」』”\"]")

#: 子句切分：中文顿号也算——「A、B、A」这种重复前面常挂着顿号。
_CLAUSE_SPLIT_RE = re.compile(r"[，。；;、\n]")

# ---------------------------------------------------------------------
# 三细节词库（细节律：环境压力 / 身体微动作 / 声音锚点·视觉母题）。
# 词库法是尽力而为的保守判定：命中算有，没命中不算没有——但
# 「零命中」的镜头几乎一定是糨糊，值得标记出来给导演重写。
# ---------------------------------------------------------------------
# 身体微动作：情绪唯一能渲染的地方是演员的身体。
_MICRO_ACTION_WORDS = (
    "下颌", "下巴", "眉", "嘴角", "嘴唇", "唇", "咽", "吞", "咬", "眨",
    "指节", "手指", "指尖", "拳头", "攥", "抠", "捏", "握", "搓", "掐",
    "呼吸", "喘", "颤抖", "发抖", "僵", "塌", "歪头", "低头", "抬头", "偏头",
    "肩膀", "耸肩", "缩", "后仰", "前倾", "踉跄", "顿住",
    "jaw", "knuckle", "finger", "fist", "swallow", "blink", "grip", "clench",
    "tremble", "breath", "shoulder", "lip", "brow", "chin", "stiffen", "flinch",
)
# 声音锚点：与全片主干绑定的重复感知钩子。
_SOUND_WORDS = (
    "秒针", "滴答", "钟", "脚步声", "雨声", "雨点", "风声", "嗡鸣", "嗡嗡",
    "鸣笛", "汽笛", "哨声", "水声", "滴水", "电流声", "滋滋", "咔", "吱",
    "嘶", "铃", "蝉鸣", "心跳", "耳鸣", "寂静", "静默",
    "ticking", "tick", "footsteps", "hum", "buzz", "drip", "whistle", "sizzle",
    "clock", "silence", "heartbeat", "ringing",
)
# 视觉母题：反复出现的感知锚点（反射面、光源、特定物件）。
_MOTIF_WORDS = (
    "倒影", "反射", "镜面", "玻璃", "积水", "屏幕的光", "霓虹", "灯闪", "闪烁",
    "频闪", "蒸汽", "雾气", "烟", "尘埃", "光斑", "秒针",
    "reflection", "mirror", "glass", "neon", "flicker", "steam", "puddle", "glow",
)
# 环境压力（光影词）：方向与相对关系是底线，光不许永远黏在脸上。
_LIGHT_WORDS = (
    "光线", "光源", "灯光", "灯", "阳光", "月光", "霓虹", "阴影", "影子", "剪影",
    "逆光", "冷光", "暖光", "光斑", "光锥", "高光", "光照", "打光", "钨丝",
    "light", "lamp", "shadow", "sunlight", "moonlight", "neon", "backlit",
    "silhouette", "glow", "flare",
)

# 三工作规则的合法值。
SHOT_FUNCTIONS = ("emotion", "action", "pressure")

# 运镜动词（「推开门」不命中；「推近/环绕/跟拍」才算运镜）。
_CAMERA_MOVE_RE = re.compile(
    r"(推近|拉近|推入|拉出|后拉|横移|摇镜|摇向|环绕|跟拍|跟随|升降|俯冲|推进)"
    r"|\b(push[- ]?in|pull[- ]?out|pan|orbit|tracking|dolly|crane|tilt)\b",
    re.IGNORECASE,
)

# 镜头语境词：运镜动词必须挨着它，才算「在描述镜头怎么动」。
# 没有语境词时，「在地面拉出一道冷光带」里的「拉出」会被误当成 pull-out——
# 这类误报曾让真实提示词 4/4 全被标成「一镜多运镜」。
_CAMERA_CONTEXT_WORDS = (
    "镜头", "摄影机", "摄像机", "机位", "运镜", "视角", "构图", "景别", "画面", "camera", "shot",
)
# 语境窗口：动词前后多少字内出现镜头词才算数。
_CAMERA_CONTEXT_WINDOW = 14


def find_camera_moves(text: str, *, context_window: int = _CAMERA_CONTEXT_WINDOW) -> list[str]:
    """找出**带镜头语境**的运镜动词。

    与 `_CAMERA_MOVE_RE` 的区别：后者只匹配字面，会把「拉出一道冷光带」误判成
    pull-out；本函数要求动词附近出现镜头/摄影机/机位等语境词，
    否则不算运镜。`camera.path`（导演写的运镜轨迹）请直接用 `find_camera_moves` 判定，
    它天然带语境。
    """
    if not text:
        return []
    out: list[str] = []
    for m in _CAMERA_MOVE_RE.finditer(text):
        verb = m.group(0)
        lo = max(0, m.start() - context_window)
        hi = min(len(text), m.end() + context_window)
        window = text[lo:hi].lower()
        if any(w in window for w in _CAMERA_CONTEXT_WORDS):
            out.append(verb)
    return out


def strip_style_block(prompt: str, style: str) -> str:
    """从提示词里剥掉**全片风格段**（美术指导产出的 camera_style 等）。

    风格段描述的是整部片的运镜语法（「以缓慢推近、轻微横移为主」），每个镜头
    都会带一遍——它不属于「本镜的运镜」，统计时必须排除，否则一镜多运镜会全量误报。

    风格段进提示词时末尾标点会被改成「，」，因此这里按去尾标点后的核心串匹配。
    """
    if not prompt or not style:
        return prompt
    core = style.strip().rstrip("。；;，,、 ")
    if core and core in prompt:
        return prompt.replace(core, " ")
    return prompt


def mentions_lighting(text: str) -> bool:
    """文本里有没有写光影（用于判断风格段还要不要补光）。

    没有就补、有就不补——同一段提示词里出现两套互相矛盾的光源描述，
    视频模型会随机挑一套执行（judge 实测：前文写冷绿日光灯，末尾又贴暖黄灯）。
    """
    return _has_any(text or "", _LIGHT_WORDS)


# 判定「这段文字有没有交代摄影机」用的词表。
# 比 _CAMERA_CONTEXT_WORDS 窄：不看「画面/视角/构图」——它们几乎每句都有，
# 会让「没写运镜」永远判成「写了」。
_CAMERA_MENTION_WORDS = (
    "镜头", "摄影机", "摄像机", "机位", "运镜", "camera", "lens", "shot",
)


def mentions_camera(text: str) -> bool:
    """文本里有没有写摄影机/运镜（用于判断风格段还要不要补摄影风格）。"""
    return _has_any(text or "", _CAMERA_MENTION_WORDS)


def strip_camera_moves(text: str) -> str:
    """剥掉文本里**列举具体运镜方式**的子句，保留其余风格描述。

    背景：`camera_style` 会被逐字拼进每一个镜头。它如果写着「以缓慢横移、推近、
    推拉为主」「跟随人物时提前或滞后」，那每个镜头都会收到一串**互相冲突的运镜指令**——
    本镜明明是固定机位或单一推进，视频模型却同时被要求做三四种运动。
    这不是风格问题，是给模型的矛盾指令。

    做法：按标点切成子句，丢掉含运镜动词的子句，其余原样拼回。
    保留下来的是「客观机位」「机位高度」「手持呼吸感」「允许轻微失焦」这类
    **不与具体镜头冲突**的风格身份；丢掉的是「本片常用哪些运动」这种运动清单。

    用的是不带语境要求的原始正则（`_CAMERA_MOVE_RE`）——在风格段里，
    孤零零一个「推近」本身就是一条运镜指令，不该因为附近没有「镜头」二字就放过。
    """
    if not text:
        return text
    # 切分要细：中文顿号/逗号也可能是子句边界（「客观机位，稳定为主，关键处缓慢推近」
    # 拆开后「客观机位」才能留下）。
    # re.split 带捕获组 → [子句, 分隔符, 子句, 分隔符, ...]
    parts = re.split(r"([。；;，,、：:])", text)
    kept: list[str] = []
    for i in range(0, len(parts), 2):
        clause = parts[i]
        if not clause:
            continue
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        if _CAMERA_MOVE_RE.search(clause):
            # 含运镜动词 → 整句丢掉（连同它后面的分隔符，否则会留下悬空标点）
            continue
        kept.append(clause + sep)
    result = "".join(kept)
    # 收拾标点：连续的分隔符合并，句首句尾多余标点去掉
    result = re.sub(r"[，,、；;：:]{2,}", "，", result)
    result = re.sub(r"^[，,、；;：:]+", "", result)
    result = re.sub(r"[，,、；;：:]+$", "", result)
    return result.strip()


# ---------- 基础检测 ----------


def find_slop_words(text: str) -> list[str]:
    """找出 prompt 里的空洞占位词（渲染不出来的形容词）。"""
    if not text:
        return []
    low = text.lower()
    return [w for w in _SLOP_WORDS_ZH if w in text] + [w for w in _SLOP_WORDS_EN if w in low]


def find_negations(text: str) -> list[str]:
    """找出否定式描述（模型会把被否定的动作画出来）。

    引号内的文字先剥掉再检测——「警戒带上写着『禁止入内』」是画面里的实体招牌，
    不是给模型的否定指令，不能误伤。
    """
    if not text:
        return []
    clean = _QUOTE_SPAN.sub("”", text)
    return [m.group(0) for m in _NEGATION_RE.finditer(clean)]


def find_repeated_clause(text: str, *, min_len: int = 8) -> str:
    """找出同一段提示词里被写了两遍的子句（返回重复的那一句，没有则空串）。

    典型来源不是模型啰嗦，而是拼装层的兜底逻辑重复追加：最终画面句按
    「锚的前几个字有没有出现过」判断，模型换个开头就被判成没写，于是整句又追一遍。
    视频模型看到同一句两次，会当成两个连续动作或两次运镜。

    只抓**逐字重复**：改写式重复交给 judge。宁可漏报，也不要把
    「他推门。门开了。」这种正当的回环判成重复。
    """
    if not text:
        return ""
    seen: set[str] = set()
    for part in _CLAUSE_SPLIT_RE.split(text):
        key = re.sub(r"\s+", "", part)
        if len(key) < min_len:
            continue
        if key in seen:
            return part.strip()
        seen.add(key)
    return ""


def _is_official_dialogue(head: str) -> bool:
    """官方音画同出句式：`角色说话：「台词」`（MiniMax/可灵/Seedance 文档确认，
    2026-11-12 提示词规则修订）。带这个标记的引用是**特性**——锚表演节奏，
    成片仍以下阶段 TTS 为准；不带标记的「说道：『…』」才是模型会渲染成
    含混假语音的旧写法，照旧剥除。"""
    return "角色说话" in head or "说话：" in head or "说话:" in head


def find_spoken_quotes(text: str) -> list[str]:
    """找出跟在说话动词后面的引用台词（官方句式除外）。"""
    if not text:
        return []
    out: list[str] = []
    for m in _QUOTE_SPAN.finditer(text):
        head = text[max(0, m.start() - 6):m.start()]
        if _is_official_dialogue(head):
            continue
        if any(v in head for v in _SPEECH_VERBS):
            out.append(m.group(1))
    return out


def strip_spoken_quotes(text: str) -> str:
    """把「跟在说话动词后面的引用台词」从提示词里剥掉（语音交给后期配音）。

    官方 `角色说话：「…」` 句式保留（见 _is_official_dialogue）。"""
    if not text:
        return text
    out: list[str] = []
    last = 0
    for m in _QUOTE_SPAN.finditer(text):
        head = text[max(0, m.start() - 6):m.start()]
        if _is_official_dialogue(head) or not any(v in head for v in _SPEECH_VERBS):
            continue
        out.append(text[last:m.start()])
        last = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def strip_slop(text: str) -> str:
    """保守剥除占位词：只删「整段都是占位词」的短句，混写句原样保留交给审计告警。

    「夜景，电影感，雨」→「夜景，雨」；「画面呈现电影感十足的夜景」原样保留
    （机械删词会破坏语法，修复要靠写作规则在源头完成）。
    """
    if not text or not find_slop_words(text):
        return text
    segments = re.split(r"([，,。；;、])", text)
    kept: list[str] = []
    for seg in segments:
        if re.fullmatch(r"[，,。；;、]", seg):
            kept.append(seg)
            continue
        if _segment_is_slop(seg):
            # 连同前面残留的逗号一起丢，避免出现悬空逗号
            if kept and re.fullmatch(r"[，,、]", kept[-1]):
                kept.pop()
            kept.append("")
            continue
        kept.append(seg)
    result = "".join(kept)
    result = re.sub(r"^[，,。；;\s]+", "", result)
    result = re.sub(r"[，,、]{2,}", lambda m: m.group(0)[0], result)
    return result.strip()


def _segment_is_slop(seg: str) -> bool:
    """判断一个短句是否「整段都是占位词」（允许夹杂 画面/质感/look 等组织词）。"""
    s = seg.strip()
    if not s or len(s) > 12:
        return False
    remaining = s.lower()
    for w in sorted(_SLOP_WORDS_ZH + _SLOP_WORDS_EN, key=len, reverse=True):
        remaining = remaining.replace(w.lower(), "")
    remaining = re.sub(r"[的之感画面质感风格镜头十足般]", "", remaining)
    remaining = re.sub(r"\b(" + "|".join(sorted(_EN_FILLER)) + r")\b", "", remaining)
    return not remaining.strip()


# ---------- 细节律：三细节判定 ----------


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(w in text or w in low for w in words)


def three_details(
    *,
    action: str = "",
    lighting: str = "",
    background_action: str = "",
    prompt: str = "",
) -> tuple[int, list[str]]:
    """按细节律统计一个镜头携带的物理细节类别数。

    返回 (命中类别数 0-3, 缺失类别名列表)。三类：
      ① 环境压力（光线的方向与相对关系、空间物理事实）
      ② 身体微动作（情绪翻译到身体上的表现）
      ③ 声音锚点 / 视觉母题（与主干绑定的重复感知钩子）
    词库法是保守判定：命中算有；零命中的镜头几乎一定是废镜头。
    """
    body = prompt or ""
    missing: list[str] = []

    env_hit = bool(lighting.strip()) or _has_any(body, _LIGHT_WORDS)
    body_hit = _has_any(action, _MICRO_ACTION_WORDS) or _has_any(body, _MICRO_ACTION_WORDS)
    motif_hit = (
        _has_any(background_action, _SOUND_WORDS + _MOTIF_WORDS)
        or _has_any(action, _SOUND_WORDS + _MOTIF_WORDS)
        or _has_any(body, _SOUND_WORDS + _MOTIF_WORDS)
    )
    if not env_hit:
        missing.append("环境压力")
    if not body_hit:
        missing.append("身体微动作")
    if not motif_hit:
        missing.append("声音锚点/视觉母题")
    return 3 - len(missing), missing


# ---------- 导演侧：戏剧结构审计（回喂 critique 循环） ----------


def audit_breakdowns(breakdowns: list[Any]) -> list[str]:
    """出厂前戏剧结构检查（第一道审计）。逐镜过五项，逐场过一项：

    1. 场景五锚点是否已命名（情绪/母题/物体/破裂/最终画面，全空才算未命名）；
    2. 每镜是否做了三件事之一（function 必填）；
    3. 每镜是否命名了最终画面（模型把它当情绪目的地）；
    4. 每镜是否通过三细节检查（零细节 = 废镜头，最常被违反；「建立镜头」不豁免）。
    """
    issues: list[str] = []
    for bd in breakdowns:
        anchors = getattr(bd, "anchors", None)
        if anchors is not None and not any(
            str(getattr(anchors, k, "") or "").strip()
            for k in ("emotion", "motif", "object", "break_point", "final_image")
        ):
            issues.append(
                f"场景 {bd.scene_id} 的五锚点全部空缺（情绪/母题/物体/破裂/最终画面）："
                "锚点没命名的场景会「好看但空」，请先想好这场戏的五个锚点再排镜头"
            )
        for s in bd.shots:
            if s.function not in SHOT_FUNCTIONS:
                issues.append(
                    f"镜头 {s.id} 没有写 function（emotion/action/pressure）："
                    "每个镜头必须至少做一件事——改变情绪、推进动作或施加压力，"
                    "三件都不做的镜头是壁纸，请删掉或合并"
                )
            if not str(s.final_frame or "").strip():
                issues.append(
                    f"镜头 {s.id} 没有写 final_frame：请补上镜头结束那一帧的可见状态——"
                    "视频模型把最后一帧当情绪目的地，没有目的地的镜头会漂"
                )
            count, missing = three_details(
                action=s.action, lighting=s.lighting or "", background_action=s.background_action,
            )
            if count == 0:
                issues.append(
                    f"镜头 {s.id} 一个物理细节都没有（零细节 = 废镜头）："
                    "请补环境压力、身体微动作、声音/视觉母题至少各一处，"
                    "「建立镜头」「过渡镜头」不豁免"
                )
            elif count == 1:
                issues.append(
                    f"镜头 {s.id} 只有「{missing[0]}」一类细节，画面会单薄："
                    "在不臃肿的前提下再补一到两类物理细节"
                )
    return issues


# ---------- 摄影指导侧：最终提示词审计（写进 prompt_audit 供人工审阅） ----------


def audit_video_prompt(shot: dict[str, Any], *, style_context: str = "") -> list[str]:
    """对最终 video_prompt 做第二道审计，返回人类可读的提醒列表。

    只报「修了会明显出片」的问题；风格偏好类不报（沿用质检的教训：
    把小瑕疵也算 FAIL 会让审阅者对告警脱敏）。

    参数：
      shot          —— 镜头 dict，至少含 video_prompt；若有 camera 字段（运镜轨迹）会更准
      style_context —— 全片风格段（美术指导的 camera_style）。用于排除「本镜没有的运镜」：
                       风格段每个镜头都带一遍，不排除会把所有镜头都误判成一镜多运镜。
    """
    prompt = str(shot.get("video_prompt") or "").strip()
    if not prompt:
        return []
    issues: list[str] = []
    if len(prompt) < 20:
        issues.append("提示词过短：没有给模型可渲染的物理事实，几乎必然自由发挥")
    slop = find_slop_words(prompt)
    if slop:
        issues.append(f"含渲染不出来的占位词：{'、'.join(sorted(set(slop)))}——请换成具体的物理细节")
    negations = find_negations(prompt)
    if negations:
        uniq = "、".join(dict.fromkeys(negations))
        issues.append(f"含否定式描述（{uniq[:24]}）：模型会把被否定的动作画出来，请改写成它应该怎样")
    quotes = find_spoken_quotes(prompt)
    if quotes:
        issues.append(f"含引用台词（「{quotes[0][:12]}」）：会渲染成含混的假说话，语音应交给后期配音")

    # 一镜多运镜：只看**本镜自己的**运镜——剥掉全片风格段，且要求动词带镜头语境。
    own_text = strip_style_block(prompt, style_context)
    flat = set(find_camera_moves(own_text))
    camera = shot.get("camera")
    if isinstance(camera, dict):
        flat |= set(find_camera_moves(str(camera.get("path") or "")))
    if len(flat) >= 2:
        issues.append(
            "疑似一镜多运镜（" + "、".join(sorted(flat)[:3]) + "）："
            "一镜只保留一种主要运动，其余删掉或另起一镜"
        )

    # 同一句写两遍：多半是兜底逻辑重复追加，模型会当成两个连续动作。
    dup = find_repeated_clause(own_text)
    if dup:
        issues.append(
            f"同一句写了两遍（「{dup[:18]}」）：视频模型会当成两个连续动作或两次运镜，删掉一处"
        )
    return issues


def refresh_prompt_audits(shots: list[dict[str, Any]], *, style_context: str = "") -> None:
    """就地刷新一组镜头 dict 的 prompt_audit 字段（人工编辑 prompt 后调用）。"""
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        shot["prompt_audit"] = audit_video_prompt(shot, style_context=style_context)

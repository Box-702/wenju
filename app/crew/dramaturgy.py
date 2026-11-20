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

# 否定句的**语法层**：词表只能挡住固定写法，同义改写一律漏——「没碰」「未拿」
# 「不拿」都不在词表里（实测漏报）。补一层保守的「否定副词 + 物理动词」模式，
# 只收触碰 / 视线 / 说话 / 抬头低头这四类（规则 8 点名的高发写法）。
#
# 「不停」「不动」「不长」这类**正向表述不收**——「脚步不停」是我们自己在规则 8
# 里推荐的写法（否定动作延续 → 改成正向），收了就是自己打自己。
_NEGATION_GRAMMAR_RE = re.compile(
    r"(?:不|没|未|别)(?:碰|摸|拿|穿|越过|接触)"
    r"|(?:不|没|未|别)(?:看|说|睁|闭)"
    r"|(?:不|没|未)(?:抬头|低头)"
)

# ---------------------------------------------------------------------
# 台词引用：引号直挂说话动词且**没有冒号**的旧写法（`说道「…」`）会渲染成
# 含混假说话，剥除；官方音画句式（`角色说话：「…」`，冒号挂引号）保留——
# native 音画同生模式下它是成片台词人声的唯一来源（2026-11-13 判据放宽）。
# 招牌/字幕类引用（「写着」）不带冒号也不带动词，不受影响。
# ---------------------------------------------------------------------
_SPEECH_VERBS = ("说", "问", "答", "喊", "叫", "念", "读", "道", "开口", "报告", "低声", "大声", "嘀咕", "嘟囔")
_QUOTE_SPAN = re.compile(r"[「『“\"]([^」』”\"]*)[」』”\"]")

#: 子句切分：中文顿号也算——「A、B、A」这种重复前面常挂着顿号。
_CLAUSE_SPLIT_RE = re.compile(r"[，。；;、\n]")

# 组提示词的跨切镜重述块：「（身份+光线锚不变）」——设计性重复，复读检测豁免。
_LOCK_BLOCK_RE = re.compile(r"（[^（）]*(?:不变)[^（）]*）|\([^()]*(?:unchanged)[^()]*\)")

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
#
# 这是**不带语境要求的原始表**：风格段剥离（strip_camera_moves）用它——
# 风格段里孤零零一个「推近」本身就是一条运镜指令，不该因为附近没有「镜头」二字就放过。
_CAMERA_MOVE_RE = re.compile(
    r"(推近|拉近|推入|拉出|后拉|横移|摇镜|摇向|环绕|跟拍|跟随|升降|俯冲|推进)"
    r"|\b(push[- ]?in|pull[- ]?out|pan|orbit|tracking|dolly|crane|tilt)\b",
    re.IGNORECASE,
)

# 强运镜动词：提示词里几乎只用于描述镜头运动——**无论离镜头语境词多远都算运镜**。
#
# 为什么分级（2026-11-17）：旧实现用固定 14 字语境窗口，于是
# 「机位在苏砚左前方约两米五，齐腰高度略仰，沿通道中轴向前缓慢推进约六十厘米」
# 这种**我们自己 SKILL.md 教的写法**检不出「推进」——审计器对它零告警，
# 「一镜多运镜」也就对它零感知（实测漏报，见 DEVELOPMENT 5.60）。
# 而把窗口直接放宽又会重演 2026-10 那次事故：真实提示词 4/4 全被误报成
# 「一镜多运镜」（「在靠门地面拉出一道冷光带」里的「拉出」被当成 pull-out）。
# 分级之后两头都不再漏：强动词无条件命中，弱动词才要求语境。
_CAMERA_MOVE_STRONG_RE = re.compile(
    r"(推近|急推|拉远|急拉|推进|横移|摇镜|摇向|横摇|上摇|下摇|环绕|跟拍|跟随|升降|俯冲|甩镜|变焦)"
    r"|\b(push[- ]?in|pull[- ]?out|push[- ]?back|pan|tilt|orbit|tracking|dolly|crane|zoom)\b",
    re.IGNORECASE,
)

# 弱运镜动词：日常义与镜头义都有（拉近/推入/后拉），必须**同一分句**里出现
# 镜头语境词才算运镜。「拉出」整条移出——它只出现在「拉出一道冷光带」这种
# 物理描述里，而中文镜头语言几乎不用它写运镜（「拉远/后拉」才是）。
_CAMERA_MOVE_WEAK_RE = re.compile(r"(拉近|推入|后拉)")

# 手持/晃动的标记词。handheld 在我们的机位类型里是**独立的一种**，
# 所以「本镜已声明运镜 + 又写手持晃动」= 一镜两种机位运动，要报。
# （纯静止镜不报——规则 12 明确允许把手持晃动当真实感写。）
#
# 「晃动」必须带镜头语境才算：实测真实提示词里「油灯火苗偶尔被穿堂风带得晃动，
# 光影随之轻颤」说的是**烛火**不是机位——按字面匹配会把这 11 个镜头全误报。
_HANDHELD_STRONG_RE = re.compile(r"(手持|机身晃|机身带)")
_HANDHELD_WEAK_RE = re.compile(r"(晃动|晃了晃|轻微晃)")
# 镜头语境词（手持用）：与 _CAMERA_CONTEXT_WORDS 同源，但不含「画面」——
# 「画面轻微晃动」可以是任何东西在晃。
_HANDHELD_CONTEXT_RE = re.compile(r"(机身|机位|摄影机|摄像机|镜头|相机|取景|画框)")

# 运镜同义词归一：推近/推进/拉近/推入 都是**同一种运动**的不同写法。
# 不归一就会把「推进」和「推近」数成两种运镜——实测真实提示词里 15 个镜头
# 因此被误报「一镜多运镜」。
_CAMERA_MOVE_CANONICAL: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("推", ("推近", "急推", "推进", "拉近", "推入", "push-in", "dolly", "push in")),
    ("拉", ("拉远", "急拉", "后拉", "pull-out", "push-back", "pull out")),
    ("横移", ("横移",)),
    ("摇", ("摇镜", "摇向", "横摇", "上摇", "下摇", "pan", "tilt")),
    ("环绕", ("环绕", "orbit")),
    ("跟", ("跟拍", "跟随", "tracking")),
    ("升降", ("升降", "crane")),
    ("俯冲", ("俯冲",)),
    ("甩", ("甩镜",)),
    ("变焦", ("变焦", "zoom")),
)


def canonical_camera_moves(verbs: list[str]) -> list[str]:
    """把运镜动词归一成「运动类型」——同义词算同一种。

    「推近」和「推进」是同一种运动；数成两种就是误报。
    """
    out: list[str] = []
    for verb in verbs:
        low = verb.lower()
        for canon, members in _CAMERA_MOVE_CANONICAL:
            if any(low == m.lower() for m in members):
                if canon not in out:
                    out.append(canon)
                break
        else:
            if verb not in out:
                out.append(verb)
    return out

# 镜头语境词：弱运镜动词必须挨着它（同一分句），才算「在描述镜头怎么动」。
# 没有语境词时，「在地面拉出一道冷光带」里的「拉出」会被误当成 pull-out——
# 这类误报曾让真实提示词 4/4 全被标成「一镜多运镜」。
_CAMERA_CONTEXT_WORDS = (
    "镜头", "摄影机", "摄像机", "机位", "运镜", "视角", "构图", "景别", "画面", "camera", "shot",
)


def _mentions_handheld(text: str) -> bool:
    """文本里有没有在说**机位**的手持/晃动。

    「手持」一律算；「晃动」必须带镜头语境词——否则「油灯火苗被穿堂风带得晃动」
    会被当成机位手持（实测真实提示词里这一句出现过 11 次，全是烛火）。
    """
    if not text:
        return False
    if _HANDHELD_STRONG_RE.search(text):
        return True
    for clause in re.split(_CLAUSE_SPLIT_RE, text):
        if _HANDHELD_WEAK_RE.search(clause) and _HANDHELD_CONTEXT_RE.search(clause):
            return True
    return False


def _split_sentences(text: str) -> list[str]:
    """按句号类标点切句（分号也算句界——中文提示词里分号常起断句作用）。"""
    return [s for s in re.split(r"[。！？!?\n；;]+", text) if s]


def _clause_spans(sentence: str) -> list[tuple[int, int]]:
    """句内分句的 (起, 止) 区间。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _CLAUSE_SPLIT_RE.finditer(sentence):
        if m.start() > pos:
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < len(sentence):
        spans.append((pos, len(sentence)))
    return spans


def _has_camera_context(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _CAMERA_CONTEXT_WORDS)


def find_camera_moves(text: str, *, context_window: int | None = None) -> list[str]:
    """找出提示词里的运镜动词。

    强动词（推进/横移/环绕/跟拍…）无条件命中；弱动词（拉近/推入/后拉）要求
    **同一分句**里出现镜头语境词（镜头/机位/构图…）——「距离拉近」不是运镜，
    「镜头拉近」才是。

    `context_window` 为兼容旧调用保留：给定时，弱动词额外放宽到前后该字数
    （默认不给 = 只认同分句）。
    """
    if not text:
        return []
    out: list[str] = []
    for sent in _split_sentences(text):
        spans = _clause_spans(sent)
        for m in _CAMERA_MOVE_STRONG_RE.finditer(sent):
            out.append(m.group(0))
        for m in _CAMERA_MOVE_WEAK_RE.finditer(sent):
            clause = ""
            for lo, hi in spans:
                if lo <= m.start() < hi:
                    clause = sent[lo:hi]
                    break
            if not clause:
                clause = sent
            if _has_camera_context(clause):
                out.append(m.group(0))
            elif context_window:
                lo = max(0, m.start() - context_window)
                hi = min(len(sent), m.end() + context_window)
                if _has_camera_context(sent[lo:hi]):
                    out.append(m.group(0))
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

    词表之外还有一层语法匹配（`_NEGATION_GRAMMAR_RE`）：「没碰」「未拿」这类
    同义改写不在词表里，但同样是喂给模型的否定概念。
    """
    if not text:
        return []
    clean = _QUOTE_SPAN.sub("”", text)
    out = [m.group(0) for m in _NEGATION_RE.finditer(clean)]
    out += [m.group(0) for m in _NEGATION_GRAMMAR_RE.finditer(clean)]
    return out


def strip_lock_blocks(text: str) -> str:
    """剥掉组提示词里的跨切镜重述块「（…不变）」（lock line，借鉴清单 #3）。

    这些块是**设计性重复**——模型跨切镜不保留提示词没写全的东西，每个机位
    块尾逐字重述身份+光线锚是防漂移的保险。复读检测（find_repeated_clause）
    不剥就会把每个多机位组全量误报 DPT-15（2026-11-14 审阅实测：锚句出现
    3 次=全局锚 1 次+lock 重述 2 次，全是正当的）。
    """
    return _LOCK_BLOCK_RE.sub(" ", text or "")


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
    """官方音画同出句式：引号前挂冒号的台词（MiniMax/可灵/Seedance 文档确认，
    2026-11-12 提示词规则修订；2026-11-13 放宽判据）。

    原判据字面认「说话：」，但 DP 写出的台词句式五花八门——「哽咽说：「…」」
    「念：「…」」「低声问：「…」」。逐字判据使它们全部按旧写法被剥掉，
    native 音画同生模式下成片就没有台词人声（2026-11-13 孔乙己实测：
    字幕 5 句、音轨只有环境声级响度）。放宽为**引号前是冒号即官方台词句式**，
    保留给模型音画同生；引号直接跟在说话动词后、没有冒号的（`说道「…」`）
    仍是会渲染成含混假语音的旧写法，照旧剥除。招牌/字幕类引用（「写着」）
    不带冒号也不带动词，两种模式下都不受影响。"""
    return head.rstrip().endswith(("：", ":"))


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
    """把「旧写法引用台词（说话动词后直接挂引号、无冒号）」从提示词里剥掉。

    官方 `角色说话：「…」` 句式保留（见 _is_official_dialogue）——native 模式
    它就是成片台词声音；dub 模式的 TTS 归属另按 dialogue_ref 走，不受此影响。"""
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
    # 提示词过长（2026-11-20《替身v2》复盘）：实测 770 字/镜里锚占 500+ 字，
    # 尾部台词与动作被模型忽略（4 个台词镜只有 2 个真出人声）。新粒度目标
    # 一个分镜 ≤250 字（锚已瘦身，见 style-guide 字数预算）。组提示词（多机位）
    # 按机位数放宽：每多一个机位多 200 字预算。
    _n_cam = max(len(re.findall(r"镜头\d+（\d+秒）", prompt)),
                 len(re.findall(r"\[Shot \d+\]", prompt)), 1)
    _cap = 250 + max(0, _n_cam - 1) * 200
    if len(prompt) > _cap:
        issues.append(
            f"提示词过长（{len(prompt)} 字，单分镜预算 {_cap} 字）：锚墙重述会让模型"
            "只执行前几句、忽略尾部台词与动作——压缩环境/人物锚，一个分镜只写一个小事件"
        )
    # 台词在句尾（旧兜底的位置病）：官方句式出现在最后 30 字内，模型最容易漏。
    # 新兜底把台词插进动作链中段，这里只提醒人工编辑时别又挪回末尾。
    _dlg_m = re.search(r"[:：]\s*[「『“\"]", prompt)
    if _dlg_m and len(prompt) - _dlg_m.start() < 30 and len(prompt) > 80:
        issues.append(
            "台词贴在句尾：模型对尾部指令执行最弱（实测有字幕没声音），"
            "把 `角色说话：「…」` 挪进动作链中间"
        )
    slop = find_slop_words(prompt)
    if slop:
        issues.append(f"含渲染不出来的占位词：{'、'.join(sorted(set(slop)))}——请换成具体的物理细节")
    negations = find_negations(prompt)
    if negations:
        uniq = "、".join(dict.fromkeys(negations))
        issues.append(f"含否定式描述（{uniq[:24]}）：模型会把被否定的动作画出来，请改写成它应该怎样")
    quotes = find_spoken_quotes(prompt)
    if quotes:
        issues.append(f"含引用台词（「{quotes[0][:12]}」）：旧写法会渲染成含混假说话，官方句式需在引号前加冒号：角色说话：「…」")

    # 一镜多运镜：只看**本镜自己的**运镜——剥掉全片风格段，且要求动词带镜头语境。
    # 动词先归一成运动类型（「推近」和「推进」是同一种，数成两种就是误报）。
    own_text = strip_style_block(prompt, style_context)
    verbs = find_camera_moves(own_text)
    camera = shot.get("camera")
    if isinstance(camera, dict):
        verbs += find_camera_moves(str(camera.get("path") or ""))
    flat = canonical_camera_moves(verbs)
    if len(flat) >= 2:
        issues.append(
            "疑似一镜多运镜（" + "、".join(flat[:3]) + "）："
            "一镜只保留一种主要运动，其余删掉或另起一镜"
        )

    # 手持晃动 vs 已声明的运镜：handheld 是**独立的机位类型**（_CAMERA_TYPES），
    # 一镜已经有 dolly 再叠手持 = 两种机位运动（规则 1）。
    # 规则 12 允许把「手持轻微晃动」当质感写，但那只在**本镜没有别的运镜**时成立；
    # 本镜已声明运镜时，治 AI 味该用焦点游移 / 曝光起伏（镜头属性，不是机位运动）。
    if flat and _mentions_handheld(own_text):
        issues.append(
            "本镜已声明运镜（" + "、".join(flat[:3]) + "），又写了手持晃动："
            "手持是独立机位类型，一镜只留一种；治 AI 味改用焦点游移 / 曝光起伏"
        )

    # 同一句写两遍：多半是兜底逻辑重复追加，模型会当成两个连续动作。
    # 先剥 lock 重述块——组提示词的跨切镜锚重述是设计性的，不是病（5.48）。
    dup = find_repeated_clause(strip_lock_blocks(own_text))
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
    # 跨镜检查：屏幕方位（轴线）是**镜与镜之间**的事，逐镜审计看不见。
    for shot_id, issues in audit_screen_sides(shots).items():
        for shot in shots:
            if isinstance(shot, dict) and shot.get("id") == shot_id:
                shot.setdefault("prompt_audit", [])
                for issue in issues:
                    if issue not in shot["prompt_audit"]:
                        shot["prompt_audit"].append(issue)
                break


# =====================================================================
# 跨镜屏幕方位（轴线）——逐镜审计够不着的那一类问题
# =====================================================================
#
# 为什么单开一块（2026-11-17）：词表层能查「句子里有没有坏词」，查不了
# 「这一镜和上一镜对不对得上」。而**越轴是我们的第一杀手**（实测：机位从人物
# 一侧绕到另一侧 → 无参考图时背景被重新想象 → 位置对应全崩）。
#
# 规则 13 定了：屏幕左右是**机位属性不是角色身份**——反打时机位描述跟着换，
# 但左右关系不变。于是「同一场戏里某人先在画面左、后在画面右」就是可查的
# 矛盾，比「这一镜好不好」更该报。
#
# 这里从提示词正文里抽「谁在画面哪一侧」，做**字段比对**（不需要理解语义），
# 只在两侧都抽到时才报——抽不准就当没看见，宁可漏报不误报。

_SIDE_AFTER_NAME_RE = re.compile(
    r"([一-龥A-Za-z][一-龥A-Za-z0-9·]{0,5}?)\s*"
    r"(?:位于|处于|站在|靠|贴|在)\s*"
    r"(?:画面|屏幕|镜头|画幅|屏)\s*[^，。；、\n]{0,6}?(左|右)(?:侧|边|方|半边)?"
)

_SIDE_BEFORE_NAME_RE = re.compile(
    r"(?:画面|屏幕|镜头|画幅|屏)\s*[^，。；、\n]{0,4}?(左|右)(?:侧|边|方|半边)?\s*"
    r"(?:是|为|站着|靠着|坐着|站着的是|是站着的)?\s*"
    r"([一-龥A-Za-z][一-龥A-Za-z0-9·]{0,5})"
)

# 抽出来的「名字」里常见的非人称词——它们不是角色，别拿去比对左右。
_NON_NAME_TOKENS = frozenset({
    "站", "在", "他", "她", "它", "我", "你", "们", "那", "这", "有人", "两人", "三人",
    "一个人", "所有", "每个", "其他", "画面", "镜头", "屏幕", "两人之间",
    "他们", "她们", "每个人", "所有人", "那侧", "这侧", "一侧", "那一侧",
    "这一侧", "左边", "右边", "左侧", "右侧", "半边", "角落", "前景", "背景",
    "墙", "墙边", "门口", "桌边", "路边", "台阶", "地上", "空中", "中轴", "中心",
    "侧", "边", "方",
})

# 名字里出现这些字 → 不是人名（正则会把「外套站在」「他的手」这类也吞进来）。
_NAME_BLACKLIST_CHARS = ("在", "站", "位", "靠", "贴", "是", "的", "了", "着", "过",
                         "来", "去", "和", "与", "把", "被", "侧", "边", "方", "着")

# 景别/比例词——它们常出现在「画面右侧三分之二」「中景」这类短语里，
# 抽取名字时会被误当成角色（实测真实提示词里「三分之二」「中景」都进来过）。
_NON_NAME_PATTERNS = ("之分", "分之", "景", "画面", "全身", "半身", "脸部", "眼睛", "手掌",
                      "角", "边缘", "方向", "位置")


def _normalize_name(token: str) -> str:
    """人名归一：去掉编号后缀（「孔乙己2号」→「孔乙己」）。

    不归一的话，同一个人在不同镜头里写成「孔乙己」「孔乙己2号」，
    比对左右时会当成两个人，两边都不冲突 → 真越轴反而看不见。
    """
    token = (token or "").strip()
    return re.sub(r"[0-9０-９]+号$", "", token)


def _looks_like_name(token: str) -> bool:
    """这段文字像不像一个角色名。"""
    token = _normalize_name(token)
    if len(token) < 2 or len(token) > 6:
        return False
    if token in _NON_NAME_TOKENS:
        return False
    if any(p in token for p in _NON_NAME_PATTERNS):
        return False
    return not any(ch in token for ch in _NAME_BLACKLIST_CHARS)


def extract_screen_sides(text: str) -> dict[str, set[str]]:
    """从提示词正文里抽「谁在画面左/右」。

    返回 {角色名: {"左", "右"}}。抽不准就不抽——宁可漏报不误报。
    """
    if not text:
        return {}
    out: dict[str, set[str]] = {}

    def _add(name: str, side: str) -> None:
        if not _looks_like_name(name):
            return
        out.setdefault(_normalize_name(name), set()).add(side)

    for m in _SIDE_AFTER_NAME_RE.finditer(text):
        _add(m.group(1), m.group(2))
    for m in _SIDE_BEFORE_NAME_RE.finditer(text):
        _add(m.group(2), m.group(1))
    return out


def audit_screen_sides(shots: list[dict[str, Any]]) -> dict[str, list[str]]:
    """跨镜比对屏幕方位：同一场戏里同一角色出现在画面两侧 → 疑似越轴。

    返回 {shot_id: [告警]}。只在**两侧都抽到**时才报（单侧抽到说明解析不了，
    不是矛盾）。
    """
    by_scene: dict[str, list[tuple[dict[str, Any], dict[str, set[str]]]]] = {}
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        text = str(shot.get("group_prompt") or shot.get("video_prompt") or "")
        if not text:
            continue
        scene = str(shot.get("scene_id") or "")
        by_scene.setdefault(scene, []).append((shot, extract_screen_sides(text)))

    issues: dict[str, list[str]] = {}
    for scene, items in by_scene.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: int(x[0].get("order") or 0))
        seen: dict[str, dict[str, list[str]]] = {}
        ambiguous: set[str] = set()
        for shot, sides in items:
            for name, side_set in sides.items():
                # 同一镜里两侧都提到 → 解析不了（可能一个是空间描述一个是角色），
                # 这种名字整场不比对，宁可漏报不误报。
                if len(side_set) > 1:
                    ambiguous.add(name)
                for side in side_set:
                    seen.setdefault(name, {}).setdefault(side, []).append(str(shot.get("id") or ""))
        for name, side_map in seen.items():
            if name in ambiguous or len(side_map) < 2:
                continue
            detail = "、".join(
                f"{side}侧:{'/'.join(sids)}" for side, sids in sorted(side_map.items())
            )
            msg = (
                f"同一场戏（{scene}）里「{name}」出现在画面两侧（{detail}）："
                "屏幕左右是机位属性、不是角色身份——反打时机位描述跟着换，左右关系不变；"
                "请确认这一处是不是越轴"
            )
            for sids in side_map.values():
                for sid in sids:
                    issues.setdefault(sid, []).append(msg)
    return issues


# =====================================================================
# CLI：把整套规则变成人可以直接用的工具
# =====================================================================
#
# 为什么要有（2026-11-17）：这套审计器原本只在系统内部自动跑——对话里出的
# 提示词自动过一遍，**手写提示词的人**（自己在别家视频模型上写提示词的作者）
# 用不上：那 13 条规则、五个杀手、两套切镜方言只存在于 SKILL.md 与本文档里。
# 有了入口，作者可以把自己写的提示词丢进来当场看告警。
#
# 用法：
#   uv run python -m app.crew.dramaturgy --file prompt.txt
#   uv run python -m app.crew.dramaturgy --text "镜头缓慢推近……"
#   uv run python -m app.crew.dramaturgy --file prompt.txt --json
#   uv run python -m app.crew.dramaturgy --shots shots.json   # 跨镜轴线检查

def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="python -m app.crew.dramaturgy",
        description="视频提示词确定性审计（否定句/占位词/假说话/一镜多运镜/手持冲突/跨镜轴线）",
    )
    ap.add_argument("--file", help="提示词文件（UTF-8 文本）")
    ap.add_argument("--text", help="直接给一段提示词")
    ap.add_argument("--shots", help="镜头 JSON 数组文件（跨镜轴线检查用）")
    ap.add_argument("--style", help="全片风格段（camera_style），用于排除风格段里的运镜词")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args(argv)

    if args.shots:
        shots = json.loads(Path(args.shots).read_text(encoding="utf-8"))
        result = {
            "shots": {sid: issues for sid, issues in audit_screen_sides(shots).items()},
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if not result["shots"]:
                print("跨镜轴线检查：未发现冲突")
            for sid, issues in result["shots"].items():
                print(f"[{sid}]")
                for issue in issues:
                    print(f"  - {issue}")
        return 0

    text = args.text or ""
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        ap.error("请给 --file / --text / --shots 之一")

    issues = audit_video_prompt({"video_prompt": text}, style_context=args.style or "")
    if args.json:
        print(json.dumps({"issues": issues}, ensure_ascii=False, indent=2))
    else:
        if not issues:
            print("确定性审计：未发现问题")
        else:
            print(f"确定性审计：{len(issues)} 条提醒")
            for issue in issues:
                print(f"  - {issue}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(_cli())

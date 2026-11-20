# =====================================================================
# screenplay.py —— 剧本型原文的确定性解析与回填
#
# 直跑链路（generate_script）只按「原文长什么样」切场，场次标题与昼夜会丢：
# 《替身v2》实测 8 场全部落成兜底「第 N 场」、time 全 None。
#   - 昼夜直接决定下游视频提示词里的光线（夜 / 日 / 傍晚 / 深夜）；
#   - 标题决定交付包的场次表与分镜上下文能不能读懂。
#
# 本模块只做解析与回填，**不改 LLM 产出的内容**：
#   - 标题还是兜底「第 N 场」→ 按原文场标题回填；
#   - time 为空 → 按原文「内景 · 地点 · 时间」回填；
#   - 已有值一律不覆盖（模型写对了就听模型的）。
# =====================================================================

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 场次标题行：「第一场 · 诞生」「第 3 场：偷偷的甜」「第十二场」
_SCENE_HEADER = re.compile(
    r"^\s*第\s*([0-9０-９一二三四五六七八九十百千零两]+)\s*场"
    r"\s*(?:[·・:：\-—|、]\s*)?(.*\S)?\s*$"
)

# 场景抬头行：「内景 · 林远家书房 · 夜」「外景 - 公司打印区 - 日」
_BLOCK_HEADER = re.compile(
    r"^\s*(内景|外景|内|外|日景|夜景|INT\.?|EXT\.?)"
    r"\s*[·・:：\-—|]\s*(.+?)\s*$"
)

# 地点前缀：回填标题时用来做短名（「林远家书房」→「书房」）
_LOC_PREFIXES = ("林远家", "公司", "苏晴家")

_CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(text: str) -> int:
    """「十二」→ 12；认不出来的按 0（调用方只用于排序与展示）。"""
    s = (text or "").strip()
    if s.isdigit():
        return int(s)
    if not s:
        return 0
    if "十" in s:
        head, _, tail = s.partition("十")
        hi = _CN_NUM.get(head, 1) if head else 1
        lo = _CN_NUM.get(tail, 0) if tail else 0
        return hi * 10 + lo
    total = 0
    for ch in s:
        if ch in _CN_NUM:
            total = total * 10 + _CN_NUM[ch]
    return total


@dataclass
class ScreenplayBlock:
    """原文里的一个取景块（一个地点 + 一个时间）。"""

    scene_title: str          # 所属场次的标题（原文场标题行）
    scene_index: int          # 原文场次序号（中文数字已转 int）
    location: str             # 地点名（「林远家书房」）
    time: str | None          # 昼夜（「夜」/「日」/「深夜」）
    is_first_in_scene: bool   # 是否是该场次的第一个取景块
    order: int = field(default=0)


def _split_parts(rest: str) -> list[str]:
    return [p.strip() for p in re.split(r"[·・:：\-—|]", rest) if p.strip()]


def _parse_block_header(line: str) -> tuple[str, str | None] | None:
    """「内景 · 林远家书房 · 夜」→ ("林远家书房", "夜")；不是抬头行则 None。"""
    m = _BLOCK_HEADER.match(line)
    if not m:
        return None
    parts = _split_parts(m.group(2))
    if not parts:
        return None
    location = parts[0]
    time = parts[1] if len(parts) > 1 else None
    # 时间位写得太长就不认（多半是把两个抬头连写了）
    if time and len(time) > 6:
        time = None
    return location, time


def parse_screenplay_blocks(raw_text: str) -> list[ScreenplayBlock]:
    """把剧本型原文拆成「场次 + 取景块」序列。

    识别的行式：
      - 场标题行：「第一场 · 诞生」
      - 取景抬头行：「内景 · 林远家书房 · 夜」
    两者都不认识的行直接跳过（对白、动作描写不参与解析）。
    """
    blocks: list[ScreenplayBlock] = []
    cur_title = ""
    cur_index = 0
    seen_scene: set[int] = set()
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _SCENE_HEADER.match(line)
        if m:
            cur_index = _cn_to_int(m.group(1))
            title = (m.group(2) or "").strip()
            cur_title = title or f"第{cur_index}场"
            continue
        hit = _parse_block_header(line)
        if hit and cur_title:
            location, time = hit
            blocks.append(ScreenplayBlock(
                scene_title=cur_title,
                scene_index=cur_index,
                location=location,
                time=time,
                is_first_in_scene=cur_index not in seen_scene,
                order=len(blocks),
            ))
            seen_scene.add(cur_index)
    return blocks


def _short_location(loc: str) -> str:
    for p in _LOC_PREFIXES:
        if loc.startswith(p) and len(loc) > len(p):
            return loc[len(p):]
    return loc


def _loc_match(block_loc: str, scene_loc: str) -> bool:
    """取景块地点与场景地点是否指同一处（双向子串即可）。"""
    a = (block_loc or "").strip()
    b = (scene_loc or "").strip()
    if not a or not b:
        return False
    return a in b or b in a


def backfill_scenes_from_source(
    scenes: list,
    location_names: dict[str, str],
    raw_text: str,
) -> tuple[int, int]:
    """按原文回填场次标题与昼夜。返回 ``(补了标题的场数, 补了时间的场数)``。

    只补空，不覆盖：模型已经写对的地方原样保留。
    对齐方式：原文取景块与生成场次都按出现顺序走，用「地点名互相包含」
    贪心匹配——剧本型原文的地点名与生成地点名通常一字不差（都是「林远家书房」）。
    """
    blocks = parse_screenplay_blocks(raw_text)
    if not blocks or not scenes:
        return (0, 0)

    fixed_title = 0
    fixed_time = 0
    used: set[int] = set()
    for sc in scenes:
        scene_loc = location_names.get(sc.location_id, "")
        hit: ScreenplayBlock | None = None
        for b in blocks:
            if b.order in used:
                continue
            if _loc_match(b.location, scene_loc):
                hit = b
                break
        if hit is None:
            continue
        used.add(hit.order)

        if not sc.time and hit.time:
            sc.time = hit.time
            fixed_time += 1

        if not sc.title or re.fullmatch(r"第\s*[0-9]+\s*场", sc.title.strip()):
            title = (hit.scene_title or "").strip()
            if title:
                if hit.is_first_in_scene:
                    sc.title = title
                else:
                    sc.title = f"{title}·{_short_location(hit.location)}"
                fixed_title += 1
    return (fixed_title, fixed_time)

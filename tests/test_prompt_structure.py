# =====================================================================
# test_prompt_structure.py —— 提示词结构改造（2026-11-20，A2）
#
# 《替身v2》实测：平均 772 字/镜的提示词里锚占 500+ 字，台词被拼在末尾——
# 4 个台词镜只有 2 个真出了人声（−33dB 的镜就是「有字幕没声音」）。
# 即梦的写法是台词嵌在动作链中间、每条 ≤200 字。
#
# 三处改：
#   1. DP 台词兜底插进动作链中段（_insert_dialogue，按 dialogue_at 折算句位）；
#   2. 审计加「提示词过长」告警（单分镜 ≤250 字，组按机位数放宽）；
#   3. 审计加「台词贴句尾」告警（人工编辑别把台词挪回末尾）。
# =====================================================================

from __future__ import annotations

from app.crew.dp import _insert_dialogue
from app.crew.dramaturgy import audit_video_prompt
from app.domain import CameraMovement, Shot


def _shot(**kw) -> Shot:
    data = {
        "id": "shot_scene_001_000", "scene_id": "scene_001", "order": 0,
        "duration_sec": 4.0, "camera": CameraMovement(type="static"),
    }
    data.update(kw)
    return Shot.model_validate(data)


# ---- 1. 台词插入位置 ----


def test_台词插在第一句动作后():
    body = "同事乙从画面右侧凑过来，一只手搭在打印机顶上。歪头看了看红灯"
    out = _insert_dialogue(body, "同事乙说话：「这台老机器又坏了。」", _shot(), "zh")
    # 台词不在句尾：后面还有动作
    assert out.endswith("歪头看了看红灯")
    assert "这台老机器又坏了" in out
    assert out.index("这台老机器") < out.index("歪头看了看红灯")


def test_dialogue_at_决定插入句位():
    body = "第一句动作。第二句动作。第三句动作。第四句动作"
    s = _shot(duration_sec=4.0, dialogue_at=3.0)   # 第 3 秒开口 → 3/4 处
    out = _insert_dialogue(body, "他说话：「来了。」", s, "zh")
    # 台词在第三句之后、第四句之前
    i = out.index("来了")
    assert out.index("第三句动作") < i < out.index("第四句动作")


def test_单句body退化为追加():
    out = _insert_dialogue("他端起水杯。", "他说话：「谢谢。」", _shot(), "zh")
    assert "谢谢" in out and "端起水杯" in out


def test_空body只留台词():
    assert _insert_dialogue("", "她说话：「辛苦了。」", _shot(), "zh") == "她说话：「辛苦了。」"


# ---- 2. 过长告警 ----


def test_过长提示词被提醒():
    long_prompt = "锚" * 300
    issues = audit_video_prompt({"video_prompt": long_prompt})
    assert any(i.startswith("提示词过长") for i in issues)


def test_预算内提示词不误报():
    ok = "夜晚书房，@林远(char_001)：38岁男性，短发乱。他撑起身抓起衣服丢到地板上。"
    assert not any(i.startswith("提示词过长") for i in audit_video_prompt({"video_prompt": ok}))


def test_组提示词按机位数放宽():
    # 3 机位组：预算 250 + 2×200 = 650，600 字不该报
    seg = "镜头1（4秒）：" + "动作描述" * 25 + "。镜头2（4秒）：" + "动作描述" * 25 + "。镜头3（4秒）：" + "动作描述" * 25
    assert 250 < len(seg) <= 650          # 超单镜预算、在 3 机位放宽内
    issues = audit_video_prompt({"video_prompt": seg})
    assert not any(i.startswith("提示词过长") for i in issues), issues


# ---- 3. 句尾台词告警 ----


def test_台词贴句尾被提醒():
    body = "他慢慢站起来，绕过桌子走到舱前，俯视舱内的自己。" + "细节描写" * 12
    tail = "林远说话：「别紧张。」"
    issues = audit_video_prompt({"video_prompt": body + tail})
    assert any("台词贴在句尾" in i for i in issues)


def test_台词在句中不误报():
    body = "他慢慢站起来。林远说话：「别紧张。」然后绕过桌子走到舱前。" + "细节" * 20
    issues = audit_video_prompt({"video_prompt": body})
    assert not any("台词贴在句尾" in i for i in issues)
